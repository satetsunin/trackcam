package com.trackcam.app

import android.content.Context

/**
 * Wrapper de SharedPreferences (Fase 5): guarda la URL BASE del servidor
 * (sin /track ni /api/login, que se derivan), el intervalo de envío, si el
 * trackeo debe estar activo y la sesión {token, usuario} del login.
 */
object TrackPrefs {

    private const val PREFS_NAME = "trackcam_prefs"

    /** Servidor por defecto (túnel Cloudflare del usuario). */
    const val DEFAULT_BASE_URL = "https://track.satetsunin.com"

    /** Intervalo por defecto: 5 s (mínimo permitido 1 s). */
    const val DEFAULT_INTERVAL = 5

    private fun prefs(ctx: Context) =
        ctx.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    /** Limpia la URL: recorta espacios y el sufijo "/track" de versiones v1. */
    private fun cleanBase(url: String): String {
        var s = url.trim().trimEnd('/')
        if (s.endsWith("/track")) s = s.removeSuffix("/track").trimEnd('/')
        return s
    }

    // ── Servidor ───────────────────────────────────────────────────────────

    fun baseUrl(ctx: Context): String {
        val stored = prefs(ctx).getString("server_base", null)
        if (!stored.isNullOrBlank()) return cleanBase(stored)
        // Migración desde v1: "server_url" guardaba la URL completa con /track
        val old = prefs(ctx).getString("server_url", null)
        return if (old.isNullOrBlank()) DEFAULT_BASE_URL else cleanBase(old)
    }

    fun setBaseUrl(ctx: Context, url: String) {
        prefs(ctx).edit()
            .putString("server_base", cleanBase(url))
            .remove("server_url")
            .apply()
    }

    /** Endpoint de login: POST {username,password} → {token, usuario}. */
    fun loginUrl(ctx: Context): String = baseUrl(ctx) + "/api/login"

    /** Endpoint de posición: POST JSON con Authorization: Bearer <token>. */
    fun trackUrl(ctx: Context): String = baseUrl(ctx) + "/track"

    // ── Sesión (F5) ────────────────────────────────────────────────────────

    fun token(ctx: Context): String? =
        prefs(ctx).getString("token", null)

    fun username(ctx: Context): String? =
        prefs(ctx).getString("username", null)

    fun saveSession(ctx: Context, token: String, username: String) {
        prefs(ctx).edit()
            .putString("token", token)
            .putString("username", username)
            .apply()
    }

    /** Borra la sesión (login caducado / logout). */
    fun clearSession(ctx: Context) {
        prefs(ctx).edit()
            .remove("token")
            .remove("username")
            .apply()
    }

    // ── Intervalo y estado ─────────────────────────────────────────────────

    fun intervalSeconds(ctx: Context): Int =
        prefs(ctx).getInt("interval_seconds", DEFAULT_INTERVAL)

    fun setIntervalSeconds(ctx: Context, seconds: Int) {
        prefs(ctx).edit().putInt("interval_seconds", seconds.coerceIn(1, 60)).apply()
    }

    /** true = el usuario quiere trackeo 24/7 (se usa al reiniciar el teléfono). */
    fun running(ctx: Context): Boolean =
        prefs(ctx).getBoolean("running", false)

    fun setRunning(ctx: Context, running: Boolean) {
        prefs(ctx).edit().putBoolean("running", running).apply()
    }

    /** true = ya se pidió el permiso de ubicación en segundo plano. */
    fun askedBackgroundOnce(ctx: Context): Boolean =
        prefs(ctx).getBoolean("asked_bg_location", false)

    fun setAskedBackgroundOnce(ctx: Context, asked: Boolean) {
        prefs(ctx).edit().putBoolean("asked_bg_location", asked).apply()
    }

    // ── Servicios de ubicación (GPS / WiFi / red / movimiento) ──────────

    /** Todos activados por defecto (el usuario desmarca los que no quiera). */
    fun servGps(ctx: Context): Boolean =
        prefs(ctx).getBoolean("serv_gps", true)

    fun setServGps(ctx: Context, on: Boolean) {
        prefs(ctx).edit().putBoolean("serv_gps", on).apply()
    }

    fun servWifi(ctx: Context): Boolean =
        prefs(ctx).getBoolean("serv_wifi", true)

    fun setServWifi(ctx: Context, on: Boolean) {
        prefs(ctx).edit().putBoolean("serv_wifi", on).apply()
    }

    fun servRed(ctx: Context): Boolean =
        prefs(ctx).getBoolean("serv_red", true)

    fun setServRed(ctx: Context, on: Boolean) {
        prefs(ctx).edit().putBoolean("serv_red", on).apply()
    }

    fun servMovimiento(ctx: Context): Boolean =
        prefs(ctx).getBoolean("serv_movimiento", true)

    fun setServMovimiento(ctx: Context, on: Boolean) {
        prefs(ctx).edit().putBoolean("serv_movimiento", on).apply()
    }

    /** true si hay al menos un servicio de ubicación activo. */
    fun algunServicio(ctx: Context): Boolean =
        servGps(ctx) || servWifi(ctx) || servRed(ctx) || servMovimiento(ctx)

    // ── Sesión Cloudflare Access (WebView) ──────────────────────────────

    /** true = el usuario completó el SSO de Cloudflare en el WebView. */
    fun cfAuthed(ctx: Context): Boolean =
        prefs(ctx).getBoolean("cf_authed", false)

    fun setCfAuthed(ctx: Context, authed: Boolean) {
        prefs(ctx).edit().putBoolean("cf_authed", authed).apply()
    }

    /** Cookies de Cloudflare para el host del servidor (CF_Authorization…). */
    fun cfCookies(ctx: Context): String {
        val host = baseUrl(ctx)
            .removePrefix("https://").removePrefix("http://").substringBefore('/')
        return try {
            android.webkit.CookieManager.getInstance().getCookie(host).orEmpty()
        } catch (e: Exception) {
            ""
        }
    }

    /** Limpia las cookies de Cloudflare (logout). */
    fun clearCfSession(ctx: Context) {
        setCfAuthed(ctx, false)
        try {
            android.webkit.CookieManager.getInstance().removeAllCookies(null)
        } catch (e: Exception) {
            // sin cookies que limpiar
        }
    }
}
