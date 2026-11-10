"""
Bot de músicas para live do TikTok - versão MULTIUSUÁRIO (v3)
- Cada usuário tem conta própria, sua live do TikTok, fila, biblioteca e configurações.
- Os arquivos de áudio ficam em cache compartilhado (uma música baixada serve a todos).
- O som toca no navegador de cada usuário (painel aberto no aparelho ligado à caixa de som).

Variáveis de ambiente (todas opcionais, exceto trocar o convite):
  BOT_CONVITE       código exigido para criar conta   (padrão: troque-este-codigo)
  BOT_MAX_USUARIOS  limite de contas                  (padrão: 20)
  BOT_MAX_GB        limite de disco do cache          (padrão: 10)
  BOT_YT_COOKIES    caminho de um cookies.txt do YouTube (ajuda quando o servidor é bloqueado)
  BOT_STREAMING     1 = não salva NENHUM arquivo: o áudio passa direto pelo servidor até o painel
  BOT_SO_BIBLIOTECA 1 = não baixa nada novo: só toca o que já está salvo (busca também só na biblioteca)
  HOST / PORT       endereço e porta                  (padrão: 0.0.0.0 / 8000)
"""
import asyncio
import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import sqlite3
import time
from collections import deque
from pathlib import Path

import httpx
import uvicorn
import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from TikTokLive import TikTokLiveClient
from TikTokLive.events import (CommentEvent, ConnectEvent, DisconnectEvent,
                               FollowEvent, GiftEvent, JoinEvent)

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))
CONVITE = os.getenv("BOT_CONVITE", "local-android")
MAX_USUARIOS = int(os.getenv("BOT_MAX_USUARIOS", "20"))
MAX_GB = float(os.getenv("BOT_MAX_GB", "10"))
YT_COOKIES = os.getenv("BOT_YT_COOKIES", "")
SO_BIBLIOTECA = os.getenv("BOT_SO_BIBLIOTECA", "") == "1"
STREAMING = os.getenv("BOT_STREAMING", "") == "1"
DOWNLOADS_SIMULTANEOS = 2

CFG_PADRAO = {"max_duracao": 600, "max_por_pessoa": 3, "votos_pular": 3,
              "bloqueadas": [], "mods": [], "autoplay": True, "volume": 80}

BASE = Path(os.getenv("BOT_DATA_DIR", str(Path(__file__).parent)))
MUSIC_DIR = BASE / "musicas"
HTML_PATH = BASE / "painel.html"
CHAT_PATH = BASE / "chat.html"
OVERLAY_PATH = BASE / "overlay.html"
UNIFICADO_PATH = BASE / "overlay-unificado.html"
MUSICAS_PATH = BASE / "overlay-musicas.html"
MIMES = {".m4a": "audio/mp4", ".mp4": "audio/mp4", ".webm": "audio/webm",
         ".opus": "audio/ogg", ".mp3": "audio/mpeg"}

# ---------------- banco de dados ----------------
db = sqlite3.connect(BASE / "dados.db", check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS usuarios(
  id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT UNIQUE, senha TEXT, sal TEXT,
  tiktok TEXT DEFAULT '', cfg TEXT, overlay TEXT, criado REAL);
CREATE TABLE IF NOT EXISTS sessoes(token TEXT PRIMARY KEY, uid INTEGER, criada REAL);
CREATE TABLE IF NOT EXISTS faixas(
  id TEXT PRIMARY KEY, titulo TEXT, artista TEXT, duracao INTEGER,
  arquivo TEXT, tamanho INTEGER DEFAULT 0, usada REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS biblioteca(
  uid INTEGER, vid TEXT, tocadas INTEGER DEFAULT 0, ultima REAL DEFAULT 0,
  favorita INTEGER DEFAULT 0, PRIMARY KEY(uid, vid));
""")
db.commit()


def _migrar():
    """Migrações idempotentes: preservam todos os dados existentes."""
    cols = {r["name"] for r in db.execute("PRAGMA table_info(usuarios)")}
    if "ovcfg" not in cols:
        db.execute("ALTER TABLE usuarios ADD COLUMN ovcfg TEXT")
    if "ovmcfg" not in cols:
        db.execute("ALTER TABLE usuarios ADD COLUMN ovmcfg TEXT")
    db.executescript("""
CREATE TABLE IF NOT EXISTS pedidos(
  id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, vid TEXT, titulo TEXT, artista TEXT,
  por TEXT, tid TEXT, quando REAL);
CREATE INDEX IF NOT EXISTS ix_pedidos_uid ON pedidos(uid, id);
""")
    db.commit()


_migrar()

# ---------------- configuração do overlay unificado (salva por usuário) ----------------
OV_PADRAO = {"cor1": "#fe2c55", "cor2": "#25f4ee", "max": 4, "capas": True, "avatares": True,
             "pedido": True, "tempo": True, "escala": 100, "posicao": "ie", "margem": 36,
             "vel": 100, "espaco": 8, "estilo": "vidro", "auto_ocultar": 0}
OV_ENUMS = {"posicao": ("se", "sd", "ie", "id", "c"), "estilo": ("vidro", "solido", "minimo")}
OV_NUMS = {"max": (1, 8), "escala": (50, 150), "margem": (0, 200), "vel": (50, 200),
           "espaco": (0, 24), "auto_ocultar": (0, 3600)}


def limpar_ov(d, base):
    """Valida a configuração recebida; valores inválidos mantêm o que já existia."""
    out = dict(base)
    if not isinstance(d, dict):
        return out
    for k in ("cor1", "cor2"):
        v = d.get(k)
        if isinstance(v, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", v):
            out[k] = v.lower()
    for k, (lo, hi) in OV_NUMS.items():
        try:
            out[k] = max(lo, min(hi, int(d[k])))
        except (KeyError, TypeError, ValueError):
            pass
    for k in ("capas", "avatares", "pedido", "tempo"):
        if isinstance(d.get(k), bool):
            out[k] = d[k]
    for k, ok in OV_ENUMS.items():
        if d.get(k) in ok:
            out[k] = d[k]
    return out


# ---------------- configuração do overlay de músicas (estilo lista, salva por usuário) ----------------
OVM_PADRAO = {
    "capa": 72, "espaco": 14, "tam_titulo": 22, "tam_info": 15, "raio": 10, "fonte": "sistema", "sombra": True,
    "cor_titulo": "#ffffff", "cor_artista": "#cfd2dc", "cor_pedido": "#25f4ee", "cor_tempo": "#ffd166",
    "posicao": "se", "pos_x": 3, "pos_y": 5, "margem": 32,
    "m_capas": True, "m_artistas": True, "m_pedido": True, "m_tempo": True, "m_inicio": True,
    "m_barra": True, "m_eq": True, "max": 5,
    "anim": True, "vel": 100, "t_entrada": 500, "t_saida": 350}
OVM_ENUMS = {"posicao": ("se", "sd", "ie", "id", "c", "pers"),
             "fonte": ("sistema", "arial", "verdana", "tahoma", "trebuchet", "georgia", "courier", "impact")}
OVM_NUMS = {"capa": (40, 160), "espaco": (0, 48), "tam_titulo": (12, 44), "tam_info": (10, 30), "raio": (0, 50),
            "pos_x": (0, 100), "pos_y": (0, 100), "margem": (0, 200), "max": (1, 10),
            "vel": (50, 200), "t_entrada": (100, 2000), "t_saida": (100, 2000)}
OVM_CORES = ("cor_titulo", "cor_artista", "cor_pedido", "cor_tempo")
OVM_BOOLS = ("sombra", "m_capas", "m_artistas", "m_pedido", "m_tempo", "m_inicio", "m_barra", "m_eq", "anim")


def limpar_ovm(d, base):
    """Valida a configuração do overlay de músicas; valores inválidos mantêm o que já existia."""
    out = dict(base)
    if not isinstance(d, dict):
        return out
    for k in OVM_CORES:
        v = d.get(k)
        if isinstance(v, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", v):
            out[k] = v.lower()
    for k, (lo, hi) in OVM_NUMS.items():
        try:
            out[k] = max(lo, min(hi, int(float(d[k]))))
        except (KeyError, TypeError, ValueError):
            pass
    for k in OVM_BOOLS:
        if isinstance(d.get(k), bool):
            out[k] = d[k]
    for k, ok in OVM_ENUMS.items():
        if d.get(k) in ok:
            out[k] = d[k]
    return out


def solicitante_de(x):
    """Nome de quem pediu, vindo dos dados reais do pedido. Nunca inventa: sem nome => 'Desconhecido'."""
    por = str(x.get("por") or "").strip()
    if por == "painel":
        return "Administrador"
    if por == "autoplay":
        return "Autoplay"
    return por or "Desconhecido"


def avatar_de(user):
    """Primeira URL https de avatar do usuário do TikTok (tolerante a versões da lib)."""
    for attr in ("avatar_thumb", "avatar_medium", "avatar_large"):
        img = getattr(user, attr, None)
        for campo in ("urls", "url_list", "m_urls"):
            for u in (getattr(img, campo, None) or []) if img else []:
                if isinstance(u, str) and u.startswith("https://") and len(u) < 600:
                    return u
    return ""


def origem_de(por):
    return "auto" if por == "autoplay" else "admin" if por == "painel" else ("chat" if por else "")


sem_download = asyncio.Semaphore(DOWNLOADS_SIMULTANEOS)
baixando: dict = {}
_tarefas: set = set()
SESSOES: dict = {}


def bg(coro):
    t = asyncio.create_task(coro)
    _tarefas.add(t)
    t.add_done_callback(_tarefas.discard)
    return t


# ---------------- busca e download (compartilhados) ----------------
def _opts_base():
    o = {"quiet": True, "no_warnings": True, "noplaylist": True}
    if YT_COOKIES and Path(YT_COOKIES).exists():
        o["cookiefile"] = YT_COOKIES
    return o


def _buscar_sync(query, n):
    opts = {**_opts_base(), "extract_flat": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as y:
        if re.match(r"https?://", query):
            entries = [y.extract_info(query, download=False)]
        else:
            entries = (y.extract_info(f"ytsearch{n}:{query}", download=False).get("entries") or [])
    out = []
    for e in entries:
        if e and e.get("id"):
            out.append({"id": e["id"], "titulo": e.get("title") or "Sem título",
                        "artista": e.get("channel") or e.get("uploader") or "",
                        "duracao": int(e.get("duration") or 0)})
    return out


async def buscar(query, n=1):
    return await asyncio.to_thread(_buscar_sync, query, n)


def buscar_local(q, n=1):
    """Busca só nas músicas já salvas (modo sem downloads)."""
    termos = [t for t in q.split() if t][:6]
    if not termos:
        return []
    sql = "SELECT id,titulo,artista,duracao FROM faixas WHERE arquivo IS NOT NULL"
    args = []
    for t in termos:
        sql += " AND (titulo LIKE ? OR artista LIKE ?)"
        args += [f"%{t}%", f"%{t}%"]
    return [dict(r) for r in db.execute(sql + " ORDER BY usada DESC LIMIT ?", args + [n]).fetchall()]


async def buscar_auto(q, n=1):
    return buscar_local(q, n) if SO_BIBLIOTECA else await buscar(q, n)


# ---- modo streaming: nada é salvo em disco ----
_urls: dict = {}


def _resolver_sync(vid):
    opts = {**_opts_base(), "format": "bestaudio[ext=m4a]/bestaudio/best"}
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
    return info["url"], info.get("http_headers") or {}


async def resolver(vid):
    c = _urls.get(vid)
    if c and c[2] > time.time():
        return c[0], c[1]
    url, h = await asyncio.to_thread(_resolver_sync, vid)
    _urls[vid] = (url, h, time.time() + 3000)
    return url, h


def salvar_meta(f):
    """Guarda só título/artista/duração (sem arquivo) para a biblioteca."""
    db.execute(
        """INSERT INTO faixas(id,titulo,artista,duracao,arquivo,tamanho,usada) VALUES(?,?,?,?,'',0,?)
           ON CONFLICT(id) DO UPDATE SET titulo=excluded.titulo, artista=excluded.artista,
             duracao=excluded.duracao""",
        (f["id"], f["titulo"], f["artista"], f["duracao"], time.time()))
    db.commit()


def _baixar_sync(vid):
    opts = {**_opts_base(), "format": "bestaudio[ext=m4a]/bestaudio/best",
            "outtmpl": str(MUSIC_DIR / "%(id)s.%(ext)s")}
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(f"https://www.youtube.com/watch?v={vid}", download=True)
        reqs = info.get("requested_downloads") or [{}]
        arquivo = reqs[0].get("filepath") or y.prepare_filename(info)
    return info, arquivo


async def _baixar(vid):
    async with sem_download:
        info, arquivo = await asyncio.to_thread(_baixar_sync, vid)
    db.execute(
        """INSERT INTO faixas(id,titulo,artista,duracao,arquivo,tamanho,usada)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET titulo=excluded.titulo, artista=excluded.artista,
             duracao=excluded.duracao, arquivo=excluded.arquivo, tamanho=excluded.tamanho""",
        (vid, info.get("title") or "Sem título", info.get("uploader") or "",
         int(info.get("duration") or 0), arquivo,
         os.path.getsize(arquivo) if os.path.exists(arquivo) else 0, time.time()))
    db.commit()
    limpar_disco()
    return arquivo


async def garantir_arquivo(vid):
    if STREAMING:
        try:
            await resolver(vid)
            return "stream"
        except Exception as e:
            print(f"(stream {vid}) {str(e)[:200]}")
            return None
    r = db.execute("SELECT arquivo FROM faixas WHERE id=?", (vid,)).fetchone()
    if r and r["arquivo"] and Path(r["arquivo"]).exists():
        return r["arquivo"]
    if SO_BIBLIOTECA:
        return None
    t = baixando.get(vid)
    if not t:
        t = asyncio.create_task(_baixar(vid))
        baixando[vid] = t
        t.add_done_callback(lambda _, v=vid: baixando.pop(v, None))
    try:
        return await t
    except Exception as e:
        print(f"(download {vid}) {str(e)[:200]}")
        return None


def limpar_disco():
    """Apaga as músicas menos usadas quando o cache passa do limite."""
    limite = MAX_GB * 1024 ** 3
    total = db.execute("SELECT COALESCE(SUM(tamanho),0) FROM faixas").fetchone()[0]
    if total <= limite:
        return
    em_uso = {x["id"] for s in SESSOES.values() for x in s.fila + ([s.atual] if s.atual else [])}
    for r in db.execute("SELECT id,arquivo,tamanho FROM faixas ORDER BY usada ASC").fetchall():
        if total <= limite * 0.9:
            break
        if r["id"] in em_uso:
            continue
        try:
            Path(r["arquivo"]).unlink(missing_ok=True)
        except Exception:
            pass
        db.execute("DELETE FROM faixas WHERE id=?", (r["id"],))
        total -= r["tamanho"]
    db.commit()


# ---------------- sessão de cada usuário ----------------
class Sessao:
    def __init__(self, row):
        self.uid = row["id"]
        self.nome = row["nome"]
        self.tiktok = (row["tiktok"] or "").lstrip("@")
        self.overlay = row["overlay"]
        self.cfg = {**CFG_PADRAO, **json.loads(row["cfg"] or "{}")}
        try:
            self.ov = limpar_ov(json.loads(row["ovcfg"] or "{}"), OV_PADRAO)
        except Exception:
            self.ov = dict(OV_PADRAO)
        try:
            self.ovm = limpar_ovm(json.loads((row["ovmcfg"] if "ovmcfg" in row.keys() else None) or "{}"), OVM_PADRAO)
        except Exception:
            self.ovm = dict(OVM_PADRAO)
        self.fila = []
        self.atual = None
        self.tocando = False
        self.pausado = False
        self.volume = self.cfg["volume"]
        self.autoplay = self.cfg["autoplay"]
        self.votos = set()
        self.conectado = False
        self.seq = 0
        self.evt_fim = None
        self.acordar = asyncio.Event()
        self.log = deque(maxlen=60)
        self.eventos = deque(maxlen=300)   # chat, presentes, seguidores, entradas
        self.ev_id = 0
        self._ult_join = 0.0
        self.imp = {"rodando": False, "total": 0, "feitas": 0, "falhas": 0}
        self.t_player = None
        self.t_tiktok = None
        # posição da música (para o overlay): o painel informa; entre avisos o servidor estima
        self.pos, self.pos_t, self.pos_dur = 0.0, 0.0, 0
        self.historico = deque(maxlen=30)   # mais recente primeiro
        for r in db.execute(
                "SELECT f.id,f.titulo,f.artista,f.duracao,b.ultima FROM biblioteca b JOIN faixas f ON f.id=b.vid "
                "WHERE b.uid=? AND b.tocadas>0 ORDER BY b.ultima DESC LIMIT 30", (self.uid,)).fetchall():
            self.historico.append({"id": r["id"], "titulo": r["titulo"], "artista": r["artista"],
                                   "duracao": r["duracao"], "por": "", "quando": r["ultima"]})

    # ---- util ----
    def say(self, msg):
        print(f"[{self.nome}] {msg}")
        self.log.appendleft(f"{time.strftime('%H:%M:%S')}  {msg}")

    def push(self, tipo, nome, txt="", qtd=0):
        self.ev_id += 1
        self.eventos.append({"id": self.ev_id, "t": tipo, "nome": str(nome)[:60],
                             "txt": str(txt)[:300], "qtd": qtd})

    def salvar(self):
        self.cfg["autoplay"] = self.autoplay
        self.cfg["volume"] = self.volume
        db.execute("UPDATE usuarios SET tiktok=?, cfg=? WHERE id=?",
                   (self.tiktok, json.dumps(self.cfg), self.uid))
        db.commit()

    def iniciar(self):
        self.t_player = asyncio.create_task(self.loop_player())
        self.reiniciar_tiktok()

    def reiniciar_tiktok(self):
        if self.t_tiktok:
            self.t_tiktok.cancel()
            self.t_tiktok = None
        self.conectado = False
        if self.tiktok:
            self.t_tiktok = asyncio.create_task(self.loop_tiktok())

    def estado(self):
        return {"atual": self.atual, "tocando": self.tocando, "pausado": self.pausado,
                "volume": self.volume, "autoplay": self.autoplay, "conectado": self.conectado,
                "fila": self.fila, "log": list(self.log)[:25], "imp": self.imp,
                "seq": self.seq, "tiktok": self.tiktok}

    # ---- fila ----
    async def enfileirar(self, f, por="painel", tid="painel", checar=False, avatar=""):
        c = self.cfg
        if checar:
            if c["max_duracao"] and f["duracao"] and f["duracao"] > c["max_duracao"]:
                return False, f"muito longa ({f['duracao'] // 60} min): {f['titulo']}"
            if sum(1 for x in self.fila if x.get("tid") == tid) >= c["max_por_pessoa"]:
                return False, f"{por} já tem {c['max_por_pessoa']} músicas na fila"
            if any(p.lower() in f["titulo"].lower() for p in c["bloqueadas"]):
                return False, f"título bloqueado: {f['titulo']}"
        if any(x["id"] == f["id"] for x in self.fila) or (self.atual and self.atual["id"] == f["id"]):
            return False, f"já está na fila: {f['titulo']}"
        if not (isinstance(avatar, str) and avatar.startswith("https://") and len(avatar) < 600):
            avatar = ""
        agora = time.time()
        self.fila.append({**f, "por": por, "tid": tid, "avatar": avatar, "pedido_em": agora})
        db.execute("INSERT INTO pedidos(uid,vid,titulo,artista,por,tid,quando) VALUES(?,?,?,?,?,?,?)",
                   (self.uid, f["id"], f["titulo"], f.get("artista", ""), por, tid, agora))
        db.execute("DELETE FROM pedidos WHERE uid=? AND id <= (SELECT MAX(id) FROM pedidos WHERE uid=?) - 2000",
                   (self.uid, self.uid))
        if STREAMING:
            salvar_meta(f)
        db.execute("INSERT OR IGNORE INTO biblioteca(uid,vid) VALUES(?,?)", (self.uid, f["id"]))
        db.commit()
        self.acordar.set()
        bg(garantir_arquivo(f["id"]))  # baixa já, antes de chegar a vez
        return True, f"adicionada: {f['titulo']} (fila: {len(self.fila)})"

    async def pular(self):
        if self.tocando and self.evt_fim:
            self.evt_fim.set()
            self.say("⏭ Música pulada")
            return True
        return False

    def posicao(self):
        """Segundo atual da música (reportado pelo painel, estimado entre os avisos)."""
        if not self.atual or not self.tocando:
            return 0.0
        p = self.pos if self.pausado else self.pos + (time.time() - self.pos_t)
        return max(0.0, min(p, self.pos_dur)) if self.pos_dur else max(0.0, p)

    def dados_overlay(self):
        a = self.atual

        def mini(x):
            return {"id": x["id"], "titulo": x["titulo"], "artista": x.get("artista", ""),
                    "duracao": x.get("duracao", 0), "por": x.get("por", ""), "quando": x.get("quando", 0),
                    "tid": x.get("tid", "") if x.get("tid") not in ("painel", "autoplay") else "",
                    "avatar": x.get("avatar", ""), "pedido_em": x.get("pedido_em", 0),
                    "origem": origem_de(x.get("por", ""))}
        return {"titulo": a["titulo"] if a and self.tocando else "",   # campos originais
                "por": a["por"] if a else "",
                "atual": mini(a) if a else None, "tocando": self.tocando, "pausado": self.pausado,
                "cfg": self.ov,
                "seq": self.seq, "pos": round(self.posicao(), 2),
                "dur": self.pos_dur or (a.get("duracao", 0) if a else 0),
                "fila": [mini(x) for x in self.fila[:8]], "fila_total": len(self.fila),
                "historico": [mini(x) for x in list(self.historico)[:12]]}

    def dados_ovm(self):
        """Dados do overlay de músicas. 'inicia' = segundos até cada música da fila começar (a partir de agora)."""
        a = self.atual

        def mini(x):
            return {"id": x["id"], "titulo": x["titulo"], "artista": x.get("artista", ""),
                    "duracao": int(x.get("duracao") or 0), "por": solicitante_de(x),
                    "chave": f"{x['id']}:{x.get('pedido_em', 0)}"}
        dur = int(self.pos_dur or (a.get("duracao", 0) if a else 0) or 0)
        pos = self.posicao() if a else 0.0
        acum = max(0.0, dur - pos) if a else 0.0
        fila = []
        for x in self.fila[:10]:   # o overlay corta pelo "max" salvo; a prévia do painel pode pedir até 10
            m = mini(x)
            m["inicia"] = int(round(acum))
            fila.append(m)
            acum += m["duracao"]
        return {"atual": mini(a) if a else None, "tocando": self.tocando, "pausado": self.pausado,
                "seq": self.seq, "pos": round(pos, 2), "dur": dur, "fila": fila,
                "fila_total": len(self.fila), "cfg": self.ovm}

    def assinatura_ovm(self):
        """Muda só quando algo estrutural muda (não a cada segundo de música)."""
        d = self.dados_ovm()
        for x in d["fila"]:
            x.pop("inicia", None)
        d.pop("pos", None)
        return json.dumps(d, sort_keys=True, default=str)

    def pausa(self, forcar=None):
        if not self.tocando:
            return False
        self.pos, self.pos_t = self.posicao(), time.time()
        self.pausado = (not self.pausado) if forcar is None else forcar
        self.pos_t = time.time()
        self.say("⏸ Pausado" if self.pausado else "▶️ Continuando")
        return True

    def definir_volume(self, v):
        self.volume = max(0, min(130, int(v)))
        self.salvar()

    async def loop_player(self):
        ultimo = None
        while True:
            if not self.fila:
                if self.autoplay:
                    r = db.execute(
                        "SELECT f.* FROM biblioteca b JOIN faixas f ON f.id=b.vid "
                        "WHERE b.uid=? AND f.id != ? ORDER BY RANDOM() LIMIT 1",
                        (self.uid, ultimo or "")).fetchone()
                    if r and (STREAMING or Path(r["arquivo"]).exists()):
                        self.fila.append({"id": r["id"], "titulo": r["titulo"],
                                          "artista": r["artista"], "duracao": r["duracao"],
                                          "por": "autoplay", "tid": "autoplay",
                                          "avatar": "", "pedido_em": time.time()})
                if not self.fila:
                    self.acordar.clear()
                    await self.acordar.wait()
                    continue
            item = self.fila.pop(0)
            self.atual, self.pausado = item, False
            self.votos.clear()
            arquivo = await garantir_arquivo(item["id"])
            if arquivo:
                self.say(f"🎵 Tocando agora: {item['titulo']}")
                db.execute("UPDATE biblioteca SET tocadas=tocadas+1, ultima=? WHERE uid=? AND vid=?",
                           (time.time(), self.uid, item["id"]))
                db.execute("UPDATE faixas SET usada=? WHERE id=?", (time.time(), item["id"]))
                db.commit()
                ultimo = item["id"]
                self.seq += 1
                self.evt_fim = asyncio.Event()
                self.pos, self.pos_t, self.pos_dur = 0.0, time.time(), int(item.get("duracao") or 0)
                self.tocando = True
                await self.evt_fim.wait()   # o painel avisa quando termina
                self.tocando = False
                self.historico.appendleft({**item, "quando": time.time()})
            else:
                self.say(f"❌ Não consegui baixar: {item['titulo']} (veja os logs do servidor)")
            self.atual = None

    # ---- comandos do chat ----
    async def comando(self, texto, tid, nome, eh_mod, avatar=""):
        t = texto.strip()
        low = t.lower()
        c = self.cfg
        if low.startswith(("!song ", "!tocar ")):
            q = t.split(" ", 1)[1].strip()
            if not q:
                return
            self.say(f"📩 Pedido de {nome}: {q}")
            try:
                res = await buscar_auto(q, 1)
            except Exception as e:
                self.say(f"❌ Erro na busca: {str(e)[:100]}")
                return
            if not res:
                self.say("❌ Não encontrei a música" + (" na biblioteca" if SO_BIBLIOTECA else ""))
                return
            ok, msg = await self.enfileirar(res[0], por=nome, tid=tid, checar=not eh_mod, avatar=avatar)
            self.say(("✅ " if ok else "⛔ ") + msg)
        elif low == "!skip":
            if eh_mod:
                await self.pular()
        elif low == "!voteskip":
            if self.atual:
                self.votos.add(tid)
                self.say(f"🗳 Votos para pular: {len(self.votos)}/{c['votos_pular']}")
                if len(self.votos) >= c["votos_pular"]:
                    await self.pular()
        elif low == "!pause":
            if eh_mod:
                self.pausa(True)
        elif low in ("!play", "!continuar"):
            if eh_mod:
                self.pausa(False)
        elif low.startswith("!volume "):
            if eh_mod and low.split()[1].isdigit():
                self.definir_volume(low.split()[1])
        elif low == "!atual":
            self.say(f"🎵 Agora: {self.atual['titulo']}" if self.atual else "Nada tocando")
        elif low == "!remover":
            meus = [x for x in self.fila if x.get("tid") == tid]
            if meus:
                self.fila.remove(meus[-1])
                self.say(f"🗑 {nome} removeu: {meus[-1]['titulo']}")
        elif low == "!fila":
            self.say(f"📋 Fila ({len(self.fila)}): " + " | ".join(
                f"{i}. {x['titulo']}" for i, x in enumerate(self.fila[:8], 1)) if self.fila else "Fila vazia")

    def montar_cliente(self):
        cli = TikTokLiveClient(unique_id="@" + self.tiktok)

        async def on_connect(ev: ConnectEvent):
            self.conectado = True
            self.say(f"✅ Conectado na live de @{self.tiktok}")

        async def on_disconnect(ev: DisconnectEvent):
            self.conectado = False
            self.say("⚠️ Conexão com a live caiu")

        async def on_comment(ev: CommentEvent):
            try:
                self.push("chat", ev.user.nickname, ev.comment)
                tid = ev.user.unique_id
                eh_mod = (getattr(ev.user, "is_moderator", False)
                          or tid.lower() == self.tiktok.lower()
                          or tid.lower() in [m.lower().lstrip("@") for m in self.cfg["mods"]])
                await self.comando(ev.comment, tid, ev.user.nickname, eh_mod, avatar_de(ev.user))
            except Exception as e:
                self.say(f"Erro no comando: {e}")

        cli.add_listener(ConnectEvent, on_connect)
        cli.add_listener(DisconnectEvent, on_disconnect)
        async def on_gift(ev: GiftEvent):
            try:
                g = ev.gift
                if getattr(g, "streakable", False) and getattr(ev, "streaking", False):
                    return  # só registra quando a sequência de presentes termina
                self.push("gift", ev.user.nickname, g.name, getattr(ev, "repeat_count", 1) or 1)
            except Exception as e:
                print(f"[{self.nome}] (gift) {e}")

        async def on_follow(ev: FollowEvent):
            self.push("follow", ev.user.nickname)

        async def on_join(ev: JoinEvent):
            agora = time.time()
            if agora - self._ult_join >= 0.7:   # evita inundar em lives grandes
                self._ult_join = agora
                self.push("join", ev.user.nickname)

        cli.add_listener(CommentEvent, on_comment)
        cli.add_listener(GiftEvent, on_gift)
        cli.add_listener(FollowEvent, on_follow)
        cli.add_listener(JoinEvent, on_join)
        return cli

    async def loop_tiktok(self):
        while True:
            cli = self.montar_cliente()
            try:
                r = await cli.start()
                if r is not None and asyncio.isfuture(r):
                    await r
            except asyncio.CancelledError:
                try:
                    await cli.disconnect()
                except Exception:
                    pass
                raise
            except Exception as e:
                print(f"[{self.nome}] (tiktok) {type(e).__name__}: {e}")
            self.conectado = False
            self.say("Aguardando a live começar... (nova tentativa em 45s)")
            await asyncio.sleep(45)

    # ---- importação (Spotify) ----
    @staticmethod
    def parse_importacao(texto):
        linhas = [l for l in texto.splitlines() if l.strip()]
        if not linhas:
            return []
        if "track name" in linhas[0].lower():  # CSV do Exportify
            rd = csv.DictReader(io.StringIO(texto))
            ik = next(k for k in rd.fieldnames if "track name" in k.lower())
            ia = next((k for k in rd.fieldnames if "artist" in k.lower()), None)
            out = []
            for r in rd:
                art = re.split(r"[;,]", r.get(ia) or "")[0].strip() if ia else ""
                out.append(f"{art} {r[ik]}".strip())
            return out
        return [l.strip() for l in linhas]

    async def importar(self, qs):
        self.imp = {"rodando": True, "total": len(qs), "feitas": 0, "falhas": 0}
        self.say(f"📥 Importando {len(qs)} músicas...")
        for q in qs:
            try:
                res = await buscar(q, 1)
                if res and STREAMING:
                    salvar_meta(res[0])
                if not res or (not STREAMING and not await garantir_arquivo(res[0]["id"])):
                    raise RuntimeError("falhou")
                db.execute("INSERT OR IGNORE INTO biblioteca(uid,vid) VALUES(?,?)",
                           (self.uid, res[0]["id"]))
                db.commit()
            except Exception:
                self.imp["falhas"] += 1
            self.imp["feitas"] += 1
        self.imp["rodando"] = False
        self.say(f"📥 Importação concluída ({self.imp['falhas']} falhas)")


def criar_sessao(row):
    s = Sessao(row)
    SESSOES[s.uid] = s
    s.iniciar()
    return s


# ---------------- autenticação ----------------
def hash_senha(senha, sal):
    return hashlib.pbkdf2_hmac("sha256", senha.encode(), bytes.fromhex(sal), 200_000).hex()


def erro(msg, code=400):
    return JSONResponse({"erro": msg}, status_code=code)


def entrar_como(uid):
    token = secrets.token_urlsafe(32)
    db.execute("INSERT INTO sessoes(token,uid,criada) VALUES(?,?,?)", (token, uid, time.time()))
    db.commit()
    resp = JSONResponse({"ok": True})
    resp.set_cookie("sessao", token, httponly=True, samesite="lax", max_age=30 * 86400)
    return resp


def auth(req: Request) -> Sessao:
    tok = req.cookies.get("sessao")
    r = db.execute("SELECT uid FROM sessoes WHERE token=? AND criada>?", (tok, time.time() - 30 * 86400)).fetchone() if tok else None
    s = SESSOES.get(r["uid"]) if r else None
    if not s:
        raise HTTPException(401)
    return s


app = FastAPI()


@app.post("/api/registrar")
async def api_registrar(req: Request):
    d = await req.json()
    nome = str(d.get("nome", "")).strip().lower()
    senha = str(d.get("senha", ""))
    if os.getenv("BOT_LOCAL_ANDROID", "") != "1" and not hmac.compare_digest(str(d.get("convite", "")).encode(), CONVITE.encode()):
        return erro("código de convite inválido")
    if not re.fullmatch(r"[a-z0-9_.-]{3,20}", nome):
        return erro("nome de usuário: 3 a 20 letras, números, . _ -")
    if len(senha) < 6:
        return erro("a senha precisa ter pelo menos 6 caracteres")
    if db.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0] >= MAX_USUARIOS:
        return erro("limite de usuários atingido")
    if db.execute("SELECT 1 FROM usuarios WHERE nome=?", (nome,)).fetchone():
        return erro("esse nome já existe")
    sal = secrets.token_hex(16)
    h = await asyncio.to_thread(hash_senha, senha, sal)
    cur = db.execute(
        "INSERT INTO usuarios(nome,senha,sal,cfg,overlay,criado) VALUES(?,?,?,?,?,?)",
        (nome, h, sal, json.dumps(CFG_PADRAO), secrets.token_urlsafe(12), time.time()))
    db.commit()
    criar_sessao(db.execute("SELECT * FROM usuarios WHERE id=?", (cur.lastrowid,)).fetchone())
    return entrar_como(cur.lastrowid)


@app.post("/api/entrar")
async def api_entrar(req: Request):
    d = await req.json()
    r = db.execute("SELECT * FROM usuarios WHERE nome=?", (str(d.get("nome", "")).strip().lower(),)).fetchone()
    await asyncio.sleep(0.8)  # atrapalha tentativas em massa
    if not r:
        return erro("usuário ou senha incorretos", 401)
    h = await asyncio.to_thread(hash_senha, str(d.get("senha", "")), r["sal"])
    if not hmac.compare_digest(h, r["senha"]):
        return erro("usuário ou senha incorretos", 401)
    return entrar_como(r["id"])


@app.post("/api/sair")
async def api_sair(req: Request):
    tok = req.cookies.get("sessao")
    if tok:
        db.execute("DELETE FROM sessoes WHERE token=?", (tok,))
        db.commit()
    return {"ok": True}


@app.get("/api/eu")
async def api_eu(req: Request):
    s = auth(req)
    return {"nome": s.nome, "tiktok": s.tiktok, "cfg": s.cfg, "overlay": s.overlay, "ov": s.ov, "ovm": s.ovm}


@app.post("/api/config")
async def api_config(req: Request):
    s = auth(req)
    d = await req.json()

    def num(k, lo, hi):
        try:
            s.cfg[k] = max(lo, min(hi, int(d.get(k, s.cfg[k]))))
        except (TypeError, ValueError):
            pass

    num("max_duracao", 0, 3600)
    num("max_por_pessoa", 1, 20)
    num("votos_pular", 1, 50)
    for k in ("bloqueadas", "mods"):
        v = d.get(k, s.cfg[k])
        if isinstance(v, str):
            v = v.split(",")
        s.cfg[k] = [x.strip() for x in v if str(x).strip()][:50]
    novo = str(d.get("tiktok", s.tiktok)).strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_.]{0,40}", novo):
        return erro("usuário do TikTok inválido")
    mudou = novo != s.tiktok
    s.tiktok = novo
    s.salvar()
    if mudou:
        s.reiniciar_tiktok()
    return {"ok": True, "msg": "salvo"}


# ---------------- páginas ----------------
@app.get("/", response_class=HTMLResponse)
async def painel():
    return HTML_PATH.read_text(encoding="utf-8")


@app.get("/chat", response_class=HTMLResponse)
async def pagina_chat():
    return CHAT_PATH.read_text(encoding="utf-8")


@app.get("/api/eventos")
async def api_eventos(req: Request, desde: int = 0, t: str = ""):
    if t:  # acesso por token (overlay), sem login
        s = next((x for x in SESSOES.values() if x.overlay == t), None)
        if not s:
            return Response(status_code=404)
    else:
        s = auth(req)
    return {"ultimo": s.ev_id, "eventos": [e for e in s.eventos if e["id"] > desde][-120:]}


def _overlay_html():
    return HTMLResponse(OVERLAY_PATH.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})


@app.get("/overlay/{token}", response_class=HTMLResponse)
async def overlay(token: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        return Response(status_code=404)
    return _overlay_html()


@app.get("/overlay/{token}/{vista}", response_class=HTMLResponse)
async def overlay_vista(token: str, vista: str):
    """Mesmo token das outras páginas: /overlay/TOKEN/fila e /overlay/TOKEN/historico."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", token) or vista not in ("agora", "fila", "historico", "unificado", "musicas"):
        return Response(status_code=404)
    if vista == "musicas":
        return HTMLResponse(MUSICAS_PATH.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})
    if vista == "unificado":
        return HTMLResponse(UNIFICADO_PATH.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})
    return _overlay_html()


@app.get("/api/overlay/{token}")
async def api_overlay(token: str):
    for s in SESSOES.values():
        if s.overlay == token:
            return JSONResponse(s.dados_overlay(), headers={"Cache-Control": "no-store"})
    return Response(status_code=404)


def sessao_por_token(token):
    """Cada token de overlay pertence a UM usuário; só ele (e quem tem o link) vê a fila dele."""
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", token):
        return None
    for x in SESSOES.values():
        if x.overlay and hmac.compare_digest(x.overlay.encode(), token.encode()):
            return x
    return None


async def fluxo_ovm(s, desconectou):
    """Server-Sent Events do overlay de músicas: só envia quando algo muda (ou a posição sai do previsto)."""
    sig, ult_t, ult_pos, ult_run = None, 0.0, 0.0, False
    ult_envio = 0.0
    while not await desconectou():
        agora = time.time()
        nova = s.assinatura_ovm()
        pos = s.posicao()
        previsto = ult_pos + (agora - ult_t) if ult_run else ult_pos
        if nova != sig or abs(pos - previsto) > 1.5 or agora - ult_envio > 20:
            d = s.dados_ovm()
            sig, ult_t, ult_pos, ult_run, ult_envio = nova, agora, d["pos"], bool(d["tocando"] and not d["pausado"]), agora
            yield f"data: {json.dumps(d, ensure_ascii=False)}\n\n"
        elif agora - ult_envio > 12:
            ult_envio = agora
            yield ": ping\n\n"
        await asyncio.sleep(0.4)


@app.get("/api/overlay-musicas/{token}")
async def api_ovm_snapshot(token: str):
    s = sessao_por_token(token)
    if not s:
        return Response(status_code=404)
    return JSONResponse(s.dados_ovm(), headers={"Cache-Control": "no-store"})


@app.get("/api/overlay-musicas/{token}/stream")
async def api_ovm_stream(token: str, req: Request):
    s = sessao_por_token(token)
    if not s:
        return Response(status_code=404)
    return StreamingResponse(fluxo_ovm(s, req.is_disconnected), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no",
                                      "Connection": "keep-alive"})


@app.get("/api/overlay-musicas-config")
async def api_ovm_get(req: Request):
    return auth(req).ovm


@app.post("/api/overlay-musicas-config")
async def api_ovm_set(req: Request):
    s = auth(req)
    try:
        d = await req.json()
    except Exception:
        return erro("dados inválidos")
    s.ovm = limpar_ovm(d, s.ovm)
    db.execute("UPDATE usuarios SET ovmcfg=? WHERE id=?", (json.dumps(s.ovm), s.uid))
    db.commit()
    return {"ok": True, "cfg": s.ovm, "msg": "overlay de músicas salvo"}


@app.get("/api/overlay-config")
async def api_ov_get(req: Request):
    return auth(req).ov


@app.post("/api/overlay-config")
async def api_ov_set(req: Request):
    s = auth(req)
    try:
        d = await req.json()
    except Exception:
        return erro("dados inválidos")
    s.ov = limpar_ov(d, s.ov)
    db.execute("UPDATE usuarios SET ovcfg=? WHERE id=?", (json.dumps(s.ov), s.uid))
    db.commit()
    return {"ok": True, "cfg": s.ov, "msg": "overlay salvo"}


@app.get("/api/stats")
async def api_stats(req: Request):
    s = auth(req)
    q = lambda sql, *a: [dict(r) for r in db.execute(sql, a).fetchall()]
    tot = db.execute("SELECT COALESCE(SUM(tocadas),0) t, COUNT(*) n, COALESCE(SUM(favorita),0) f "
                     "FROM biblioteca WHERE uid=?", (s.uid,)).fetchone()
    return {"tocadas": tot["t"], "biblioteca": tot["n"], "favoritas": tot["f"],
            "top": q("SELECT f.id,f.titulo,f.artista,b.tocadas FROM biblioteca b JOIN faixas f ON f.id=b.vid "
                     "WHERE b.uid=? AND b.tocadas>0 ORDER BY b.tocadas DESC, b.ultima DESC LIMIT 5", s.uid),
            "pedidores": q("SELECT MAX(por) por, COUNT(*) n FROM pedidos WHERE uid=? "
                           "AND tid NOT IN ('painel','autoplay') GROUP BY tid ORDER BY n DESC LIMIT 5", s.uid),
            "recentes": q("SELECT titulo,artista,por,tid,quando FROM pedidos WHERE uid=? ORDER BY id DESC LIMIT 8", s.uid)}


@app.post("/api/posicao")
async def api_posicao(req: Request):
    """O painel (que toca o áudio) informa em que segundo a música está."""
    s = auth(req)
    d = await req.json()
    if d.get("seq") == s.seq and s.tocando:
        try:
            s.pos, s.pos_t = max(0.0, float(d.get("pos", 0))), time.time()
            if d.get("dur"):
                s.pos_dur = int(float(d["dur"]))
        except (TypeError, ValueError):
            pass
    return {"ok": True}


@app.get("/audio/{vid}")
async def audio(vid: str, req: Request):
    auth(req)
    if STREAMING:
        try:
            url, h = await resolver(vid)
        except Exception:
            return Response(status_code=502)
        headers = {k: v for k, v in h.items() if k.lower() in ("user-agent", "accept", "accept-language")}
        if req.headers.get("range"):
            headers["Range"] = req.headers["range"]
        cli = httpx.AsyncClient(timeout=30, follow_redirects=True)
        up = await cli.send(cli.build_request("GET", url, headers=headers), stream=True)
        if up.status_code >= 400:
            await up.aclose()
            await cli.aclose()
            _urls.pop(vid, None)
            return Response(status_code=502)
        hdr = {k: up.headers[k] for k in ("content-type", "content-length", "content-range", "accept-ranges")
               if k in up.headers}

        async def gen():
            try:
                async for chunk in up.aiter_raw():
                    yield chunk
            finally:
                await up.aclose()
                await cli.aclose()

        return StreamingResponse(gen(), status_code=up.status_code, headers=hdr)
    r = db.execute("SELECT arquivo FROM faixas WHERE id=?", (vid,)).fetchone()
    if not r or not r["arquivo"] or not Path(r["arquivo"]).exists():
        return Response(status_code=404)
    return FileResponse(r["arquivo"], media_type=MIMES.get(Path(r["arquivo"]).suffix.lower(),
                                                           "application/octet-stream"))


# ---------------- API do painel ----------------
@app.get("/api/estado")
async def api_estado(req: Request):
    return auth(req).estado()


@app.post("/api/buscar")
async def api_buscar(req: Request):
    auth(req)
    d = await req.json()
    try:
        return await buscar_auto(str(d.get("q", "")).strip(), 6)
    except Exception:
        return []


@app.post("/api/add")
async def api_add(req: Request):
    s = auth(req)
    try:
        d = await req.json()
        f = {"id": str(d["id"]), "titulo": str(d.get("titulo", ""))[:200],
             "artista": str(d.get("artista", ""))[:100], "duracao": int(d.get("duracao") or 0)}
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", f["id"]):
            raise ValueError
    except Exception:
        return erro("música inválida")
    ok, msg = await s.enfileirar(f)
    s.say(("✅ " if ok else "⛔ ") + msg)
    return {"ok": ok, "msg": msg}


@app.post("/api/pular")
async def api_pular(req: Request):
    return {"ok": await auth(req).pular()}


@app.post("/api/pausa")
async def api_pausa(req: Request):
    return {"ok": auth(req).pausa()}


@app.post("/api/volume")
async def api_volume(req: Request):
    s = auth(req)
    s.definir_volume((await req.json())["v"])
    return {"ok": True}


@app.post("/api/autoplay")
async def api_autoplay(req: Request):
    s = auth(req)
    s.autoplay = bool((await req.json())["on"])
    s.salvar()
    s.acordar.set()
    return {"ok": True}


@app.post("/api/remover")
async def api_remover(req: Request):
    s = auth(req)
    try:
        i = int((await req.json())["i"])
    except Exception:
        return erro("posição inválida")
    if 0 <= i < len(s.fila):
        s.fila.pop(i)
    return {"ok": True}


@app.post("/api/limpar")
async def api_limpar(req: Request):
    auth(req).fila.clear()
    return {"ok": True}


@app.post("/api/terminou")
async def api_terminou(req: Request):
    s = auth(req)
    d = await req.json()
    if d.get("seq") == s.seq and s.evt_fim:
        s.evt_fim.set()
    return {"ok": True}


@app.get("/api/biblioteca")
async def api_biblioteca(req: Request, filtro: str = "historico", q: str = ""):
    s = auth(req)
    sql = ("SELECT f.id,f.titulo,f.artista,f.duracao,b.tocadas,b.favorita FROM biblioteca b "
           "JOIN faixas f ON f.id=b.vid WHERE b.uid=?")
    args = [s.uid]
    ordem = " ORDER BY f.titulo"
    if filtro == "historico":
        sql += " AND b.tocadas>0"
        ordem = " ORDER BY b.ultima DESC"
    elif filtro == "favoritas":
        sql += " AND b.favorita=1"
    if q:
        sql += " AND (f.titulo LIKE ? OR f.artista LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    return [dict(r) for r in db.execute(sql + ordem + " LIMIT 300", args).fetchall()]


@app.post("/api/usar")
async def api_usar(req: Request):
    s = auth(req)
    r = db.execute("SELECT * FROM faixas WHERE id=?", ((await req.json())["id"],)).fetchone()
    if not r:
        return {"ok": False}
    ok, msg = await s.enfileirar({"id": r["id"], "titulo": r["titulo"],
                                  "artista": r["artista"], "duracao": r["duracao"]})
    return {"ok": ok, "msg": msg}


@app.post("/api/fav")
async def api_fav(req: Request):
    s = auth(req)
    db.execute("UPDATE biblioteca SET favorita = 1 - favorita WHERE uid=? AND vid=?",
               (s.uid, (await req.json())["id"]))
    db.commit()
    return {"ok": True}


@app.post("/api/importar")
async def api_importar(req: Request):
    s = auth(req)
    d = await req.json()
    if SO_BIBLIOTECA:
        return {"ok": False, "msg": "downloads desativados neste servidor (modo só biblioteca)"}
    if s.imp["rodando"]:
        return {"ok": False, "msg": "já existe uma importação em andamento"}
    try:
        qs = s.parse_importacao(d.get("texto", ""))
    except Exception:
        return {"ok": False, "msg": "não consegui ler o arquivo"}
    if not qs:
        return {"ok": False, "msg": "lista vazia"}
    qs = qs[:500]
    bg(s.importar(qs))
    return {"ok": True, "msg": f"importando {len(qs)} músicas"}


async def main():
    MUSIC_DIR.mkdir(exist_ok=True)
    for r in db.execute("SELECT * FROM usuarios").fetchall():
        criar_sessao(r)
    if STREAMING:
        print("Modo streaming: nenhum arquivo de áudio será salvo.")
    if SO_BIBLIOTECA:
        print("Modo só biblioteca: nenhum download novo será feito.")
    if CONVITE == "troque-este-codigo":
        print("⚠️  Defina BOT_CONVITE com um código seu antes de abrir o bot para outras pessoas.")
    print(f"Painel: http://localhost:{PORT}   ({len(SESSOES)} usuário(s) carregado(s))")
    await uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT, log_level="warning")).serve()


_android_thread = None
_android_server = None

def android_start():
    """Inicia o backend local em uma thread dedicada, chamado pelo serviço Android."""
    global _android_thread
    import threading
    if _android_thread and _android_thread.is_alive():
        return
    def runner():
        try:
            asyncio.run(main())
        except Exception as exc:
            print(f"[Android] Falha ao iniciar backend: {exc}")
    _android_thread = threading.Thread(target=runner, name="BotMusicaLocal", daemon=True)
    _android_thread.start()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

