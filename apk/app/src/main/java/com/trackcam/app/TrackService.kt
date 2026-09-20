package com.trackcam.app

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.location.Location
import android.net.wifi.ScanResult
import android.net.wifi.WifiManager
import android.os.Build
import android.os.Looper
import android.os.PowerManager
import android.os.SystemClock
import android.provider.Settings
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import androidx.lifecycle.LifecycleService
import com.google.android.gms.location.ActivityRecognition
import com.google.android.gms.location.ActivityRecognitionClient
import com.google.android.gms.location.ActivityRecognitionResult
import com.google.android.gms.location.DetectedActivity
import com.google.android.gms.location.FusedLocationProviderClient
import com.google.android.gms.location.LocationCallback
import com.google.android.gms.location.LocationRequest
import com.google.android.gms.location.LocationResult
import com.google.android.gms.location.LocationServices
import com.google.android.gms.location.Priority
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
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
import java.security.MessageDigest
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
        /**
         * F5.27b — PULSO EN PARADO.
         *
         * Android deja de entregar posiciones cuando el móvil está quieto (es
         * ahorro de batería del sistema, no de la app) y la traza quedaba con
         * huecos de hasta 42 minutos: al reanudar el movimiento la línea
         * "saltaba" 50-130 m porque el último punto enviado era muy antiguo
         * (medido en la traza real del 14-09: 15 casos en un día; el usuario lo
         * describió como "hace un salto como si no hubiera registrado").
         *
         * Si no ha llegado ningún fix en PULSO_S se pide una posición puntual de
         * bajo consumo (sirve la de red/wifi, no hace falta GPS fino) y se
         * envía, así el hueco nunca pasa de ~2 minutos.
         */
        private const val PULSO_S = 120_000L

        /** F5.27b — Job del pulso en parado (evita huecos largos en la traza). */
        @Volatile var pulsoJob: Job? = null

        const val ACTION_START = "com.trackcam.app.action.START"
        const val ACTION_STOP = "com.trackcam.app.action.STOP"

        /** Broadcast de estado que escucha MainActivity. */
        const val ACTION_STATUS = "com.trackcam.app.action.STATUS"

        /**
         * F5.31 — Punto forzado por la alarma anti-Doze (AlarmReceiver).
         * Android duerme la app de madrugada y deja huecos de horas; la alarma
         * despierta el proceso y aquí se envía un punto.
         */
        const val ACTION_PULSO_ALARMA = "com.trackcam.app.action.PULSO_ALARMA"

        /** Broadcast: 401 → sesión caducada, volver al login. */
        const val ACTION_UNAUTHORIZED = "com.trackcam.app.action.UNAUTHORIZED"

        /**
         * Acción interna del PendingIntent con el que Google entrega los
         * resultados de Activity Recognition a este mismo servicio.
         * NO es una orden de arranque/parada: solo refresca la última actividad.
         */
        const val ACTION_ACTIVITY_UPDATE = "com.trackcam.app.action.ACTIVITY_UPDATE"

        private const val TAG = "TrackCamService"
        private const val NOTIF_CHANNEL_ID = "trackcam_channel"
        private const val NOTIF_ID = 1
        private const val MAX_PENDING = 30
        private const val HTTP_TIMEOUT_S = 8L
        private const val MAX_ATTEMPTS = 2

        /** Cada cuánto pide Google una muestra de actividad (~25 s: barato). */
        private const val ACTIVITY_INTERVAL_MS = 25_000L
        private const val ACTIVITY_PI_REQUEST = 42

        /**
         * Cada cuánto se RECALCULA la huella de redes wifi visibles (~8 min).
         * NO en cada punto: en Android moderno startScan() está limitado
         * (throttling en segundo plano) y escanear cada 2 s no aportaría nada.
         * Entre recálculo y recálculo se reenvía la última huella calculada.
         */
        private const val WIFI_HUE_INTERVAL_MS = 8 * 60 * 1000L

        /** Pausa tras startScan() para que el sistema rellene la lista (best-effort). */
        private const val WIFI_SCAN_SETTLE_MS = 1_200L

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

        /**
         * Última actividad conocida, ya mapeada al contrato del servidor:
         * 'still'|'walk'|'run'|'bike'|'vehicle'|'tilt' — "" si se desconoce
         * (sin permiso, sin datos todavía o actividad UNKNOWN).
         */
        @Volatile var lastActivity: String = ""
            private set

        /** Confianza (0-100) de [lastActivity]; 0 si se desconoce. */
        @Volatile var lastActivityConf: Int = 0
            private set
    }

    private lateinit var fusedLocationClient: FusedLocationProviderClient
    private lateinit var wakeLock: PowerManager.WakeLock
    private lateinit var okHttpClient: OkHttpClient
    private lateinit var notifManager: NotificationManager

    /** Cliente de detección de actividad (acelerómetro) de Google Play Services. */
    private lateinit var activityRecognitionClient: ActivityRecognitionClient

    /** PendingIntent registrado para recibir los resultados de actividad (null = sin registrar). */
    private var activityPendingIntent: PendingIntent? = null

    /**
     * Identificador ESTABLE del dispositivo: Settings.Secure.ANDROID_ID.
     * Es un valor de 64 bits (hex de 16 caracteres) único por app+usuario+
     * dispositivo; NO cambia mientras la app siga instalada, así que se lee
     * una sola vez y se reutiliza en todos los puntos.
     * A diferencia de `dev` (fabricante_modelo, que es idéntico en todos los
     * Redmi de este modelo) permite distinguir dispositivos en la BD.
     * No requiere ningún permiso; si no se puede leer → cadena vacía.
     */
    private val androidId: String by lazy {
        try {
            Settings.Secure.getString(contentResolver, Settings.Secure.ANDROID_ID).orEmpty()
        } catch (e: Exception) {
            Log.w(TAG, "No se pudo leer ANDROID_ID: ${e.message}")
            ""
        }
    }

    /** Última huella de redes wifi visibles ("<hash8>:<n>"), "" si no hay datos. */
    @Volatile
    private var wifiHueActual: String = ""

    /** Job del bucle periódico de escaneo wifi (null = no está corriendo). */
    private var wifiHueJob: Job? = null

    private val sendScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    /**
     * Comprueba cada [PULSO_S] si el último envío es antiguo y, en ese caso,
     * pide una posición puntual y la envía. Se cancela al parar el seguimiento.
     */
    /**
     * F5.31 — Un punto forzado por la alarma anti-Doze (cada
     * [AlarmReceiver.ALARMA_MIN] minutos). A diferencia del pulso por corrutina
     * —que respeta el último envío— aquí se pide posición SIEMPRE: si la alarma
     * ha despertado el móvil, ese punto es justo lo que evita el hueco.
     */
    private fun pulsoPorAlarma() {
        try {
            fusedLocationClient.getCurrentLocation(
                com.google.android.gms.location.Priority.PRIORITY_BALANCED_POWER_ACCURACY,
                null
            ).addOnSuccessListener { loc ->
                if (loc != null) {
                    Log.i(TAG, "Alarma anti-Doze: enviando punto forzado")
                    enqueue(loc)
                } else {
                    Log.i(TAG, "Alarma anti-Doze: el sistema no dio posicion")
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "Alarma anti-Doze: fallo pidiendo posicion: ${e.message}")
        }
    }

    private fun startPulsoParado() {
        if (pulsoJob?.isActive == true) return
        pulsoJob = sendScope.launch {
            while (isActive) {
                delay(PULSO_S)
                if (!tracking) return@launch
                val desde = System.currentTimeMillis() - lastSendAtMillis
                if (lastSendAtMillis > 0L && desde < PULSO_S) continue
                try {
                    // Posición puntual de bajo consumo (sirve la de red/wifi):
                    // solo queremos un punto para que el hueco no crezca.
                    fusedLocationClient.getCurrentLocation(
                        com.google.android.gms.location.Priority.PRIORITY_BALANCED_POWER_ACCURACY,
                        null
                    ).addOnSuccessListener { loc ->
                        if (loc != null) {
                            Log.i(TAG, "Pulso: sin fixes desde hace ${desde / 1000} s -> enviar posicion puntual")
                            enqueue(loc)
                        } else {
                            Log.i(TAG, "Pulso: el sistema no dio posicion (seguimos esperando)")
                        }
                    }.addOnFailureListener { e ->
                        Log.w(TAG, "Pulso: fallo al pedir posicion (${e.message})")
                    }
                } catch (e: Exception) {
                    Log.w(TAG, "Pulso: ${e.message}")
                }
            }
        }
    }

    private fun stopPulsoParado() {
        pulsoJob?.cancel()
        pulsoJob = null
    }
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

            // ── MODO PRUEBA (v1.8): envío DIRECTO de cada fix ──────────────
            // El intervalo de escucha del GPS (2 s por defecto) ES el intervalo
            // de envío: cada posición que llega se manda al servidor.
            // (La frecuencia adaptativa por velocidad se reintroducirá cuando
            //  validemos que el pipeline 2 s funciona de punta a punta.)
            enqueue(loc)
        }
    }

    /** Modo de transporte actual (vehiculo/andando/parado). */
    @Volatile
    var modoActual: String = "parado"

    /** Último envío EXITOSO al servidor (ms). */
    @Volatile
    var lastOkAtMillis: Long = 0L

    override fun onCreate() {
        super.onCreate()
        notifManager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        createNotificationChannel()

        fusedLocationClient = LocationServices.getFusedLocationProviderClient(this)

        // Detección de actividad (acelerómetro) — se registra al arrancar el trackeo.
        activityRecognitionClient = ActivityRecognition.getClient(this)

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
            ACTION_PULSO_ALARMA -> {
                // F5.31 — la alarma anti-Doze ha despertado al móvil: se manda un
                // punto y se reprograma la siguiente. Si el trackeo ya no está
                // activo, se apaga el servicio (no se resucita solo).
                if (tracking) {
                    pulsoPorAlarma()
                    AlarmReceiver.programar(this)
                    return START_STICKY
                }
                stopSelf()
                return START_NOT_STICKY
            }
            ACTION_ACTIVITY_UPDATE -> {
                // Resultado de Activity Recognition: SOLO actualiza la última
                // actividad; nunca arranca ni detiene el trackeo. Si el servicio
                // ya no trackea (PendingIntent obsoleto), se apaga solo.
                if (tracking) {
                    onActivityDeteccion(intent)
                    return START_STICKY
                }
                stopSelf()
                return START_NOT_STICKY
            }
            // null intent = reinicio del sistema (START_STICKY): reanudar si toca
            else -> startTracking()
        }
        return START_STICKY
    }

    override fun onDestroy() {
        stopPulsoParado()
        // F5.31 — sin trackeo no hay alarma que valga
        AlarmReceiver.cancelar(this)
        sendScope.cancel()
        // Quitar la detección de actividad (deja de llegar el PendingIntent)
        stopActivityRecognition()
        // Parar el bucle de escaneo wifi
        stopWifiHueLoop()
        stopPulsoParado()
        // F5.31 — sin trackeo no hay alarma que valga
        AlarmReceiver.cancelar(this)
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
        // Detección de actividad: opcional — si no hay permiso, degrada en silencio
        startActivityRecognition()
        // Contexto wifi (SSID + huella de redes visibles): opcional, degrada en silencio
        startWifiHueLoop()
        startPulsoParado()
        // F5.31 — alarma que despierta al móvil en Doze (madrugada)
        AlarmReceiver.programar(this)
        reuseLastKnownPosition()
        broadcastStatus()
        // Config remota (OTA): descargar al arrancar + reenviar puntos offline
        sendScope.launch {
            descargarConfigRemota()
            flushOfflineCola()
        }
    }

    /** Descarga la config remota (/api/app_config) y la aplica en caliente. */
    private suspend fun descargarConfigRemota() {
        val token = TrackPrefs.token(this) ?: return
        val cf = TrackPrefs.cfCookies(this)
        try {
            val rb = Request.Builder()
                .url(TrackPrefs.baseUrl(this) + "/api/app_config")
                .addHeader("Authorization", "Bearer $token")
            if (cf.isNotEmpty()) rb.addHeader("Cookie", cf)
            okHttpClient.newCall(rb.get().build()).execute().use { resp ->
                if (resp.isSuccessful) {
                    val body = resp.body?.string().orEmpty()
                    val o = org.json.JSONObject(body)
                    val ver = o.optInt("version_config", 0)
                    if (ver != TrackPrefs.configVersion(this)) {
                        val map = HashMap<String, Any?>()
                        o.keys().forEach { k -> map[k] = o.get(k) }
                        TrackPrefs.saveRemoteConfig(this, map)
                        Log.i(TAG, "Config remota v$ver aplicada: " +
                            "vehículo ${TrackPrefs.cfgIntervaloVehiculoS(this)}s, " +
                            "andando ${TrackPrefs.cfgIntervaloAndandoS(this)}s, " +
                            "parado ${TrackPrefs.cfgIntervaloParadoS(this)}s")
                        // Re-aplicar el intervalo de escucha con la config nueva
                        startLocationUpdates()
                    } else {
                        Log.d(TAG, "Config remota ya actual (v$ver)")
                    }
                } else {
                    Log.w(TAG, "No se pudo descargar config remota: HTTP ${resp.code}")
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "Config remota no disponible: ${e.message}")
        }
    }

    private fun stopTracking() {
        tracking = false
        TrackPrefs.setRunning(this, false)
        try {
            fusedLocationClient.removeLocationUpdates(locationCallback)
        } catch (e: Exception) {
            // no había updates
        }
        // Dejar de pedir detección de actividad (ahorra batería al parar)
        stopActivityRecognition()
        // Parar el bucle de escaneo wifi y olvidar la última huella
        stopWifiHueLoop()
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
        // Si ya había updates (cambio de intervalo/modo), se re-aplican limpiamente
        try {
            fusedLocationClient.removeLocationUpdates(locationCallback)
        } catch (e: Exception) {
            // nada que quitar
        }

        // MODO PRUEBA (v1.8): escucha GPS FIJA a 2 s, pase lo que pase.
        // Cada fix que llega se envía directo (ver locationCallback).
        // La config remota de intervalos por modo queda desactivada hasta
        // validar el pipeline; luego se reintroduce la adaptación.
        val escuchaS = TrackPrefs.intervalSeconds(this).coerceIn(1, 60)
        val intervalMs = escuchaS * 1000L
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

        Log.i(TAG, "GPS modo=$modoActual cada ${intervalMs / 1000} s · prioridad $priority")

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

    // ── Detección de actividad (Google Activity Recognition, acelerómetro) ──
    //
    // requestActivityUpdates: Google entrega (vía PendingIntent → este servicio,
    // acción ACTION_ACTIVITY_UPDATE) la actividad MÁS PROBABLE con su confianza,
    // pidiendo una muestra cada ACTIVITY_INTERVAL_MS (~25 s). Es barato en batería
    // frente a leer el acelerómetro sin parar, y suficiente porque cada punto GPS
    // viaja con la última actividad conocida.
    //
    // Degradación limpia: si el permiso está denegado (o Google Play Services no
    // está disponible), NO se registra nada y los puntos siguen enviándose con
    // act="" y act_conf=0. Nunca lanza excepción hacia arriba.

    private fun startActivityRecognition() {
        // En Android 10+ (API 29) ACTIVITY_RECOGNITION es permiso de runtime.
        // En Android ≤9 el permiso de Google es normal (concedido al instalar).
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q &&
            ContextCompat.checkSelfPermission(
                this, Manifest.permission.ACTIVITY_RECOGNITION
            ) != PackageManager.PERMISSION_GRANTED
        ) {
            Log.w(TAG, "Sin permiso de actividad: se enviará act='' act_conf=0")
            return
        }
        try {
            // Re-registrar limpiamente (el servicio puede arrancarse de nuevo)
            removeActivityUpdatesQuieto()

            val flags = PendingIntent.FLAG_UPDATE_CURRENT or
                (if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                    PendingIntent.FLAG_MUTABLE // Android 12+: el sistema rellena extras
                } else {
                    0
                })
            val pi = PendingIntent.getService(
                this,
                ACTIVITY_PI_REQUEST,
                Intent(this, TrackService::class.java).setAction(ACTION_ACTIVITY_UPDATE),
                flags
            )
            activityPendingIntent = pi
            activityRecognitionClient
                .requestActivityUpdates(ACTIVITY_INTERVAL_MS, pi)
                .addOnFailureListener { e ->
                    Log.w(TAG, "Detección de actividad no disponible: ${e.message}")
                }
            Log.i(TAG, "Detección de actividad registrada cada ${ACTIVITY_INTERVAL_MS / 1000} s")
        } catch (e: SecurityException) {
            Log.w(TAG, "Permiso de actividad denegado: se envía sin act/act_conf")
        } catch (e: Exception) {
            Log.w(TAG, "No se pudo registrar la detección de actividad: ${e.message}")
        }
    }

    /** Desregistra la detección de actividad y descarta el PendingIntent. */
    private fun stopActivityRecognition() {
        val pi = activityPendingIntent ?: return
        activityPendingIntent = null
        if (::activityRecognitionClient.isInitialized) {
            try {
                activityRecognitionClient.removeActivityUpdates(pi)
            } catch (e: Exception) {
                // sin updates que quitar / sin permiso
            }
        }
        pi.cancel()
        // Reiniciar el estado: la próxima muestra lo recalculará.
        lastActivity = ""
        lastActivityConf = 0
    }

    /** Igual que [stopActivityRecognition] pero sin tocar el estado compartido. */
    private fun removeActivityUpdatesQuieto() {
        val pi = activityPendingIntent ?: return
        activityPendingIntent = null
        try {
            activityRecognitionClient.removeActivityUpdates(pi)
        } catch (e: Exception) {
            // nada que quitar
        }
        pi.cancel()
    }

    /**
     * Procesa el resultado de Activity Recognition y actualiza la última
     * actividad conocida con su confianza (0-100).
     */
    private fun onActivityDeteccion(intent: Intent) {
        val result = ActivityRecognitionResult.extractResult(intent) ?: return
        // mostProbableActivity nunca es null en esta versión de Play Services
        // (si no hay certeza devuelve UNKNOWN, que mapea a "").
        val actividad = result.mostProbableActivity
        lastActivity = mapActividad(actividad.type)
        lastActivityConf = actividad.confidence.coerceIn(0, 100)
        Log.i(TAG, "Actividad: '$lastActivity' ($lastActivityConf%) [tipo=${actividad.type}]")
        broadcastStatus()
    }

    /**
     * Mapea el tipo de [DetectedActivity] al contrato del servidor:
     * STILL→'still', ON_FOOT/WALKING→'walk', RUNNING→'run', ON_BICYCLE→'bike',
     * IN_VEHICLE→'vehicle', TILTING→'tilt', UNKNOWN u otro→'' (desconocido).
     */
    private fun mapActividad(tipo: Int): String = when (tipo) {
        DetectedActivity.STILL -> "still"
        DetectedActivity.ON_FOOT, DetectedActivity.WALKING -> "walk"
        DetectedActivity.RUNNING -> "run"
        DetectedActivity.ON_BICYCLE -> "bike"
        DetectedActivity.IN_VEHICLE -> "vehicle"
        DetectedActivity.TILTING -> "tilt"
        else -> ""
    }

    // ── Contexto WiFi (SSID conectado + huella de redes visibles) ────────────
    //
    // Con cada punto se envían tres cosas nuevas:
    //   dev_id    → ANDROID_ID (identificador estable del dispositivo)
    //   wifi_ssid → SSID del wifi al que está CONECTADO el móvil ("" si no hay)
    //   wifi_hue  → huella "<hash8>:<n>" de las redes wifi VISIBLES
    //
    // Degradación limpia: sin permisos de wifi, con el wifi apagado o sin que el
    // sistema devuelva resultados, la app sigue enviando puntos con los tres
    // campos vacíos. Nada de esto puede lanzar excepciones hacia arriba.

    /**
     * Arranca el bucle que recalcula la huella de redes visibles cada ~8 min
     * (WIFI_HUE_INTERVAL_MS). La huella se guarda en [wifiHueActual] y cada
     * punto GPS la reenvía tal cual: no se escanea en cada punto porque en
     * Android moderno startScan() está limitado y sería inútil/gastón.
     */
    private fun startWifiHueLoop() {
        wifiHueJob?.cancel()
        wifiHueJob = sendScope.launch {
            // Primera lectura inmediata: no esperar 8 min al primer punto
            wifiHueActual = escanearYCalcularHue()
            Log.i(TAG, "Huella wifi visible: ${wifiHueActual.ifEmpty { "(vacía)" }}")
            while (coroutineContext.isActive && tracking) {
                delay(WIFI_HUE_INTERVAL_MS)
                wifiHueActual = escanearYCalcularHue()
                Log.i(TAG, "Huella wifi visible: ${wifiHueActual.ifEmpty { "(vacía)" }}")
            }
        }
    }

    /** Cancela el bucle de escaneo y olvida la última huella (se recalcula al re-arrancar). */
    private fun stopWifiHueLoop() {
        wifiHueJob?.cancel()
        wifiHueJob = null
        wifiHueActual = ""
    }

    /**
     * SSID del wifi al que está CONECTADO el móvil, o "" si no hay wifi o no se
     * puede leer (permiso denegado, wifi apagado, SSID oculto...).
     *
     * Es barato (solo consulta el estado del adaptador, no escanea), así que se
     * lee en CADA punto para reflejar cambios de red al momento.
     * getSSID() puede devolver valores basura — "<unknown ssid>", "0x" o vacío —
     * cuando la app no tiene permiso de ubicación o el wifi está apagado: se
     * filtran y se envían como cadena vacía.
     */
    private fun wifiSsidConectado(): String {
        return try {
            val wm = applicationContext.getSystemService(Context.WIFI_SERVICE) as? WifiManager
                ?: return ""
            @Suppress("DEPRECATION")
            val crudo = wm.connectionInfo?.ssid ?: return ""
            val ssid = crudo.trim().trim('"')
            when {
                ssid.isEmpty() -> ""
                crudo.equals("<unknown ssid>", ignoreCase = true) -> ""
                ssid.equals("<unknown ssid>", ignoreCase = true) -> ""
                ssid == "0x" -> ""
                else -> ssid
            }
        } catch (e: SecurityException) {
            "" // sin permiso: se envía wifi_ssid vacío
        } catch (e: Exception) {
            Log.w(TAG, "No se pudo leer el SSID conectado: ${e.message}")
            ""
        }
    }

    /**
     * Intenta un escaneo wifi (best-effort) y devuelve la huella resultante.
     *
     * startScan() puede fallar o estar limitado (throttling) en segundo plano:
     * da igual — getScanResults() devuelve los últimos resultados que el
     * sistema tenga cacheados (Android escanea por su cuenta para el
     * posicionamiento). Cualquier fallo → "" y el envío de puntos continúa.
     */
    @Suppress("DEPRECATION") // startScan() está deprecado en API 28+, pero sigue siendo la vía pública
    private suspend fun escanearYCalcularHue(): String {
        return try {
            val wm = applicationContext.getSystemService(Context.WIFI_SERVICE) as? WifiManager
                ?: return ""
            try {
                // Solo se usa para intentar refrescar; su fallo NO se propaga.
                wm.startScan()
            } catch (e: SecurityException) {
                Log.d(TAG, "startScan() sin permiso: se usan los resultados cacheados")
            } catch (e: Exception) {
                Log.d(TAG, "startScan() rechazado (limitado): se usan los resultados cacheados")
            }
            // Margen para que un escaneo aceptado rellene la lista
            delay(WIFI_SCAN_SETTLE_MS)
            calcularWifiHue()
        } catch (e: Exception) {
            Log.w(TAG, "Escaneo wifi no disponible: ${e.message}")
            ""
        }
    }

    /**
     * Huella de las redes wifi VISIBLES: "<hash8>:<n>".
     *   hash8 = 8 primeros caracteres hex de sha1(BSSID de las 5 redes más
     *           fuertes, ordenados — el orden del escaneo no es estable)
     *   n     = número total de redes devueltas por el escaneo
     *
     * ¿Por qué una HUELLA y no las redes completas?
     *  1. Privacidad: el BSSID es un identificador permanente del router; una
     *     lista de BSSID en claro permite geolocalizar al móvil cruzando bases
     *     de datos públicas de wifi. Un sha1 truncado a 8 hex no es reversible
     *     para este uso: no reconstruye la red ni su posición.
     *  2. Tamaño: con un punto cada 2 s, mandar 20-50 redes completas
     *     multiplicaría el tráfico y llenaría la BD sin aportar nada. Al
     *     servidor solo le interesa saber si es LA MISMA huella o ha CAMBIADO
     *     (indicio de que el móvil cambió de sitio o de red).
     *
     * Sin resultados (sin permiso, ubicación apagada, escaneo limitado) → "".
     */
    private fun calcularWifiHue(): String {
        return try {
            val wm = applicationContext.getSystemService(Context.WIFI_SERVICE) as? WifiManager
                ?: return ""
            val resultados: List<ScanResult> = wm.scanResults ?: return ""
            if (resultados.isEmpty()) return ""
            // Las 5 redes MÁS FUERTES: se ordenan por nivel de señal, se toman 5
            // y luego se ordenan por BSSID para que la huella sea estable e
            // independiente del orden en que el sistema devuelva el escaneo.
            val bssids = resultados.asSequence()
                .filter { !it.BSSID.isNullOrBlank() }
                .sortedByDescending { it.level }
                .take(5)
                .map { it.BSSID.lowercase(Locale.US) }
                .sorted()
                .toList()
            if (bssids.isEmpty()) return ""
            val digest = MessageDigest.getInstance("SHA-1")
                .digest(bssids.joinToString("|").toByteArray(Charsets.UTF_8))
            val hash8 = digest.joinToString("") { "%02x".format(it.toInt() and 0xFF) }.take(8)
            "$hash8:${resultados.size}"
        } catch (e: SecurityException) {
            Log.w(TAG, "Sin permiso para leer las redes wifi visibles: huella vacía")
            ""
        } catch (e: Exception) {
            Log.w(TAG, "No se pudo calcular la huella wifi: ${e.message}")
            ""
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
        var ultimoOk = false
        while (coroutineContext.isActive && tracking) {
            val loc = synchronized(queueLock) { queue.pollFirst() } ?: break
            val ok = sendWithRetry(loc)
            ultimoOk = ok
            synchronized(queueLock) {
                // Si falló por red, sendWithRetry ya guardó el punto en la cola
                // offline persistente (guardarOffline) → NO re-encolar aquí
                // (evita duplicados y bucles infinitos sin conexión).
                pendingCount = queue.size
            }
            if (ok && tracking && pendingCount == 0) updateNotification()
            // Sin red: parar el worker hasta el próximo fix GPS (que reintentará)
            if (!ok) break
        }
        workerRunning.set(false)
        // Reenviar la cola offline SOLO si el último envío fue OK (hay red).
        // Si el envío falló, flushOfflineCola también fallaría y duplicaría
        // puntos (cada reintento guardaría copias). El próximo fix con red
        // disparará el flush.
        if (tracking && ultimoOk) flushOfflineCola()
    }

    /** Reenvía los puntos guardados sin cobertura (cola offline persistente). */
    private suspend fun flushOfflineCola() {
        if (!TrackPrefs.cfgColaOffline(this)) return
        while (coroutineContext.isActive && tracking) {
            val p = TrackPrefs.colaOfflineRemoveFirst(this) ?: break
            val loc = Location("offline").apply {
                latitude = p[1]; longitude = p[2]
                accuracy = p[3].toFloat()
                speed = p[4].toFloat()
                // Restaurar la hora EXACTA del fix original (buildJson la usa)
                time = p[0].toLong()
            }
            val ok = sendWithRetry(loc, guardarSiFalla = false)
            if (!ok) {
                // Sin red todavía: devolver el punto a la cola y esperar
                TrackPrefs.colaOfflineAdd(
                    this, p[0].toLong(), p[1], p[2], p[3].toFloat(), p[4].toFloat()
                )
                break
            }
        }
        broadcastStatus()
    }

    /** Guarda un punto en la cola offline (sin cobertura → no se pierde). */
    private fun guardarOffline(loc: Location) {
        if (!TrackPrefs.cfgColaOffline(this)) return
        // Hora EXACTA del fix (loc.time), no la de guardado
        val tsMs = if (loc.time > 0) loc.time else System.currentTimeMillis()
        // Dedupe: si el último punto de la cola ya tiene este ts exacto (mismo
        // fix repetido por el GPS), no guardar otra copia.
        val cola = TrackPrefs.colaOffline(this)
        if (cola.isNotEmpty() && cola.last()[0].toLong() == tsMs) {
            Log.d(TAG, "Fix duplicado (ts=$tsMs): se omite guardar offline")
            return
        }
        val ok = TrackPrefs.colaOfflineAdd(
            this,
            tsMs,
            loc.latitude,
            loc.longitude,
            loc.accuracy,
            if (loc.hasSpeed()) loc.speed else 0f
        )
        Log.w(TAG, "Sin conexión: punto guardado offline (cola: ${TrackPrefs.colaOfflineSize(this)})")
        broadcastStatus()
    }

    /**
     * POST JSON a <base>/track con Authorization: Bearer <token>.
     * OkHttp con timeout de 8 s y 1 reintento (2 intentos en total).
     * Devuelve false solo si ambos intentos fallaron por red (se re-encola).
     * Un 401 (token caducado) no se reintenta: se limpia la sesión y se
     * avisa a la UI para volver al login.
     */
    private suspend fun sendWithRetry(loc: Location, guardarSiFalla: Boolean = true): Boolean {
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
                lastOkAtMillis = lastSendAtMillis
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
        Log.w(TAG, "Envío fallido definitivo — guardando offline si procede")
        if (guardarSiFalla) guardarOffline(loc)
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
        // Hora EXACTA del fix GPS (loc.time) — no la hora de envío. Así los
        // puntos reenviados desde la cola offline conservan su hora real.
        val tsMs = if (loc.time > 0) loc.time else System.currentTimeMillis()
        return JSONObject()
            .put("lat", loc.latitude)
            .put("lon", loc.longitude)
            .put("ts", tsMs / 1000.0)
            .put("acc", if (loc.hasAccuracy()) loc.accuracy.toDouble() else 0.0)
            .put("vel", if (loc.hasSpeed()) loc.speed.toDouble() else 0.0)
            // Actividad del móvil (acelerómetro): contrato fijo del servidor.
            // act = "" si se desconoce; act_conf = 0 en ese caso.
            .put("act", lastActivity)
            .put("act_conf", lastActivityConf)
            .put("dev", deviceId())
            // Identificador ESTABLE del dispositivo (ANDROID_ID): distingue
            // móviles aunque compartan fabricante+modelo. "" si no se pudo leer.
            .put("dev_id", androidId)
            // Wifi: SSID al que está conectado (se lee ahora, es barato) y
            // huella de las redes visibles (última calculada, ~8 min). "" si no
            // hay wifi/datos — el punto se envía igual.
            .put("wifi_ssid", wifiSsidConectado())
            .put("wifi_hue", wifiHueActual)
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
