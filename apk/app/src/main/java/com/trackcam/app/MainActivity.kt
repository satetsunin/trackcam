package com.trackcam.app

import android.Manifest
import android.content.BroadcastReceiver
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.text.format.DateUtils
import android.view.View
import android.widget.AdapterView
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.Spinner
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import java.util.Locale

class MainActivity : AppCompatActivity() {

    companion object {
        private const val RC_LOCATION = 1001
        private const val RC_NOTIF = 1002
        private const val RC_BG_LOCATION = 1003
    }

    private lateinit var spInterval: Spinner
    private lateinit var btnToggle: Button
    private lateinit var tvSesion: TextView
    private lateinit var tvEstado: TextView
    private lateinit var tvConexion: TextView
    private lateinit var tvPosicion: TextView
    private lateinit var tvBateria: TextView
    private lateinit var btnBateria: Button
    private lateinit var btnExencion: Button
    private lateinit var btnLogout: Button

    private val intervalValues = intArrayOf(1, 2, 5, 10, 30, 60)

    /** Recibe los broadcasts del servicio: refresca la UI o vuelve al login (401). */
    private val statusReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            when (intent.action) {
                TrackService.ACTION_UNAUTHORIZED ->
                    goToLogin(LoginActivity.MOTIVO_SESION_EXPIRADA)
                TrackService.ACTION_STATUS -> refreshUi()
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        spInterval = findViewById(R.id.spInterval)
        btnToggle = findViewById(R.id.btnToggle)
        tvSesion = findViewById(R.id.tvSesion)
        tvEstado = findViewById(R.id.tvEstado)
        tvConexion = findViewById(R.id.tvConexion)
        tvPosicion = findViewById(R.id.tvPosicion)
        tvBateria = findViewById(R.id.tvBateria)
        btnBateria = findViewById(R.id.btnBateria)
        btnExencion = findViewById(R.id.btnExencion)
        btnLogout = findViewById(R.id.btnLogout)

        // Selector de intervalo: 1/2/5/10/30/60 s
        val labels = resources.getStringArray(R.array.intervalos).toList()
        spInterval.adapter = ArrayAdapter(
            this,
            android.R.layout.simple_spinner_item,
            labels
        ).apply {
            setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item)
        }
        val current = TrackPrefs.intervalSeconds(this)
        val pos = intervalValues.indexOf(current).let { if (it < 0) 2 else it } // 2 = 5 s
        spInterval.setSelection(pos)

        btnToggle.setOnClickListener {
            if (TrackService.tracking) {
                stopTracking()
            } else {
                startTrackingFlow()
            }
        }
        btnBateria.setOnClickListener { openBatterySettings() }
        btnExencion.setOnClickListener { requestIgnoreBatteryOptimization() }
        btnLogout.setOnClickListener { logout() }

        // Si se cambia el intervalo con el trackeo activo, se aplica al momento
        spInterval.onItemSelectedListener = object : AdapterView.OnItemSelectedListener {
            override fun onItemSelected(
                parent: AdapterView<*>?,
                view: View?,
                position: Int,
                id: Long
            ) {
                if (!TrackService.tracking) return
                val newInterval =
                    intervalValues[position.coerceIn(0, intervalValues.size - 1)]
                if (newInterval != TrackPrefs.intervalSeconds(this@MainActivity)) {
                    TrackPrefs.setIntervalSeconds(this@MainActivity, newInterval)
                    startService(
                        Intent(this@MainActivity, TrackService::class.java)
                            .setAction(TrackService.ACTION_START)
                    )
                    Toast.makeText(this@MainActivity, R.string.toast_intervalo, Toast.LENGTH_SHORT)
                        .show()
                }
            }

            override fun onNothingSelected(parent: AdapterView<*>?) {
                // sin acción
            }
        }

        setupBatteryUi()
    }

    override fun onStart() {
        super.onStart()
        // Sin sesión (login caducado o logout) → pantalla de login
        if (TrackPrefs.token(this).isNullOrBlank()) {
            goToLogin(null)
            return
        }
        ContextCompat.registerReceiver(
            this,
            statusReceiver,
            IntentFilter().apply {
                addAction(TrackService.ACTION_STATUS)
                addAction(TrackService.ACTION_UNAUTHORIZED)
            },
            ContextCompat.RECEIVER_NOT_EXPORTED
        )
        refreshUi()
    }

    override fun onStop() {
        super.onStop()
        try {
            unregisterReceiver(statusReceiver)
        } catch (e: IllegalArgumentException) {
            // ya no estaba registrado
        }
    }

    // ── Navegación login ───────────────────────────────────────────────────

    private fun goToLogin(motivo: String?) {
        if (isFinishing) return
        val intent = Intent(this, LoginActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK)
        if (motivo != null) intent.putExtra(LoginActivity.EXTRA_MOTIVO, motivo)
        startActivity(intent)
        finish()
    }

    /** Cierra la sesión, detiene el trackeo y vuelve al login. */
    private fun logout() {
        try {
            startService(
                Intent(this, TrackService::class.java)
                    .setAction(TrackService.ACTION_STOP)
            )
        } catch (e: Exception) {
            // el servicio no estaba corriendo
        }
        TrackPrefs.clearSession(this)
        goToLogin(null)
    }

    // ── Arranque / parada del trackeo ───────────────────────────────────────

    private fun startTrackingFlow() {
        if (!hasLocationPermission()) {
            // Paso 1: ubicación "mientras se usa"
            ActivityCompat.requestPermissions(
                this,
                arrayOf(
                    Manifest.permission.ACCESS_FINE_LOCATION,
                    Manifest.permission.ACCESS_COARSE_LOCATION
                ),
                RC_LOCATION
            )
            return
        }
        // Paso 2 (Android 10+): ubicación "permitir siempre" (background)
        maybeRequestBackgroundLocation()
        maybeRequestNotificationPermission()
        maybeRequestInstallPackages()
        doStart()
    }

    /** Pide ACCESS_BACKGROUND_LOCATION en dos pasos (Android 10+):
     *  primero el usuario debe dar "mientras se usa", luego este diálogo. */
    private fun maybeRequestBackgroundLocation() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) return
        if (ContextCompat.checkSelfPermission(
                this, Manifest.permission.ACCESS_BACKGROUND_LOCATION
            ) == PackageManager.PERMISSION_GRANTED) return
        if (!ActivityCompat.shouldShowRequestPermissionRationale(
                this, Manifest.permission.ACCESS_FINE_LOCATION
            ) && TrackPrefs.askedBackgroundOnce(this)) {
            // Ya lo pedimos antes y lo denegó: llevarle a ajustes
            Toast.makeText(this, R.string.toast_bg_location, Toast.LENGTH_LONG).show()
            try {
                startActivity(
                    Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS)
                        .setData(Uri.parse("package:$packageName"))
                )
            } catch (e: Exception) { /* sin ajustes */ }
            return
        }
        TrackPrefs.setAskedBackgroundOnce(this, true)
        ActivityCompat.requestPermissions(
            this,
            arrayOf(Manifest.permission.ACCESS_BACKGROUND_LOCATION),
            RC_BG_LOCATION
        )
    }

    /** Para OTA: si no puede instalar apps de orígenes desconocidos, avisar
     *  y ofrecer abrir el ajuste (Redmi: "Instalar apps desconocidas"). */
    private fun maybeRequestInstallPackages() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O &&
            !packageManager.canRequestPackageInstalls()
        ) {
            try {
                startActivity(
                    Intent(
                        Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                        Uri.parse("package:$packageName")
                    )
                )
            } catch (e: Exception) {
                Toast.makeText(this, R.string.toast_ota_permiso, Toast.LENGTH_LONG).show()
            }
        }
    }

    private fun hasLocationPermission(): Boolean =
        ContextCompat.checkSelfPermission(
            this, Manifest.permission.ACCESS_FINE_LOCATION
        ) == PackageManager.PERMISSION_GRANTED ||
            ContextCompat.checkSelfPermission(
                this, Manifest.permission.ACCESS_COARSE_LOCATION
            ) == PackageManager.PERMISSION_GRANTED

    private fun maybeRequestNotificationPermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(
                this, Manifest.permission.POST_NOTIFICATIONS
            ) != PackageManager.PERMISSION_GRANTED
        ) {
            ActivityCompat.requestPermissions(
                this,
                arrayOf(Manifest.permission.POST_NOTIFICATIONS),
                RC_NOTIF
            )
        }
    }

    private fun doStart() {
        if (TrackPrefs.token(this).isNullOrBlank()) {
            goToLogin(LoginActivity.MOTIVO_SESION_EXPIRADA)
            return
        }
        val interval =
            intervalValues[spInterval.selectedItemPosition.coerceIn(0, intervalValues.size - 1)]
        TrackPrefs.setIntervalSeconds(this, interval)

        val intent = Intent(this, TrackService::class.java)
            .setAction(TrackService.ACTION_START)
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                startForegroundService(intent)
            } else {
                startService(intent)
            }
        } catch (e: Exception) {
            Toast.makeText(this, R.string.err_start, Toast.LENGTH_LONG).show()
            return
        }
        refreshUi()
        maybeWarnBattery()
    }

    private fun stopTracking() {
        try {
            startService(
                Intent(this, TrackService::class.java)
                    .setAction(TrackService.ACTION_STOP)
            )
        } catch (e: Exception) {
            // el servicio no estaba corriendo
        }
        refreshUi()
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == RC_LOCATION) {
            if (grantResults.isNotEmpty() &&
                grantResults.any { it == PackageManager.PERMISSION_GRANTED }
            ) {
                maybeRequestNotificationPermission()
                doStart()
            } else {
                Toast.makeText(this, R.string.err_location_perm, Toast.LENGTH_LONG).show()
            }
        }
    }

    // ── Refresco de la pantalla ─────────────────────────────────────────────

    private fun refreshUi() {
        val tracking = TrackService.tracking
        btnToggle.text = getString(if (tracking) R.string.btn_stop else R.string.btn_start)

        val user = TrackPrefs.username(this).orEmpty()
        tvSesion.text = getString(R.string.sesion_info, user, serverHost())

        tvEstado.text = when {
            !tracking -> getString(R.string.estado_detenido)
            TrackService.lastSendAtMillis == 0L -> getString(R.string.estado_enviando)
            else -> getString(R.string.estado_trackeando, formatAgo(TrackService.lastSendAtMillis))
        }

        tvConexion.text = when {
            TrackService.lastOk == null -> getString(R.string.conexion_ninguno)
            TrackService.lastOk == true ->
                getString(R.string.conexion_ok, formatTime(TrackService.lastSendAtMillis))
            else -> getString(
                R.string.conexion_fail,
                formatTime(TrackService.lastSendAtMillis),
                TrackService.pendingCount
            )
        }

        val lat = TrackService.lastLat
        val lon = TrackService.lastLon
        if (lat != null && lon != null) {
            tvPosicion.text = getString(
                R.string.posicion,
                String.format(Locale.US, "%.6f", lat),
                String.format(Locale.US, "%.6f", lon),
                TrackService.lastAcc?.toInt() ?: 0,
                TrackService.lastVel?.toInt() ?: 0
            )
        } else {
            tvPosicion.text = getString(R.string.posicion_nula)
        }

        updateBatteryStatus()
    }

    /** "track.satetsunin.com" a partir de la URL base guardada. */
    private fun serverHost(): String =
        TrackPrefs.baseUrl(this)
            .removePrefix("https://")
            .removePrefix("http://")
            .substringBefore('/')

    private fun formatAgo(epochMillis: Long): String {
        val secs = (System.currentTimeMillis() - epochMillis) / 1000L
        return if (secs < 1) getString(R.string.ahora_mismo)
        else DateUtils.formatElapsedTime(secs)
    }

    private fun formatTime(epochMillis: Long): String =
        java.text.SimpleDateFormat("HH:mm:ss", Locale.getDefault())
            .format(java.util.Date(epochMillis))

    // ── Mitigación de batería (crítico en Redmi/Xiaomi) ─────────────────────

    private fun setupBatteryUi() {
        btnBateria.text = getString(
            if (isXiaomi()) R.string.btn_bateria_xiaomi else R.string.btn_bateria_generico
        )
        updateBatteryStatus()
    }

    private fun updateBatteryStatus() {
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        val ignoring = pm.isIgnoringBatteryOptimizations(packageName)
        tvBateria.text = getString(if (ignoring) R.string.bateria_ok else R.string.bateria_activa)
        tvBateria.setTextColor(
            ContextCompat.getColor(this, if (ignoring) R.color.ok_green else R.color.warn_red)
        )
    }

    private fun isXiaomi(): Boolean {
        val m = Build.MANUFACTURER.lowercase(Locale.ROOT)
        return m.contains("xiaomi") || m.contains("redmi") || m.contains("poco")
    }

    /**
     * Abre los ajustes de batería adecuados para este fabricante:
     *  - Xiaomi/Redmi/Poco: primero Autostart de Security Center, luego
     *    PowerSettings y por último el Security Center genérico.
     *  - Resto: lista de apps con optimización de batería del sistema.
     */
    private fun openBatterySettings() {
        updateBatteryStatus()
        if (isXiaomi()) {
            if (!openXiaomiAutostart() && !openXiaomiPowerSettings() && !openXiaomiSecurityApp()) {
                openIgnoreSettings()
            }
        } else {
            openIgnoreSettings()
        }
    }

    private fun openXiaomiAutostart(): Boolean = try {
        startActivity(
            Intent().setComponent(
                ComponentName(
                    "com.miui.securitycenter",
                    "com.miui.permcenter.autostart.AutoStartManagementActivity"
                )
            )
        )
        true
    } catch (e: Exception) {
        false
    }

    private fun openXiaomiPowerSettings(): Boolean = try {
        startActivity(
            Intent().setComponent(
                ComponentName(
                    "com.miui.securitycenter",
                    "com.miui.powercenter.PowerSettings"
                )
            )
        )
        true
    } catch (e: Exception) {
        false
    }

    private fun openXiaomiSecurityApp(): Boolean = try {
        startActivity(Intent().setPackage("com.miui.securitycenter"))
        true
    } catch (e: Exception) {
        false
    }

    private fun openIgnoreSettings() {
        try {
            startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
        } catch (e: Exception) {
            Toast.makeText(this, R.string.err_settings, Toast.LENGTH_LONG).show()
        }
    }

    /** Pide la exención estándar de Android (diálogo de "No optimizar"). */
    private fun requestIgnoreBatteryOptimization() {
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        if (pm.isIgnoringBatteryOptimizations(packageName)) {
            Toast.makeText(this, R.string.bateria_ya_exenta, Toast.LENGTH_LONG).show()
            return
        }
        try {
            startActivity(
                Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS)
                    .setData(Uri.parse("package:$packageName"))
            )
        } catch (e: Exception) {
            openIgnoreSettings()
        }
    }

    private fun maybeWarnBattery() {
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        if (!pm.isIgnoringBatteryOptimizations(packageName)) {
            Toast.makeText(this, R.string.toast_bateria, Toast.LENGTH_LONG).show()
        }
    }
}
