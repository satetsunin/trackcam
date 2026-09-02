package com.trackcam.app

import android.content.Context

/**
 * Wrapper de SharedPreferences: guarda la URL del servidor, el intervalo
 * de envío y si el trackeo debe estar activo (para el auto-reinicio).
 */
object TrackPrefs {

    private const val PREFS_NAME = "trackcam_prefs"

    /** Túnel Cloudflare del usuario (por defecto). */
    const val DEFAULT_URL = "https://track.satetsunin.com/track"

    /** Intervalo por defecto: 5 s (mínimo permitido 1 s). */
    const val DEFAULT_INTERVAL = 5

    private fun prefs(ctx: Context) =
        ctx.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    fun serverUrl(ctx: Context): String =
        prefs(ctx).getString("server_url", DEFAULT_URL) ?: DEFAULT_URL

    fun setServerUrl(ctx: Context, url: String) {
        val cleaned = url.trim().trimEnd('/')
        prefs(ctx).edit().putString("server_url", cleaned).apply()
    }

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
}
