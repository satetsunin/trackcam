package com.trackcam.app

import android.annotation.SuppressLint
import android.content.Intent
import android.graphics.Bitmap
import android.os.Build
import android.os.Bundle
import android.view.View
import android.webkit.CookieManager
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.ProgressBar
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity

/**
 * Navegador integrado (WebView) para autenticarse con Cloudflare Access
 * SIN salir de la app. Carga la URL base del servidor; si Cloudflare Access
 * está activo muestra el SSO (login con Google/email); al completarlo, la
 * cookie de sesión (CF_Authorization) queda en el CookieManager de Android
 * y la app puede usarla en las peticiones HTTP.
 *
 * Resultado: RESULT_OK si hay sesión CF (o el servidor responde sin login),
 * RESULT_CANCELED si el usuario aborta.
 */
class AuthWebViewActivity : AppCompatActivity() {

    companion object {
        const val EXTRA_URL = "extra_url"
        const val EXTRA_HOST = "extra_host"
    }

    private lateinit var webView: WebView
    private lateinit var progress: ProgressBar
    private lateinit var tvEstado: TextView
    private var hostServidor = ""
    private var autenticado = false

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_auth_webview)

        webView = findViewById(R.id.wvAuth)
        progress = findViewById(R.id.pbAuth)
        tvEstado = findViewById(R.id.tvAuthEstado)

        val url = intent.getStringExtra(EXTRA_URL) ?: TrackPrefs.DEFAULT_BASE_URL
        hostServidor = intent.getStringExtra(EXTRA_HOST)
            ?: url.removePrefix("https://").removePrefix("http://").substringBefore('/')

        webView.settings.javaScriptEnabled = true
        webView.settings.domStorageEnabled = true
        webView.settings.loadWithOverviewMode = true
        webView.settings.useWideViewPort = true

        // Compartir cookies con el resto de la app (OkHttp)
        CookieManager.getInstance().setAcceptCookie(true)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
            CookieManager.getInstance().setAcceptThirdPartyCookies(webView, true)
        }

        webView.webViewClient = object : WebViewClient() {
            override fun onPageStarted(view: WebView?, url: String?, favicon: Bitmap?) {
                progress.visibility = View.VISIBLE
                tvEstado.text = getString(R.string.auth_cargando, url.orEmpty())
            }

            override fun onPageFinished(view: WebView?, url: String?) {
                progress.visibility = View.GONE
                comprobarAutenticacion(url.orEmpty())
            }

            override fun shouldOverrideUrlLoading(
                view: WebView?, request: WebResourceRequest?
            ): Boolean {
                val u = request?.url?.toString().orEmpty()
                // Si volvemos al host del servidor tras el SSO, casi listo
                if (u.contains(hostServidor)) comprobarAutenticacion(u)
                return false
            }
        }

        tvEstado.text = getString(R.string.auth_iniciando)
        webView.loadUrl(url)
    }

    /** Detecta la cookie de Cloudflare o que el servidor responde sin login. */
    private fun comprobarAutenticacion(url: String) {
        if (autenticado) return
        val cookies = CookieManager.getInstance().getCookie(hostServidor).orEmpty()
        val tieneCf = cookies.contains("CF_Authorization") ||
            cookies.contains("cf_authorization") ||
            cookies.contains("__cf_access") ||
            cookies.contains("CF_Authorization=")
        if (tieneCf && url.contains(hostServidor)) {
            autenticado = true
            TrackPrefs.setCfAuthed(this, true)
            Toast.makeText(this, R.string.auth_ok, Toast.LENGTH_LONG).show()
            setResult(RESULT_OK)
            finish()
        }
    }

    /** Botón "ya me he autenticado / continuar" por si la detección falla. */
    fun onContinuar(v: View) {
        val cookies = CookieManager.getInstance().getCookie(hostServidor).orEmpty()
        if (cookies.isNotEmpty() || webView.url?.contains(hostServidor) == true) {
            TrackPrefs.setCfAuthed(this, true)
            setResult(RESULT_OK)
            finish()
        } else {
            Toast.makeText(this, R.string.auth_sin_cookie, Toast.LENGTH_LONG).show()
        }
    }

    fun onCancelar(v: View) {
        setResult(RESULT_CANCELED)
        finish()
    }

    override fun onBackPressed() {
        if (webView.canGoBack()) webView.goBack()
        else {
            setResult(RESULT_CANCELED)
            finish()
        }
    }
}
