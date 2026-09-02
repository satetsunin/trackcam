# Reglas ProGuard/R8 de TrackCam.
# El release no minifica (isMinifyEnabled = false), así que no se necesitan reglas
# especiales. Si algún día se activa minify, mantener estas excepciones:

# okhttp
-dontwarn okhttp3.**
-dontwarn okio.**

# play-services-location (guarda los modelos de datos)
-keep class com.google.android.gms.location.** { *; }
