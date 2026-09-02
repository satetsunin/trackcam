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

    // ── Config remota (OTA desde el servidor, sin recompilar) ────────────

    private const val KEY_CFG = "config_remota_json"
    private const val KEY_CFG_VER = "config_remota_version"

    /** Config por defecto (coincide con el servidor). */
    val CONFIG_DEFAULT = mapOf(
        "vel_vehiculo_kmh" to 20.0,     // > esto = vehículo
        "vel_andando_kmh" to 6.0,       // entre andando y vehículo = andando
        "intervalo_vehiculo_s" to 2.0,  // en vehículo: cada 2 s
        "intervalo_andando_s" to 10.0,  // andando: cada 10 s
        "intervalo_parado_s" to 600.0,  // parado: cada 10 min
        "cola_offline" to true,         // guardar sin cobertura
        "cola_max" to 5000.0,
        "radio_cache_m" to 2000.0,
    )

    /** Guarda la config remota descargada del servidor. */
    fun saveRemoteConfig(ctx: Context, map: Map<String, Any?>) {
        val json = org.json.JSONObject()
        CONFIG_DEFAULT.forEach { (k, v) ->
            json.put(k, map[k] ?: v)
        }
        prefs(ctx).edit()
            .putString(KEY_CFG, json.toString())
            .putInt(KEY_CFG_VER, (map["version_config"] as? Number)?.toInt() ?: 0)
            .apply()
    }

    fun configVersion(ctx: Context): Int =
        prefs(ctx).getInt(KEY_CFG_VER, 0)

    private fun cfgMap(ctx: Context): Map<String, Double> {
        val raw = prefs(ctx).getString(KEY_CFG, null)
        val out = HashMap<String, Double>()
        CONFIG_DEFAULT.forEach { (k, v) ->
            if (v is Double) out[k] = v
        }
        if (raw != null) {
            try {
                val o = org.json.JSONObject(raw)
                CONFIG_DEFAULT.keys.forEach { k ->
                    if (o.has(k)) out[k] = o.getDouble(k)
                }
            } catch (e: Exception) { /* defaults */ }
        }
        return out
    }

    fun cfgVehiculoKmh(ctx: Context): Double = cfgMap(ctx)["vel_vehiculo_kmh"] ?: 20.0
    fun cfgAndandoKmh(ctx: Context): Double = cfgMap(ctx)["vel_andando_kmh"] ?: 6.0
    fun cfgIntervaloVehiculoS(ctx: Context): Int = cfgMap(ctx)["intervalo_vehiculo_s"]!!.toInt()
    fun cfgIntervaloAndandoS(ctx: Context): Int = cfgMap(ctx)["intervalo_andando_s"]!!.toInt()
    fun cfgIntervaloParadoS(ctx: Context): Int = cfgMap(ctx)["intervalo_parado_s"]!!.toInt()
    fun cfgColaOffline(ctx: Context): Boolean =
        prefs(ctx).getString(KEY_CFG, null)?.let {
            try { org.json.JSONObject(it).optBoolean("cola_offline", true) } catch (e: Exception) { true }
        } ?: true
    fun cfgColaMax(ctx: Context): Int = cfgMap(ctx)["cola_max"]!!.toInt()

    /** Clasifica la velocidad (m/s) en: vehiculo / andando / parado. */
    fun modoPorVelocidad(ctx: Context, velMs: Float): String {
        val kmh = velMs * 3.6
        return when {
            kmh >= cfgVehiculoKmh(ctx) -> "vehiculo"
            kmh >= cfgAndandoKmh(ctx) -> "andando"
            else -> "parado"
        }
    }

    /** Intervalo de envío (s) según el modo de transporte. */
    fun intervaloParaModo(ctx: Context, modo: String): Int = when (modo) {
        "vehiculo" -> cfgIntervaloVehiculoS(ctx)
        "andando" -> cfgIntervaloAndandoS(ctx)
        else -> cfgIntervaloParadoS(ctx)
    }

    // ── Cola offline persistente (sin cobertura → se guarda y reenvía) ────

    private const val KEY_COLA = "cola_offline_json"

    /** Puntos pendientes [ts,lat,lon,acc,vel] guardados sin conexión. */
    fun colaOffline(ctx: Context): List<DoubleArray> {
        val raw = prefs(ctx).getString(KEY_COLA, null) ?: return emptyList()
        return try {
            val arr = org.json.JSONArray(raw)
            (0 until arr.length()).map { i ->
                val o = arr.getJSONArray(i)
                doubleArrayOf(o.getDouble(0), o.getDouble(1), o.getDouble(2),
                    o.getDouble(3), o.getDouble(4))
            }
        } catch (e: Exception) { emptyList() }
    }

    fun colaOfflineSize(ctx: Context): Int = colaOffline(ctx).size

    /** Añade un punto a la cola offline (respeta cola_max). */
    fun colaOfflineAdd(ctx: Context, ts: Long, lat: Double, lon: Double,
                       acc: Float, vel: Float): Boolean {
        val items = colaOffline(ctx).toMutableList()
        if (items.size >= cfgColaMax(ctx)) items.removeAt(0)
        items.add(doubleArrayOf(ts.toDouble(), lat, lon, acc.toDouble(), vel.toDouble()))
        colaOfflineSet(ctx, items)
        return true
    }

    fun colaOfflineRemoveFirst(ctx: Context): DoubleArray? {
        val items = colaOffline(ctx).toMutableList()
        if (items.isEmpty()) return null
        val first = items.removeAt(0)
        colaOfflineSet(ctx, items)
        return first
    }

    fun colaOfflineClear(ctx: Context) {
        prefs(ctx).edit().remove(KEY_COLA).apply()
    }

    private fun colaOfflineSet(ctx: Context, items: List<DoubleArray>) {
        val arr = org.json.JSONArray()
        items.forEach { p ->
            arr.put(org.json.JSONArray().put(p[0]).put(p[1]).put(p[2]).put(p[3]).put(p[4]))
        }
        prefs(ctx).edit().putString(KEY_COLA, arr.toString()).apply()
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
