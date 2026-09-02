package com.trackcam.app

import android.app.AlertDialog
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.widget.Toast
import androidx.core.content.FileProvider
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.io.File
import java.util.concurrent.TimeUnit

/**
 * Actualización OTA: consulta <base>/api/apk/version; si el servidor tiene un
 * versionCode mayor que el instalado, descarga el APK y lanza la instalación.
 * Incluye las cookies de Cloudflare (si el túnel está protegido con Access).
 */
object OtaUpdater {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private val http = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)   // descarga larga
        .writeTimeout(60, TimeUnit.SECONDS)
        .build()

    /** true si hay una versión más nueva en el servidor (comprobación rápida). */
    fun hayActualizacion(ctx: Context): Boolean {
        val token = TrackPrefs.token(ctx) ?: return false
        return try {
            val rb = Request.Builder()
                .url(TrackPrefs.baseUrl(ctx) + "/api/apk/version")
                .addHeader("Authorization", "Bearer $token")
            val cf = TrackPrefs.cfCookies(ctx)
            if (cf.isNotEmpty()) rb.addHeader("Cookie", cf)
            http.newCall(rb.get().build()).execute().use { resp ->
                if (!resp.isSuccessful) return false
                val o = JSONObject(resp.body?.string().orEmpty())
                val serverCode = o.optInt("versionCode", 0)
                serverCode > BuildConfig.VERSION_CODE
            }
        } catch (e: Exception) {
            false // sin red / sin auth: silencioso
        }
    }

    /** Comprueba y si hay nueva versión pregunta al usuario. */
    fun comprobar(ctx: Context, silenciosoSiActual: Boolean = false) {
        val token = TrackPrefs.token(ctx) ?: return
        scope.launch {
            val info = withContext(Dispatchers.IO) { consultarVersion(ctx, token) }
            if (info == null) {
                if (!silenciosoSiActual) {
                    Toast.makeText(ctx, "No se pudo consultar el servidor", Toast.LENGTH_SHORT).show()
                }
                return@launch
            }
            val (serverCode, serverName, tam) = info
            if (serverCode <= BuildConfig.VERSION_CODE) {
                if (!silenciosoSiActual) {
                    Toast.makeText(
                        ctx,
                        "Ya tienes la última versión (${BuildConfig.VERSION_NAME})",
                        Toast.LENGTH_SHORT
                    ).show()
                }
                return@launch
            }
            // Hay versión nueva → diálogo
            AlertDialog.Builder(ctx)
                .setTitle("📲 Nueva versión disponible")
                .setMessage("TrackCam ${serverName} (tamaño ${tam / 1048576} MB).\n" +
                    "Tu versión actual: ${BuildConfig.VERSION_NAME}.\n\n¿Descargar e instalar?")
                .setPositiveButton("Descargar e instalar") { _, _ -> descargarEInstalar(ctx, token) }
                .setNegativeButton("Ahora no", null)
                .show()
        }
    }

    private suspend fun consultarVersion(ctx: Context, token: String): Triple<Int, String, Long>? {
        return try {
            val rb = Request.Builder()
                .url(TrackPrefs.baseUrl(ctx) + "/api/apk/version")
                .addHeader("Authorization", "Bearer $token")
            val cf = TrackPrefs.cfCookies(ctx)
            if (cf.isNotEmpty()) rb.addHeader("Cookie", cf)
            http.newCall(rb.get().build()).execute().use { resp ->
                if (!resp.isSuccessful) return null
                val o = JSONObject(resp.body?.string().orEmpty())
                Triple(o.optInt("versionCode", 0), o.optString("versionName", "?"),
                    o.optLong("tam", 0))
            }
        } catch (e: Exception) {
            null
        }
    }

    /** Descarga el APK a cacheDir/apk/ y lanza el instalador (ACTION_VIEW). */
    private fun descargarEInstalar(ctx: Context, token: String) {
        scope.launch {
            val apkFile = withContext(Dispatchers.IO) {
                descargar(ctx, token)
            }
            if (apkFile == null) {
                Toast.makeText(ctx, "Fallo al descargar la actualización", Toast.LENGTH_LONG).show()
                return@launch
            }
            instalar(ctx, apkFile)
        }
    }

    private fun descargar(ctx: Context, token: String): File? {
        return try {
            val dir = File(ctx.cacheDir, "apk").apply { mkdirs() }
            val destino = File(dir, "trackcam-ota.apk")
            val rb = Request.Builder()
                .url(TrackPrefs.baseUrl(ctx) + "/api/apk/download")
                .addHeader("Authorization", "Bearer $token")
            val cf = TrackPrefs.cfCookies(ctx)
            if (cf.isNotEmpty()) rb.addHeader("Cookie", cf)
            http.newCall(rb.get().build()).execute().use { resp ->
                if (!resp.isSuccessful) return null
                resp.body?.byteStream()?.use { input ->
                    destino.outputStream().use { output -> input.copyTo(output) }
                }
            }
            if (destino.length() > 1_000_000) destino else null // sanity: >1 MB
        } catch (e: Exception) {
            null
        }
    }

    private fun instalar(ctx: Context, apk: File) {
        try {
            val uri: Uri = FileProvider.getUriForFile(
                ctx, ctx.packageName + ".fileprovider", apk
            )
            val intent = Intent(Intent.ACTION_VIEW).apply {
                setDataAndType(uri, "application/vnd.android.package-archive")
                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            }
            ctx.startActivity(intent)
        } catch (e: Exception) {
            Toast.makeText(ctx, "No se pudo abrir el instalador: ${e.message}",
                Toast.LENGTH_LONG).show()
        }
    }
}
