# Bot Música Android (local)

Backend Python + painel HTML rodando **inteiramente no celular** via Chaquopy.
Sem VPS.

## Como gerar o APK automaticamente

1. Faça push deste repositório para o GitHub
2. Vá na aba **Actions**
3. Aguarde o workflow "Build APK" terminar
4. Baixe o artefato **bot-musica-debug**
5. Instale o `app-debug.apk` no celular

O backend escuta em `http://127.0.0.1:8000` (apenas localhost).
