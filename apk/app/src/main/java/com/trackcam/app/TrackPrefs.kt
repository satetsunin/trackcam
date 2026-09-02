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
}
