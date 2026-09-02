package com.trackcam.app

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build
import android.util.Log

/**
 * Relanza TrackService tras el arranque del teléfono (o tras actualizar la app)
 * si el usuario tenía el trackeo activo. Necesita RECEIVE_BOOT_COMPLETED.
 */
class BootReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        val action = intent.action ?: return

        val relevant = action == Intent.ACTION_BOOT_COMPLETED ||
            action == "android.intent.action.QUICKBOOT_POWERON" || // arranque rápido MIUI/Samsung
            action == Intent.ACTION_MY_PACKAGE_REPLACED // tras actualizar la app

        if (!relevant) return
        if (!TrackPrefs.running(context)) return
        // Fase 5: sin token de sesión no se puede reanudar el trackeo
        if (TrackPrefs.token(context).isNullOrBlank()) {
            TrackPrefs.setRunning(context, false)
            return
        }

        Log.i(TAG, "Reiniciando TrackService tras $action")

        val serviceIntent = Intent(context, TrackService::class.java)
            .setAction(TrackService.ACTION_START)

        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(serviceIntent)
            } else {
                context.startService(serviceIntent)
            }
        } catch (e: Exception) {
            // Por ejemplo ForegroundServiceStartNotAllowedException: lo intentamos
            // de nuevo la próxima vez que abran la app. No debe tumbar el sistema.
            Log.e(TAG, "No se pudo relanzar el servicio: ${e.message}")
        }
    }

    companion object {
        private const val TAG = "TrackCamBoot"
    }
}
