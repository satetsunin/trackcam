package com.trackcam.app

import android.annotation.SuppressLint
import android.content.Intent
import android.os.Bundle
import android.os.Message
import android.view.View
import android.view.ViewGroup
import android.webkit.CookieManager
import android.webkit.GeolocationPermissions
import android.webkit.PermissionRequest
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.ProgressBar
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import java.util.Locale

/**
 * Navegador interno para autenticarse SIN salir de la app (patrón satetsunin):
 * - Carga la URL real del servidor (la web de TrackCam).
 * - Si Cloudflare Access protege el túnel, su SSO se muestra AQUÍ dentro y el
 *   usuario se loguea viendo la página de verdad.
 * - domStorage + cookies activados → la sesión de Cloudflare queda guardada
 *   en el WebView y en el CookieManager (lo usa TrackService en cada POST).
 * - Navegación estricta interna: popups (target=_blank) se reencaminan al
 *   mismo WebView; nada abre el navegador del móvil.
 * - Botón "✓ He terminado": el usuario lo pulsa cuando ve que ya está dentro.
 */
class AuthWebViewActivity : AppCompatActivity() {

    companion object {
        const val EXTRA_URL = "extra_url"
        const val EXTRA_USUARIO = "extra_usuario"
    }

    private lateinit var web: WebView
    private lateinit var progress: ProgressBar
    private var url = ""
    private var hostProyecto = ""
    private var usuario = ""

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_auth_webview)

        web = findViewById(R.id.wvAuth)
        progress = findViewById(R.id.pbAuth)
        val tvEstado = findViewById<TextView>(R.id.tvAuthEstado)
        val btnOk = findViewById<View>(R.id.btnOk)
        val btnCancel = findViewById<View>(R.id.btnCancel)

        url = intent.getStringExtra(EXTRA_URL) ?: TrackPrefs.DEFAULT_BASE_URL
        usuario = intent.getStringExtra(EXTRA_USUARIO).orEmpty()

        try {
            val h = android.net.Uri.parse(url).host
            if (h != null) hostProyecto = h.lowercase(Locale.US)
        } catch (e: Exception) { /* sin host */ }

        tvEstado.text = getString(R.string.auth_iniciando)

        val s: WebSettings = web.settings
        s.javaScriptEnabled = true
        s.domStorageEnabled = true // ← clave: guarda logins/localStorage de Cloudflare
        s.allowFileAccess = false
        s.mediaPlaybackRequiresUserGesture = true
        s.loadWithOverviewMode = true
        s.useWideViewPort = true

        // Cookies compartidas con el resto de la app (OkHttp del TrackService)
        CookieManager.getInstance().setAcceptCookie(true)

        web.webViewClient = object : WebViewClient() {
            // Solo http/https navegan dentro. Cualquier otro esquema se bloquea.
            private fun esWeb(u: String?): Boolean {
                val lo = u?.lowercase(Locale.US).orEmpty()
                return lo.startsWith("http://") || lo.startsWith("https://")
            }

            override fun shouldOverrideUrlLoading(view: WebView?, urlL: String?): Boolean =
                !esWeb(urlL)

            override fun shouldOverrideUrlLoading(
                view: WebView?, request: WebResourceRequest?
            ): Boolean = !esWeb(request?.url?.toString())

            override fun onPageFinished(view: WebView?, pageUrl: String?) {
                prefillUsuarioSiProcede(pageUrl)
            }
        }

        web.webChromeClient = object : WebChromeClient() {
            override fun onProgressChanged(view: WebView?, newProgress: Int) {
                if (newProgress >= 100) progress.visibility = View.GONE
                else {
                    progress.visibility = View.VISIBLE
                    progress.progress = newProgress
                }
            }

            // Popups (target=_blank, window.open) → al WebView principal
            override fun onCreateWindow(
                view: WebView?, isDialog: Boolean, isUserGesture: Boolean,
                resultMsg: Message?
            ): Boolean {
                val child = WebView(this@AuthWebViewActivity)
                val transport = resultMsg?.obj as? WebView.WebViewTransport
                transport?.webView = child
                resultMsg?.sendToTarget()
                child.webViewClient = object : WebViewClient() {
                    override fun shouldOverrideUrlLoading(
                        v: WebView?, request: WebResourceRequest?
                    ): Boolean {
                        val u = request?.url?.toString()
                        if (u != null && (u.startsWith("http://") || u.startsWith("https://"))) {
                            view?.loadUrl(u)
                        }
                        return true
                    }
                    override fun shouldOverrideUrlLoading(v: WebView?, u: String?): Boolean {
                        if (u != null && (u.startsWith("http://") || u.startsWith("https://"))) {
                            view?.loadUrl(u)
                        }
                        return true
                    }
                }
                return true
            }

            override fun onPermissionRequest(request: PermissionRequest?) {
                if (request?.resources?.isNotEmpty() == true) request.grant(request.resources)
                else request?.deny()
            }

            override fun onGeolocationPermissionsShowPrompt(
                origin: String?, callback: GeolocationPermissions.Callback?
            ) {
                callback?.invoke(origin, true, false)
            }
        }

        btnOk.setOnClickListener {
            // El usuario pulsa esto cuando YA ve el contenido (login hecho)
            TrackPrefs.setCfAuthed(this, true)
            setResult(RESULT_OK)
            finish()
        }
        btnCancel.setOnClickListener {
            setResult(RESULT_CANCELED)
            finish()
        }

        web.loadUrl(url)
    }

    /** Si el proyecto define "usuario", autocompleta el campo de usuario del
     *  formulario de login (nunca guarda contraseñas). */
    private fun prefillUsuarioSiProcede(pageUrl: String?) {
        if (usuario.isEmpty() || hostProyecto.isEmpty()) return
        val u = pageUrl ?: return
        if (!u.startsWith("http://") && !u.startsWith("https://")) return
        try {
            val host = android.net.Uri.parse(u).host?.lowercase(Locale.US) ?: return
            val mismoDominio = host == hostProyecto || host.endsWith("." + hostProyecto)
            if (!mismoDominio) return
        } catch (e: Exception) { return }
        val valJs = org.json.JSONObject.quote(usuario)
        val js = buildString {
            append("(function(){")
            append("var pw=document.querySelector('input[type=password]');")
            append("if(!pw)return;")
            append("var scope=pw.form||document;")
            append("var us=scope.querySelector('input[type=text],input[type=email],")
            append("input[autocomplete=username],input[name*=user i],input[id*=user i],")
            append("input[name*=login i],input[id*=login i]');")
            append("if(!us||us.value)return;")
            append("var proto=Object.getPrototypeOf(us);")
            append("if(proto&&proto.set){var setter=proto.set;proto.set.call(us," + valJs + ");}")
            append("else{us.value=" + valJs + ";}")
            append("us.dispatchEvent(new Event('input',{bubbles:true}));")
            append("us.dispatchEvent(new Event('change',{bubbles:true}));")
            append("})();")
        }
        web.evaluateJavascript(js, null)
    }

    override fun onBackPressed() {
        if (web.canGoBack()) web.goBack()
        else {
            setResult(RESULT_CANCELED)
            finish()
        }
    }

    override fun onDestroy() {
        try {
            val parent = web.parent as? ViewGroup
            parent?.removeView(web)
            web.stopLoading()
            web.destroy()
        } catch (e: Exception) { /* ya destruido */ }
        super.onDestroy()
    }
}
