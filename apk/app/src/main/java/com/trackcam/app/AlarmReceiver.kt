package com.trackcam.app

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.SystemClock
import android.util.Log

/**
 * F5.31 — ALARMA ANTI-DOZE.
 *
 * Medido en los datos reales: todos los huecos largos de la traza empiezan de
 * madrugada (09-09 a las 01:04 con 9 h sin datos, 11-09 a las 00:16, 19-09 a las
 * 00:49, 20-09 a las 00:22 con 3 h 20 min). El PC y el servidor estaban
 * encendidos, así que el que deja de enviar es el MÓVIL: Android entra en Doze
 * (ahorro de batería) cuando está quieto y de noche, y suspende las
 * actualizaciones de ubicación y los temporizadores normales.
 *
 * El pulso por corrutina (F5.27b) no basta en Doze porque los `delay` se
 * agrupan y se retrasan. Las alarmas con `setAndAllowWhileIdle` SÍ despiertan el
 * dispositivo en Doze (es el mecanismo que Android reserva para esto), así que
 * cada ALARMA_MIN minutos se garantiza un punto.
 */
class AlarmReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent?) {
        Log.i(TAG, "alarma anti-Doze: despertando para pedir un punto")
        val servicio = Intent(context, TrackService::class.java).apply {
            action = TrackService.ACTION_PULSO_ALARMA
        }
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(servicio)
            } else {
                context.startService(servicio)
            }
        } catch (e: Exception) {
            Log.w(TAG, "no se pudo arrancar el servicio desde la alarma: ${e.message}")
        }
    }

    companion object {
        private const val TAG = "TrackCamAlarma"

        /** Cada cuánto se fuerza un punto aunque el móvil esté dormido. */
        const val ALARMA_MIN = 15L

        private const val RC = 4711

        private fun pending(context: Context): PendingIntent = PendingIntent.getBroadcast(
            context, RC, Intent(context, AlarmReceiver::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or
                (if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M)
                    PendingIntent.FLAG_IMMUTABLE else 0)
        )

        /** Programa la próxima alarma (válida en Doze, sin permisos extra). */
        fun programar(context: Context) {
            val am = context.getSystemService(Context.ALARM_SERVICE) as? AlarmManager ?: return
            val cuando = SystemClock.elapsedRealtime() + ALARMA_MIN * 60_000L
            try {
                // setAndAllowWhileIdle (no EXACTA a propósito): funciona en Doze y
                // NO necesita el permiso SCHEDULE_EXACT_ALARM de Android 12+, que
                // exigiría una concesión manual más.
                am.setAndAllowWhileIdle(AlarmManager.ELAPSED_REALTIME_WAKEUP, cuando, pending(context))
                Log.i(TAG, "alarma programada en $ALARMA_MIN min")
            } catch (e: Exception) {
                Log.w(TAG, "no se pudo programar la alarma: ${e.message}")
            }
        }

        fun cancelar(context: Context) {
            val am = context.getSystemService(Context.ALARM_SERVICE) as? AlarmManager ?: return
            try {
                am.cancel(pending(context))
                Log.i(TAG, "alarma cancelada")
            } catch (e: Exception) {
                Log.w(TAG, "no se pudo cancelar la alarma: ${e.message}")
            }
        }
    }
}
