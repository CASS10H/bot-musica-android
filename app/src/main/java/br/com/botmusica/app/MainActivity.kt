package br.com.botmusica.app

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.webkit.CookieManager
import android.webkit.WebChromeClient
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import android.widget.Toast
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat

class MainActivity : Activity() {
    private lateinit var web: WebView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        askNotificationPermission()
        val service = Intent(this, BotService::class.java)
        ContextCompat.startForegroundService(this, service)

        web = WebView(this)
        web.settings.javaScriptEnabled = true
        web.settings.domStorageEnabled = true
        web.settings.mediaPlaybackRequiresUserGesture = false
        web.settings.allowFileAccess = false
        web.settings.allowContentAccess = false
        web.webChromeClient = WebChromeClient()
        web.webViewClient = object : WebViewClient() {
            override fun onReceivedError(
                view: WebView?, request: android.webkit.WebResourceRequest?,
                error: android.webkit.WebResourceError?
            ) {
                if (request?.isForMainFrame == true) {
                    Toast.makeText(this@MainActivity, "Iniciando o bot local…", Toast.LENGTH_SHORT).show()
                    web.postDelayed({ web.loadUrl("http://127.0.0.1:8000/") }, 1800)
                }
            }
        }
        CookieManager.getInstance().setAcceptCookie(true)
        setContentView(FrameLayout(this).apply { addView(web) })
        web.loadUrl("http://127.0.0.1:8000/")
    }

    private fun askNotificationPermission() {
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
            != PackageManager.PERMISSION_GRANTED) {
            ActivityCompat.requestPermissions(this, arrayOf(Manifest.permission.POST_NOTIFICATIONS), 7)
        }
    }

    @Deprecated("Deprecated in Android")
    override fun onBackPressed() {
        if (::web.isInitialized && web.canGoBack()) web.goBack() else super.onBackPressed()
    }

    override fun onDestroy() {
        // O serviço continua ativo mesmo quando a tela do painel é fechada.
        super.onDestroy()
    }
}
