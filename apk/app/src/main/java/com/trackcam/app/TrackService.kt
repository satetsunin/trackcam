package com.trackcam.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.location.Location
import android.os.Build
import android.os.Looper
import android.os.PowerManager
import android.os.SystemClock
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import androidx.lifecycle.LifecycleService
import com.google.android.gms.location.FusedLocationProviderClient
import com.google.android.gms.location.LocationCallback
import com.google.android.gms.location.LocationRequest
import com.google.android.gms.location.LocationResult
import com.google.android.gms.location.LocationServices
import com.google.android.gms.location.Priority
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.IOException
import java.text.SimpleDateFormat
import java.util.ArrayDeque
import java.util.Date
import java.util.Locale
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.coroutines.coroutineContext

private val JSON_MEDIA = "application/json; charset=utf-8".toMediaType()

/**
 * Servicio en primer plano 24/7 (Fase 5):
 *  - GPS con FusedLocationProvider (intervalo configurable, 1–60 s)
 *  - Envío inmediato de cada posición por POST JSON a <base>/track con
 *    cabecera Authorization: Bearer <token> (sesión obtenida en el login)
 *  - Si el servidor responde 401 (token caducado) → limpia la sesión,
 *    detiene el servicio y avisa a la UI para que vuelva al login
 *  - OkHttp con timeout de 8 s, 1 reintento y cola de máx. 30 pendientes
 *  - START_STICKY + notificación persistente + wake lock
 */
class TrackService : LifecycleService() {

    companion object {
        const val ACTION_START = "com.trackcam.app.action.START"
        const val ACTION_STOP = "com.trackcam.app.action.STOP"

        /** Broadcast de estado que escucha MainActivity. */
        const val ACTION_STATUS = "com.trackcam.app.action.STATUS"

        /** Broadcast: 401 → sesión caducada, volver al login. */
        const val ACTION_UNAUTHORIZED = "com.trackcam.app.action.UNAUTHORIZED"

        private const val TAG = "TrackCamService"
        private const val NOTIF_CHANNEL_ID = "trackcam_channel"
        private const val NOTIF_ID = 1
        private const val MAX_PENDING = 30
        private const val HTTP_TIMEOUT_S = 8L
        private const val MAX_ATTEMPTS = 2

        // ── Estado compartido con la UI (volátil → hilo seguro) ──
        @Volatile var tracking = false
            private set
        @Volatile var lastOk: Boolean? = null
            private set
        @Volatile var lastSendAtMillis = 0L
            private set
        @Volatile var pendingCount = 0
            private set
        @Volatile var lastLat: Double? = null
            private set
        @Volatile var lastLon: Double? = null
            private set
        @Volatile var lastAcc: Float? = null
            private set
        @Volatile var lastVel: Float? = null
            private set
        /** Última posición ENVIADA [lat, lon] (para el filtro de movimiento). */
        @Volatile var lastEnviado: DoubleArray? = null
    }

    private lateinit var fusedLocationClient: FusedLocationProviderClient
    private lateinit var wakeLock: PowerManager.WakeLock
    private lateinit var okHttpClient: OkHttpClient
    private lateinit var notifManager: NotificationManager

    private val sendScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val queueLock = Any()
    private val queue = ArrayDeque<Location>()
    private val workerRunning = AtomicBoolean(false)
    private var lastNotifText: String? = null

    private val locationCallback = object : LocationCallback() {
        override fun onLocationResult(result: LocationResult) {
            val loc = result.lastLocation ?: return
            // Filtro de fijación "basura" (0,0) que a veces devuelve el GPS
            if (loc.latitude == 0.0 && loc.longitude == 0.0) return

            lastLat = loc.latitude
            lastLon = loc.longitude
            lastAcc = loc.accuracy
            lastVel = if (loc.hasSpeed()) loc.speed else 0f

            // Detección de movimiento: si la casilla está marcada y llevamos
            // parados (<15 m desde el último envío), no spamear envíos
            // (ahorra batería/datos en reposo). El GPS sigue activo.
            if (TrackPrefs.servMovimiento(this@TrackService)) {
                val ult = lastEnviado
                if (ult != null) {
                    val d = FloatArray(1)
                    Location.distanceBetween(
                        ult[0], ult[1], loc.latitude, loc.longitude, d
                    )
                    if (d[0] < 15f && loc.speed < 0.5f) return
                }
            }

            // Envío INMEDIATO en cuanto llega la posición (sin esperar nada más)
            enqueue(loc)
        }
    }

    override fun onCreate() {
        super.onCreate()
        notifManager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        createNotificationChannel()

        fusedLocationClient = LocationServices.getFusedLocationProviderClient(this)

        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "TrackCam:location")
        wakeLock.setReferenceCounted(false)
        wakeLock.acquire()

        okHttpClient = OkHttpClient.Builder()
            .connectTimeout(HTTP_TIMEOUT_S, TimeUnit.SECONDS)
            .readTimeout(HTTP_TIMEOUT_S, TimeUnit.SECONDS)
            .writeTimeout(HTTP_TIMEOUT_S, TimeUnit.SECONDS)
            .build()

        Log.i(TAG, "Servicio creado")
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        super.onStartCommand(intent, flags, startId)
        when (intent?.action) {
            ACTION_STOP -> {
                stopTracking()
                return START_NOT_STICKY
            }
            // null intent = reinicio del sistema (START_STICKY): reanudar si toca
            else -> startTracking()
        }
        return START_STICKY
    }

    override fun onDestroy() {
        sendScope.cancel()
        if (::wakeLock.isInitialized && wakeLock.isHeld) {
            try {
                wakeLock.release()
            } catch (e: Exception) {
                // ya liberado
            }
        }
        tracking = false
        super.onDestroy()
    }

    // ── Arranque / parada ───────────────────────────────────────────────────

    private fun startTracking() {
        // Fase 5: sin token de sesión no se puede enviar nada.
        if (TrackPrefs.token(this).isNullOrBlank()) {
            Log.w(TAG, "Sin token de sesión: no se inicia el trackeo")
            TrackPrefs.setRunning(this, false)
            stopSelf()
            return
        }
        tracking = true
        TrackPrefs.setRunning(this, true)
        startForegroundCompat()
        startLocationUpdates()
        reuseLastKnownPosition()
        broadcastStatus()
    }

    private fun stopTracking() {
        tracking = false
        TrackPrefs.setRunning(this, false)
        try {
            fusedLocationClient.removeLocationUpdates(locationCallback)
        } catch (e: Exception) {
            // no había updates
        }
        synchronized(queueLock) {
            queue.clear()
        }
        pendingCount = 0
        broadcastStatus()
        ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    private fun startForegroundCompat() {
        val notification = buildNotification(getString(R.string.notif_waiting))
        try {
            ServiceCompat.startForeground(
                this,
                NOTIF_ID,
                notification,
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION
                } else {
                    0
                }
            )
        } catch (e: Exception) {
            // Pasa si no hay permiso de ubicación concedido (Android 14 lo exige
            // para un FGS de tipo "location").
            Log.e(TAG, "startForeground falló (¿permiso de ubicación?): ${e.message}")
            tracking = false
            TrackPrefs.setRunning(this, false)
            stopSelf()
        }
    }

    // ── GPS ─────────────────────────────────────────────────────────────────

    private fun startLocationUpdates() {
        // Si ya había updates (cambio de intervalo), se re-aplican limpiamente
        try {
            fusedLocationClient.removeLocationUpdates(locationCallback)
        } catch (e: Exception) {
            // nada que quitar
        }

        val intervalMs = TrackPrefs.intervalSeconds(this).coerceIn(1, 60) * 1000L
        // Servicios de ubicación elegidos por el usuario (GPS/WiFi/red):
        //  - GPS activo  → precisión total (GNSS + WiFi + red)
        //  - Solo WiFi/red → modo equilibrado (sin GPS, ahorra batería)
        //  - Sin GPS/WiFi/red → solo red móvil (bajo consumo)
        val gps = TrackPrefs.servGps(this)
        val wifi = TrackPrefs.servWifi(this)
        val red = TrackPrefs.servRed(this)
        val priority = when {
            gps -> Priority.PRIORITY_HIGH_ACCURACY
            wifi -> Priority.PRIORITY_BALANCED_POWER_ACCURACY
            red -> Priority.PRIORITY_LOW_POWER
            else -> Priority.PRIORITY_HIGH_ACCURACY // al menos algo (GPS)
        }

        val request = LocationRequest.Builder(intervalMs)
            .setPriority(priority)
            .setMinUpdateIntervalMillis(intervalMs)
            .setMaxUpdateDelayMillis(0)
            .setWaitForAccurateLocation(false)
            .build()

        Log.i(TAG, "GPS cada ${intervalMs / 1000} s · prioridad $priority")

        try {
            fusedLocationClient.requestLocationUpdates(
                request,
                locationCallback,
                Looper.getMainLooper()
            )
        } catch (e: SecurityException) {
            Log.e(TAG, "Sin permiso de ubicación: ${e.message}")
            stopTracking()
        }
    }

    /**
     * Reutiliza la última posición conocida (si es reciente) para enviar un
     * punto de inmediato sin esperar a la primera fijación GPS.
     * Con intervalo de 1 s no hace falta: el primer fix llega enseguida.
     */
    private fun reuseLastKnownPosition() {
        if (TrackPrefs.intervalSeconds(this) <= 1) return
        try {
            fusedLocationClient.lastLocation.addOnSuccessListener { loc ->
                if (loc == null) return@addOnSuccessListener
                val ageMs =
                    (SystemClock.elapsedRealtime() * 1_000_000L - loc.elapsedRealtimeNanos) / 1_000_000L
                if (ageMs in 0..60_000L) {
                    Log.i(TAG, "Reutilizando última posición (${ageMs / 1000} s de antigüedad)")
                    enqueue(loc)
                }
            }
        } catch (e: SecurityException) {
            // aún sin permiso: el requestLocationUpdates ya está en marcha
        }
    }

    // ── Cola de envíos ──────────────────────────────────────────────────────

    private fun enqueue(loc: Location) {
        val startWorker = synchronized(queueLock) {
            if (queue.size >= MAX_PENDING) queue.removeFirst() // cola llena: se descarta la más antigua
            queue.addLast(loc)
            !workerRunning.getAndSet(true)
        }
        if (startWorker) {
            sendScope.launch { senderLoop() }
        }
    }

    private suspend fun senderLoop() {
        while (coroutineContext.isActive && tracking) {
            val loc = synchronized(queueLock) { queue.pollFirst() } ?: break
            val ok = sendWithRetry(loc)
            synchronized(queueLock) {
                // Reintento fallido (red): vuelve al principio de la cola.
                // Tras un 401 tracking ya es false: no se re-encola nada.
                if (!ok && tracking && queue.size < MAX_PENDING) {
                    queue.addFirst(loc)
                }
                pendingCount = queue.size
            }
            if (ok && tracking && pendingCount == 0) updateNotification()
        }
        workerRunning.set(false)
    }

    /**
     * POST JSON a <base>/track con Authorization: Bearer <token>.
     * OkHttp con timeout de 8 s y 1 reintento (2 intentos en total).
     * Devuelve false solo si ambos intentos fallaron por red (se re-encola).
     * Un 401 (token caducado) no se reintenta: se limpia la sesión y se
     * avisa a la UI para volver al login.
     */
    private suspend fun sendWithRetry(loc: Location): Boolean {
        val token = TrackPrefs.token(this)
        if (token.isNullOrBlank()) {
            Log.w(TAG, "Sin token de sesión: no se puede enviar la posición")
            handleUnauthorized()
            return true
        }
        val url = TrackPrefs.trackUrl(this)
        val json = buildJson(loc)
        // Cookies de Cloudflare Access (si el túnel está protegido con SSO)
        val cfCookies = TrackPrefs.cfCookies(this)
        var attempts = 0

        while (attempts < MAX_ATTEMPTS) {
            attempts++
            try {
                val rb = Request.Builder()
                    .url(url)
                    .addHeader("Authorization", "Bearer $token")
                if (cfCookies.isNotEmpty()) {
                    rb.addHeader("Cookie", cfCookies)
                }
                val request = rb
                    .post(json.toRequestBody(JSON_MEDIA))
                    .build()

                val response = okHttpClient.newCall(request).execute()
                val code = response.code
                response.close()

                if (code == 401) {
                    Log.w(TAG, "HTTP 401: token inválido o expirado → volver al login")
                    handleUnauthorized()
                    return true
                }
                if (code !in 200..299) throw IOException("HTTP $code")

                lastOk = true
                lastSendAtMillis = System.currentTimeMillis()
                lastEnviado = doubleArrayOf(loc.latitude, loc.longitude)
                broadcastStatus()
                updateNotification()
                Log.i(TAG, "Enviado ${loc.latitude},${loc.longitude} a $url")
                return true
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                Log.w(TAG, "Intento $attempts/$MAX_ATTEMPTS fallido: ${e.message}")
                if (attempts < MAX_ATTEMPTS) delay(1000L)
            }
        }

        lastOk = false
        lastSendAtMillis = System.currentTimeMillis()
        broadcastStatus()
        updateNotification()
        Log.w(TAG, "Envío fallido definitivo (se re-encola si hay hueco)")
        return false
    }

    /**
     * 401: la sesión ya no es válida. Borra el token guardado, detiene el
     * trackeo y avisa por broadcast para que la UI abra el login.
     */
    private fun handleUnauthorized() {
        Log.w(TAG, "Sesión no autorizada: limpiando token y deteniendo servicio")
        TrackPrefs.clearSession(this)
        broadcastUnauthorized()
        stopTracking()
    }

    private fun broadcastUnauthorized() {
        try {
            // setPackage → el broadcast va solo a nuestra app (seguro en API 34)
            sendBroadcast(Intent(ACTION_UNAUTHORIZED).setPackage(packageName))
        } catch (e: Exception) {
            // sin listeners
        }
    }

    private fun buildJson(loc: Location): String {
        return JSONObject()
            .put("lat", loc.latitude)
            .put("lon", loc.longitude)
            .put("ts", System.currentTimeMillis() / 1000.0)
            .put("acc", if (loc.hasAccuracy()) loc.accuracy.toDouble() else 0.0)
            .put("vel", if (loc.hasSpeed()) loc.speed.toDouble() else 0.0)
            .put("dev", deviceId())
            .toString()
    }

    /** Identificador de dispositivo: fabricante_modelo (el servidor lo trunca a 40). */
    private fun deviceId(): String {
        val id = Build.MANUFACTURER.replace(" ", "_") + "_" + Build.MODEL.replace(" ", "_")
        return id.take(40)
    }

    // ── Notificación ────────────────────────────────────────────────────────

    private fun createNotificationChannel() {
        val channel = NotificationChannel(
            NOTIF_CHANNEL_ID,
            getString(R.string.notif_channel_name),
            NotificationManager.IMPORTANCE_LOW
        ).apply {
            description = getString(R.string.notif_channel_desc)
            setShowBadge(false)
        }
        notifManager.createNotificationChannel(channel)
    }

    private fun buildNotification(text: String): Notification {
        val openIntent = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        val stopIntent = PendingIntent.getService(
            this,
            1,
            Intent(this, TrackService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        return NotificationCompat.Builder(this, NOTIF_CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_stat_trackcam)
            .setContentTitle(getString(R.string.notif_title))
            .setContentText(text)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setContentIntent(openIntent)
            .addAction(0, getString(R.string.notif_stop), stopIntent)
            .build()
    }

    private fun notificationText(): String {
        val pend = synchronized(queueLock) { queue.size }
        if (lastSendAtMillis == 0L) return getString(R.string.notif_waiting)
        val t = SimpleDateFormat("HH:mm:ss", Locale.getDefault()).format(Date(lastSendAtMillis))
        return if (lastOk == true) {
            getString(R.string.notif_sent_ok, t)
        } else {
            getString(R.string.notif_sent_fail, t, pend)
        }
    }

    private fun updateNotification() {
        val text = notificationText()
        if (text == lastNotifText) return // sin cambios: no martillear el sistema
        lastNotifText = text
        try {
            notifManager.notify(NOTIF_ID, buildNotification(text))
        } catch (e: Exception) {
            Log.w(TAG, "No se pudo actualizar la notificación: ${e.message}")
        }
    }

    private fun broadcastStatus() {
        try {
            // setPackage → el broadcast va solo a nuestra app (seguro en API 34)
            sendBroadcast(Intent(ACTION_STATUS).setPackage(packageName))
        } catch (e: Exception) {
            // sin listeners
        }
    }
}
