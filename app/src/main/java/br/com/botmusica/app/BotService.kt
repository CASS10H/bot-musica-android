package br.com.botmusica.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.os.Build
import android.os.IBinder
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.io.File

class BotService : Service() {
    override fun onCreate() {
        super.onCreate()
        createChannel()
        startForeground(42, notification("Iniciando o mecanismo local…"))
        Thread {
            try {
                copyAssets("bot-musica", File(filesDir, "bot-musica"))
                if (!Python.isStarted()) Python.start(AndroidPlatform(this))
                Python.getInstance()
                    .getModule("app")
                    .callAttr("start", File(filesDir, "bot-musica").absolutePath)
                updateNotification("Ativo neste celular")
            } catch (e: Exception) {
                updateNotification("Erro ao iniciar: ${e.message ?: "verifique os arquivos"}")
                android.util.Log.e("BotMusica", "Falha ao iniciar Python", e)
            }
        }.start()
    }

    private fun copyAssets(assetPath: String, destination: File) {
        destination.mkdirs()
        val children = assets.list(assetPath) ?: emptyArray()
        for (child in children) {
            val childAsset = "$assetPath/$child"
            val out = File(destination, child)
            val nested = assets.list(childAsset) ?: emptyArray()
            if (nested.isNotEmpty()) {
                copyAssets(childAsset, out)
            } else {
                assets.open(childAsset).use { input ->
                    out.outputStream().use { output -> input.copyTo(output) }
                }
            }
        }
    }

    private fun createChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                "bot_musica_local", "Bot Música em execução",
                NotificationManager.IMPORTANCE_LOW
            )
            getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
        }
    }

    private fun notification(message: String): Notification {
        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
            Notification.Builder(this, "bot_musica_local")
        else Notification.Builder(this)
        return builder.setContentTitle("Bot Música")
            .setContentText(message)
            .setSmallIcon(android.R.drawable.ic_media_play)
            .setOngoing(true)
            .build()
    }

    private fun updateNotification(message: String) {
        getSystemService(NotificationManager::class.java).notify(42, notification(message))
    }

    override fun onBind(intent: Intent?): IBinder? = null
}
