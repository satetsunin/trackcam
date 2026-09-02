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
import android.widget.EditText
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
    }

    private lateinit var etUrl: EditText
    private lateinit var spInterval: Spinner
    private lateinit var btnToggle: Button
    private lateinit var tvEstado: TextView
    private lateinit var tvConexion: TextView
    private lateinit var tvPosicion: TextView
    private lateinit var tvBateria: TextView
    private lateinit var btnBateria: Button
    private lateinit var btnExencion: Button

    private val intervalValues = intArrayOf(1, 2, 5, 10, 30, 60)

    /** Recibe los broadcasts de estado del servicio y refresca la pantalla. */
    private val statusReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            if (intent.action == TrackService.ACTION_STATUS) refreshUi()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        etUrl = findViewById(R.id.etUrl)
        spInterval = findViewById(R.id.spInterval)
        btnToggle = findViewById(R.id.btnToggle)
        tvEstado = findViewById(R.id.tvEstado)
        tvConexion = findViewById(R.id.tvConexion)
        tvPosicion = findViewById(R.id.tvPosicion)
        tvBateria = findViewById(R.id.tvBateria)
        btnBateria = findViewById(R.id.btnBateria)
        btnExencion = findViewById(R.id.btnExencion)

        etUrl.setText(TrackPrefs.serverUrl(this))

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
        ContextCompat.registerReceiver(
            this,
            statusReceiver,
            IntentFilter(TrackService.ACTION_STATUS),
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

    // ── Arranque / parada del trackeo ───────────────────────────────────────

    private fun startTrackingFlow() {
        if (!hasLocationPermission()) {
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
        maybeRequestNotificationPermission()
        doStart()
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
        val url = etUrl.text?.toString()?.trim().orEmpty()
        if (url.isEmpty()) {
            etUrl.error = getString(R.string.err_url)
            return
        }
        TrackPrefs.setServerUrl(this, url)
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
