package com.trackcam.app

import android.content.Intent
import android.os.Bundle
import android.view.View
import android.view.inputmethod.EditorInfo
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * Pantalla de login (Fase 5). Es el launcher de la app:
 *  - Pide URL BASE del servidor + usuario + contraseña.
 *  - POST {username,password} a <base>/api/login → {token, usuario}.
 *  - Guarda la sesión y pasa a MainActivity.
 * Si ya hay un token guardado (y no venimos de un 401) salta directo a Main.
 */
class LoginActivity : AppCompatActivity() {

    companion object {
        const val EXTRA_MOTIVO = "extra_motivo"
        const val MOTIVO_SESION_EXPIRADA = "sesion_expirada"

        private const val RC_CF_AUTH = 2001

        // Códigos de resultado del intento de login
        private const val OUT_RED = -1     // fallo de red / timeout
        private const val OUT_PARSE = -2   // respuesta inesperada del servidor
        private const val OUT_CF = -3      // Cloudflare Access pide login (302)
    }

    private lateinit var etUrl: EditText
    private lateinit var etUser: EditText
    private lateinit var etPass: EditText
    private lateinit var btnLogin: Button
    private lateinit var btnCf: Button
    private lateinit var tvCfEstado: TextView
    private lateinit var tvError: TextView
    private lateinit var tvDebug: TextView
    private lateinit var btnDebug: Button
    private var ultimoDetalle: String = ""

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)

    private val httpClient = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(15, TimeUnit.SECONDS)
        .writeTimeout(15, TimeUnit.SECONDS)
        .build()

    private data class LoginOutcome(
        val status: Int,
        val token: String = "",
        val username: String = "",
        val detalle: String = ""   // cuerpo crudo del servidor (debug)
    )

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_login)

        etUrl = findViewById(R.id.etUrl)
        etUser = findViewById(R.id.etUser)
        etPass = findViewById(R.id.etPass)
        btnLogin = findViewById(R.id.btnLogin)
        btnCf = findViewById(R.id.btnCf)
        tvCfEstado = findViewById(R.id.tvCfEstado)
        tvError = findViewById(R.id.tvError)
        tvDebug = findViewById(R.id.tvDebug)
        btnDebug = findViewById(R.id.btnDebug)
        btnDebug.setOnClickListener { copiarDiagnostico() }

        // Ya hay sesión guardada → panel de control directo
        if (!TrackPrefs.token(this).isNullOrBlank()) {
            openMain()
            return
        }

        etUrl.setText(TrackPrefs.baseUrl(this))
        etUser.setText(TrackPrefs.username(this).orEmpty())
        actualizarEstadoCf()

        // Venimos de un 401 (sesión caducada): avisamos en el login
        if (intent.getStringExtra(EXTRA_MOTIVO) == MOTIVO_SESION_EXPIRADA) {
            showError(getString(R.string.sesion_expirada))
        }

        btnCf.setOnClickListener { abrirAuthCloudflare() }
        btnLogin.setOnClickListener { login() }
        etPass.setOnEditorActionListener { _, actionId, _ ->
            if (actionId == EditorInfo.IME_ACTION_DONE) {
                login()
                true
            } else {
                false
            }
        }
    }

    /** Abre el navegador integrado (WebView) con la web del servidor.
     *  Si Cloudflare Access protege el túnel, el SSO se hace aquí dentro.
     *  El usuario pulsa "✓ He terminado" cuando ya ve el contenido. */
    private fun abrirAuthCloudflare() {
        val base = etUrl.text?.toString()?.trim().orEmpty()
        if (base.isEmpty()) {
            showError(getString(R.string.err_url))
            return
        }
        hideError()
        TrackPrefs.setBaseUrl(this, base)
        etUrl.setText(TrackPrefs.baseUrl(this)) // normalizada (sin /track)
        // Carga la WEB real (login/SSO incluidos), NO un endpoint JSON
        startActivityForResult(
            Intent(this, AuthWebViewActivity::class.java)
                .putExtra(
                    AuthWebViewActivity.EXTRA_URL,
                    TrackPrefs.baseUrl(this) + "/"
                )
                .putExtra(
                    AuthWebViewActivity.EXTRA_USUARIO,
                    etUser.text?.toString()?.trim().orEmpty()
                ),
            RC_CF_AUTH
        )
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == RC_CF_AUTH) {
            actualizarEstadoCf()
        }
    }

    private fun actualizarEstadoCf() {
        val ok = TrackPrefs.cfAuthed(this)
        tvCfEstado.text = getString(if (ok) R.string.cf_ok else R.string.cf_pendiente)
        tvCfEstado.setTextColor(
            ContextCompat.getColor(this, if (ok) R.color.ok_green else R.color.warn_red)
        )
    }

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }

    // ── Login ──────────────────────────────────────────────────────────────

    private fun login() {
        val base = etUrl.text?.toString()?.trim().orEmpty()
        val user = etUser.text?.toString()?.trim().orEmpty()
        val pass = etPass.text?.toString().orEmpty()

        when {
            base.isEmpty() -> showError(getString(R.string.err_url))
            user.isEmpty() || pass.isEmpty() -> showError(getString(R.string.err_login_campos))
            else -> {
                hideError()
                TrackPrefs.setBaseUrl(this, base)
                etUrl.setText(TrackPrefs.baseUrl(this)) // normalizada (sin /track)
                btnLogin.isEnabled = false
                btnLogin.text = getString(R.string.btn_login_progress)
                scope.launch {
                    val out = withContext(Dispatchers.IO) { attemptLogin(user, pass) }
                    handleOutcome(out)
                }
            }
        }
    }

    /** POST {username,password} a /api/login → {token, usuario}. En hilo IO.
     *  Incluye las cookies de Cloudflare Access (si el túnel está protegido),
     *  porque sin ellas el servidor responde 302 (HTML) en vez de JSON. */
    private fun attemptLogin(user: String, pass: String): LoginOutcome {
        val url = TrackPrefs.loginUrl(this)
        val cfCookies = TrackPrefs.cfCookies(this)
        val body = JSONObject()
            .put("username", user)
            .put("password", pass)
            .toString()
        return try {
            val rb = Request.Builder()
                .url(url)
            if (cfCookies.isNotEmpty()) {
                rb.addHeader("Cookie", cfCookies)
            }
            val request = rb
                .post(body.toRequestBody(JSON_MEDIA))
                .build()
            httpClient.newCall(request).execute().use { resp ->
                val cuerpo = resp.body?.string().orEmpty()
                if (resp.isSuccessful) {
                    try {
                        val json = JSONObject(cuerpo)
                        val token = json.optString("token")
                        if (token.isBlank()) {
                            LoginOutcome(OUT_PARSE, detalle = cuerpo.take(300))
                        } else {
                            val usuario = json.optJSONObject("usuario")
                            val name =
                                usuario?.optString("username")?.takeIf { it.isNotBlank() } ?: user
                            LoginOutcome(resp.code, token, name, detalle = cuerpo.take(300))
                        }
                    } catch (e: Exception) {
                        LoginOutcome(OUT_PARSE, detalle = "NO-JSON: " + cuerpo.take(300))
                    }
                } else {
                    // 302/HTML de Cloudflare Access = aún sin sesión CF
                    if (resp.code == 302 || resp.code == 307) {
                        LoginOutcome(OUT_CF, detalle = "REDIRECCIÓN: " + cuerpo.take(200))
                    } else {
                        LoginOutcome(resp.code, detalle = "HTTP ${resp.code}: " + cuerpo.take(300))
                    }
                }
            }
        } catch (e: IOException) {
            LoginOutcome(OUT_RED, detalle = "IOException: ${e.message}")
        } catch (e: Exception) {
            LoginOutcome(OUT_PARSE, detalle = "${e.javaClass.simpleName}: ${e.message}")
        }
    }

    private fun handleOutcome(out: LoginOutcome) {
        btnLogin.isEnabled = true
        btnLogin.text = getString(R.string.btn_login)
        ultimoDetalle = out.detalle
        mostrarDetalle(out.detalle)
        when {
            out.status in 200..299 -> {
                TrackPrefs.saveSession(this, out.token, out.username)
                openMain()
            }
            out.status == 401 -> showError(getString(R.string.err_login_credenciales))
            out.status == 400 -> showError(getString(R.string.err_login_campos))
            out.status == OUT_CF -> showError(getString(R.string.err_login_cf))
            out.status == OUT_RED -> showError(getString(R.string.err_login_red))
            out.status == OUT_PARSE -> showError(getString(R.string.err_login_respuesta))
            else -> showError(getString(R.string.err_login_server, out.status))
        }
    }

    /** Muestra el detalle técnico (debug) bajo el formulario. */
    private fun mostrarDetalle(detalle: String) {
        if (detalle.isBlank()) {
            tvDebug.visibility = View.GONE
            return
        }
        tvDebug.text = detalle
        tvDebug.visibility = View.VISIBLE
    }

    /** Copia al portapapeles un diagnóstico completo para enviarlo a Hermes. */
    private fun copiarDiagnostico() {
        val host = TrackPrefs.baseUrl(this)
            .removePrefix("https://").removePrefix("http://").substringBefore('/')
        val cookies = TrackPrefs.cfCookies(this)
        val diag = buildString {
            append("URL: ").append(TrackPrefs.baseUrl(this@LoginActivity)).append('\n')
            append("Host: ").append(host).append('\n')
            append("Cookies CF: ").append(if (cookies.isEmpty()) "NINGUNA" else cookies.take(80)).append('\n')
            append("CF authed flag: ").append(TrackPrefs.cfAuthed(this@LoginActivity)).append('\n')
            append("Usuario: ").append(etUser.text).append('\n')
            append("Último error: ").append(ultimoDetalle).append('\n')
        }
        val cm = getSystemService(CLIPBOARD_SERVICE) as android.content.ClipboardManager
        cm.setPrimaryClip(android.content.ClipData.newPlainText("TrackCam diag", diag))
        showError(diag) // visible en pantalla para leerlo
        Toast.makeText(this, R.string.toast_diag_copiado, Toast.LENGTH_LONG).show()
    }

    // ── Navegación / UI ────────────────────────────────────────────────────

    private fun openMain() {
        startActivity(
            Intent(this, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK)
        )
        finish()
    }

    private fun showError(msg: String) {
        tvError.text = msg
        tvError.visibility = View.VISIBLE
    }

    private fun hideError() {
        tvError.visibility = View.GONE
    }

    private val JSON_MEDIA = "application/json; charset=utf-8".toMediaType()
}
