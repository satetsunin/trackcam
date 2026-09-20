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

    /** Intervalo por defecto: 2 s (modo prueba v1.8; mínimo 1 s). */
    const val DEFAULT_INTERVAL = 2

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
        "cola_max" to 200000.0,
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

    // ── Cola offline persistente (F5.37: fichero append-only) ──────────
    //
    // ANTES: una única cadena JSON en SharedPreferences. Cada punto guardado
    // obligaba a leer y REESCRIBIR la cola completa (O(n) por punto) y con
    // miles de pendientes el fichero de preferencias se volvía inmanejable
    // (el JSON viajaba entero en cada operación y en cada vuelta del envío).
    // AHORA: un fichero de texto con una línea por punto "ts,lat,lon,acc,vel".
    // Guardar es un append (O(1)); el envío lee la cola UNA vez y reescribe
    // solo lo que quede pendiente. El contador se lleva aparte para poder
    // preguntar cuántos hay sin leer el fichero.

    private const val FICHERO_COLA = "cola_offline.csv"
    private const val KEY_COLA_VIEJA = "cola_offline_json"   // formato antiguo
    private const val KEY_COLA_N = "cola_offline_n"          // nº de puntos guardados
    private const val KEY_COLA_ULT_TS = "cola_offline_ult_ts"
    private val lockCola = Any()

    private fun ficheroCola(ctx: Context) = java.io.File(ctx.filesDir, FICHERO_COLA)

    private fun lineaPunto(p: DoubleArray): String =
        "${p[0].toLong()},${p[1]},${p[2]},${p[3]},${p[4]}\n"

    private fun puntoDeLinea(l: String): DoubleArray? = try {
        val c = l.trim().split(',')
        if (c.size < 5) null
        else doubleArrayOf(c[0].toDouble(), c[1].toDouble(), c[2].toDouble(),
                           c[3].toDouble(), c[4].toDouble())
    } catch (e: Exception) {
        null
    }

    /** Pasa (una sola vez) la cola del formato antiguo al fichero nuevo. */
    private fun migrarColaAntigua(ctx: Context) {
        val viejo = prefs(ctx).getString(KEY_COLA_VIEJA, null) ?: return
        try {
            val arr = org.json.JSONArray(viejo)
            val sb = StringBuilder()
            for (i in 0 until arr.length()) {
                val o = arr.getJSONArray(i)
                sb.append(o.getDouble(0).toLong()).append(',')
                    .append(o.getDouble(1)).append(',')
                    .append(o.getDouble(2)).append(',')
                    .append(o.getDouble(3)).append(',')
                    .append(o.getDouble(4)).append('\n')
            }
            if (sb.isNotEmpty()) {
                ficheroCola(ctx).appendText(sb.toString())
                val n = prefs(ctx).getInt(KEY_COLA_N, 0) + arr.length()
                prefs(ctx).edit().putInt(KEY_COLA_N, n)
                    .putLong(KEY_COLA_ULT_TS, arr.getJSONArray(arr.length() - 1).getDouble(0).toLong())
                    .apply()
            }
        } catch (e: Exception) {
            // cola antigua ilegible: se descarta (mejor seguir trackeando)
        }
        prefs(ctx).edit().remove(KEY_COLA_VIEJA).apply()
    }

    /** Cuenta de verdad los puntos del fichero (por si el contador se desajustó). */
    fun colaOfflineContar(ctx: Context): Int = synchronized(lockCola) {
        val f = ficheroCola(ctx)
        val n = if (!f.exists()) 0 else try {
            f.readLines().count { it.isNotBlank() }
        } catch (e: Exception) {
            0
        }
        prefs(ctx).edit().putInt(KEY_COLA_N, n).apply()
        n
    }

    /** Puntos pendientes [ts,lat,lon,acc,vel], del más antiguo al más nuevo. */
    fun colaOffline(ctx: Context): List<DoubleArray> = synchronized(lockCola) {
        migrarColaAntigua(ctx)
        val f = ficheroCola(ctx)
        if (!f.exists()) return emptyList()
        try {
            f.readLines().asSequence().filter { it.isNotBlank() }
                .mapNotNull { puntoDeLinea(it) }.toList()
        } catch (e: Exception) {
            // F5.38 — No se pudo leer la cola: se APARTA el fichero en vez de
            // dejarlo para que el siguiente guardado lo sobrescriba (así los
            // puntos, aunque ilegibles, no se destruyen en silencio).
            try {
                f.renameTo(java.io.File(ctx.filesDir, "$FICHERO_COLA.bad"))
                prefs(ctx).edit().putInt(KEY_COLA_N, 0).remove(KEY_COLA_ULT_TS).apply()
            } catch (e2: Exception) {
                // sin permisos para apartarlo: se sigue
            }
            emptyList()
        }
    }

    /** Nº de puntos pendientes sin leer el fichero entero. */
    fun colaOfflineSize(ctx: Context): Int {
        migrarColaAntigua(ctx)
        val f = ficheroCola(ctx)
        if (!f.exists() || f.length() == 0L) return 0
        val n = prefs(ctx).getInt(KEY_COLA_N, -1)
        return if (n >= 0) n else colaOfflineContar(ctx)
    }

    /** Último ts guardado (permite deduplicar sin releer la cola). */
    fun colaOfflineUltimoTs(ctx: Context): Long = prefs(ctx).getLong(KEY_COLA_ULT_TS, 0L)

    /** Añade un punto a la cola (append: O(1), sin reescribir la cola).
     *
     * F5.38 — El dedupe por ts vive AQUÍ, dentro del lock: antes se comprobaba
     * fuera (en el servicio) y dos llamadas concurrentes con el mismo fix
     * podían colar la copia (carrera TOCTOU). */
    fun colaOfflineAdd(ctx: Context, ts: Long, lat: Double, lon: Double,
                       acc: Float, vel: Float): Boolean = synchronized(lockCola) {
        migrarColaAntigua(ctx)
        if (prefs(ctx).getLong(KEY_COLA_ULT_TS, 0L) == ts) return false   // ya está
        try {
            var n = prefs(ctx).getInt(KEY_COLA_N, 0)
            val tope = cfgColaMax(ctx)
            if (n >= tope) {
                // Cola llena: se tira lo más ANTIGUO (perder lo viejo es menos
                // malo que quedarse sin lo reciente).
                val restantes = colaOffline(ctx).takeLast(tope - 1)
                colaOfflineReescribirLocked(ctx, restantes)
                n = restantes.size
            }
            ficheroCola(ctx).appendText(
                lineaPunto(doubleArrayOf(ts.toDouble(), lat, lon, acc.toDouble(), vel.toDouble())))
            prefs(ctx).edit().putInt(KEY_COLA_N, n + 1)
                .putLong(KEY_COLA_ULT_TS, ts).apply()
            true
        } catch (e: Exception) {
            false
        }
    }

    /** Reescribe la cola con los puntos que quedan pendientes. */
    fun colaOfflineReescribir(ctx: Context, quedan: List<DoubleArray>) {
        synchronized(lockCola) { colaOfflineReescribirLocked(ctx, quedan) }
    }

    private fun colaOfflineReescribirLocked(ctx: Context, quedan: List<DoubleArray>) {
        val f = ficheroCola(ctx)
        try {
            if (quedan.isEmpty()) {
                if (f.exists()) f.delete()
                prefs(ctx).edit().putInt(KEY_COLA_N, 0).remove(KEY_COLA_ULT_TS).apply()
                return
            }
            val tmp = java.io.File(ctx.filesDir, "$FICHERO_COLA.tmp")
            tmp.writeText(quedan.joinToString("") { lineaPunto(it) })
            if (f.exists() && !f.delete()) {
                // no se pudo borrar: se escribe encima
                f.writeText(quedan.joinToString("") { lineaPunto(it) })
                tmp.delete()
            } else if (!tmp.renameTo(f)) {
                f.writeText(quedan.joinToString("") { lineaPunto(it) })
                tmp.delete()
            }
            prefs(ctx).edit().putInt(KEY_COLA_N, quedan.size)
                .putLong(KEY_COLA_ULT_TS, quedan.last()[0].toLong()).apply()
        } catch (e: Exception) {
            // si falló la escritura, el contador no se toca (se recontará)
        }
    }

    fun colaOfflineRemoveFirst(ctx: Context): DoubleArray? {
        val items = colaOffline(ctx)
        if (items.isEmpty()) return null
        val first = items.first()
        colaOfflineReescribir(ctx, items.drop(1))
        return first
    }

    fun colaOfflineClear(ctx: Context) {
        synchronized(lockCola) {
            val f = ficheroCola(ctx)
            if (f.exists()) f.delete()
            prefs(ctx).edit().putInt(KEY_COLA_N, 0).remove(KEY_COLA_ULT_TS).apply()
        }
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
    /** F5.38 — El trackeo se paró por un 401 y hay que reanudarlo tras el login. */
    fun relanzarTrasLogin(ctx: Context): Boolean =
        prefs(ctx).getBoolean("relanzar_tras_login", false)

    fun setRelanzarTrasLogin(ctx: Context, si: Boolean) {
        prefs(ctx).edit().putBoolean("relanzar_tras_login", si).apply()
    }

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
