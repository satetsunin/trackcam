package com.trackcam.app

import android.content.Intent
import android.os.Bundle
import android.view.View
import android.view.inputmethod.EditorInfo
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
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

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)

    private val httpClient = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(15, TimeUnit.SECONDS)
        .writeTimeout(15, TimeUnit.SECONDS)
        .build()

    private data class LoginOutcome(
        val status: Int,
        val token: String = "",
        val username: String = ""
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
                if (resp.isSuccessful) {
                    val json = JSONObject(resp.body?.string().orEmpty())
                    val token = json.optString("token")
                    if (token.isBlank()) {
                        LoginOutcome(OUT_PARSE)
                    } else {
                        val usuario = json.optJSONObject("usuario")
                        val name =
                            usuario?.optString("username")?.takeIf { it.isNotBlank() } ?: user
                        LoginOutcome(resp.code, token, name)
                    }
                } else {
                    // 302/HTML de Cloudflare Access = aún sin sesión CF
                    if (resp.code == 302 || resp.code == 307) {
                        LoginOutcome(OUT_CF)
                    } else {
                        LoginOutcome(resp.code)
                    }
                }
            }
        } catch (e: IOException) {
            LoginOutcome(OUT_RED)
        } catch (e: Exception) {
            LoginOutcome(OUT_PARSE)
        }
    }

    private fun handleOutcome(out: LoginOutcome) {
        btnLogin.isEnabled = true
        btnLogin.text = getString(R.string.btn_login)
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
