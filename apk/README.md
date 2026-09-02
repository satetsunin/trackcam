# TrackCam — APK Android (cliente GPS)

App compañera del servidor TrackCam. Obtiene la ubicación del teléfono con
**FusedLocationProvider** (intervalo configurable: 1/2/5/10/30/60 s, por defecto 5 s) y la
envía **al momento** por POST JSON al servidor:

```
{ "lat": 43.263012, "lon": -2.935001, "ts": 1756781234.567, "acc": 4.2, "vel": 1.3, "dev": "Redmi_Note_12_Pro" }
```

Servidor por defecto: **https://track.satetsunin.com/track** (túnel Cloudflare del usuario).
Para pruebas en LAN se puede poner **http://192.168.1.236:8099/track** (la app permite tráfico
HTTP en claro, necesario para la IP local).

Diseñada para funcionar **24/7**: servicio en primer plano con notificación persistente
("TrackCam activo"), `START_STICKY`, wake lock, reinicio tras arrancar el teléfono
(`BOOT_COMPLETED`) y código + botones para la **mitigación de batería de Redmi/Xiaomi**.

---

## 1. Estructura del proyecto

```
apk/
├── build.gradle.kts                 # proyecto raíz (AGP 8.5.2 + Kotlin 1.9.24)
├── settings.gradle.kts              # repos google() + mavenCentral()
├── gradle.properties
├── gradle/wrapper/gradle-wrapper.properties   # Gradle 8.7 (Android Studio lo descarga solo)
├── app/
│   ├── build.gradle.kts             # applicationId com.trackcam.app · minSdk 26 · targetSdk 34
│   ├── proguard-rules.pro
│   └── src/main/
│       ├── AndroidManifest.xml      # permisos + service (location) + boot receiver
│       ├── java/com/trackcam/app/
│       │   ├── MainActivity.kt      # pantalla principal (Views clásicos)
│       │   ├── TrackService.kt      # Foreground Service: GPS + envío HTTP
│       │   ├── TrackPrefs.kt        # SharedPreferences (URL, intervalo, running)
│       │   └── BootReceiver.kt      # relanza el servicio tras el boot
│       └── res/                     # layout, strings (es), tema, iconos vectoriales
└── README.md
```

**Requisitos**: Android Studio (Ladybug o posterior, con JDK 17 incluido). No hace falta SDK
preinstalado: Android Studio lo gestiona solo.

---

## 2. Abrir y compilar en Android Studio

1. Abre Android Studio → **Open** → selecciona la carpeta `~/Escritorio/proyectos/trackcam/apk`.
   (Si pregunta por el SDK de Android, acepta la instalación del SDK 34.)
2. Espera a que Gradle sincronice (primera vez descarga Gradle 8.7 + dependencias: unos minutos).
3. Compilar el APK:
   - Menú **Build → Build App Bundle(s) / APK(s) → Build APK(s)** → el APK de depuración
     aparece en `app/build/outputs/apk/debug/app-debug.apk`.
   - O desde terminal (dentro de `apk/`):
     ```bash
     ./gradlew assembleDebug        # APK de depuración
     ./gradlew assembleRelease      # APK de release (firmado con debug key si no configuras release)
     ```
   - Para instalar con cable y `adb`:
     ```bash
     adb install -r app/build/outputs/apk/debug/app-debug.apk
     ```

> Nota: el proyecto no incluye `gradlew` ni el `.jar` del wrapper (Android Studio lo regenera
> al abrir el proyecto). Si quieres compilar desde terminal sin Android Studio:
> ```bash
> cd apk && gradle wrapper --gradle-version 8.7   # con un Gradle 8.x instalado
> ./gradlew assembleDebug
> ```

---

## 3. Instalar en el Redmi (sin cable)

1. Copia el APK al teléfono (cable USB → carpeta Descargas, o por la web/Bluetooth).
2. En el teléfono: **Ajustes → Aplicaciones → Gestionar aplicaciones → (icono 3 puntos)
   → Permisos → Instalar vía USB / fuentes desconocidas**. Si el sistema lo pide, activa
   "Instalar desde fuentes desconocidas" para el gestor de archivos.
3. Toca el `.apk` en el gestor de archivos → **Instalar**.
4. Abre **TrackCam** → pulsa **INICIAR TRACKEO** y concede:
   - **Ubicación** (permite en todo momento / "Permitir siempre" — importante, no "solo con la app abierta").
   - **Notificaciones** (para ver la notificación persistente "TrackCam activo").
5. Verifica en el servidor que llegan puntos: `curl http://192.168.1.236:8099/api/track`.

---

## 4. ⚠️ Guía paso a paso: mitigación de batería Redmi/Xiaomi (OBLIGATORIO)

MIUI/HyperOS es extremadamente agresivo matando apps en segundo plano. Sin estos pasos el
trackeo **se detendrá al bloquear la pantalla**. Hazlos todos:

### 4.1 Desactivar la optimización de batería (lo más importante)
1. **Ajustes → Aplicaciones → Gestionar aplicaciones → TrackCam**.
2. Toca **Ahorro de batería** (o "Batería").
3. Selecciona **Sin restricciones** (no "Ahorro de batería" ni "Restricción de batería").
   > En algunos MIUI: Ajustes → Batería → (⋮ tres puntos) → **Ajustes de batería** →
   > TrackCam → **Sin restricciones**.

### 4.2 Activar Autostart (inicio automático)
1. **Ajustes → Aplicaciones → Gestionar aplicaciones → TrackCam**.
2. Toca **Autostart** (o "Inicio automático").
3. **Actívalo** (toggle ON). Sin esto, MIUI mata el servicio y no lo relanza.
   > Atajo: la propia app tiene el botón **"Abrir ajustes de batería (Redmi)"**, que abre
   > directamente la pantalla de Autostart de Security Center.

### 4.3 Exención de optimización de batería de Android
1. En la app pulsa **"Solicitar exención de optimización"**.
2. En el diálogo del sistema, selecciona **Permitir / No optimizar**.
   > Alternativa manual: Ajustes → Batería → Optimización de batería → TrackCam → **No optimizar**.

### 4.4 Permitir datos en segundo plano
1. **Ajustes → Aplicaciones → Gestionar aplicaciones → TrackCam → Datos en segundo plano / Ahorro de datos**.
2. Activa **"Permitir datos en segundo plano"** (y "Datos en segundo plano sin límite" si existe).

### 4.5 Fijar la app en el selector de recientes (protección extra)
1. Abre el selector de apps recientes (botón cuadrado).
2. Mantén pulsada la tarjeta de **TrackCam** → **Bloquear** (icono del candado).
   Así no se puede deslizar/cerrar accidentalmente.

### 4.6 Comprobación final
1. Pulsa **INICIAR TRACKEO**, apaga la pantalla y espera 10 minutos.
2. Vuelve a abrir TrackCam: debe seguir en "Trackeando".
3. En el servidor: `curl http://192.168.1.236:8099/api/track` → deben aparecer puntos
   continuados en el tiempo (1 por intervalo).
4. Reinicia el teléfono: al arrancar, la app debe relanzarse sola y reanudar el envío
   (notificación "TrackCam activo" visible sin abrir la app).

> Si tras un reinicio no se relanza: comprueba que **Autostart** (4.2) sigue activo y que
> pulsa **INICIAR TRACKEO** una vez más (el estado "activo" se guarda y se restaura en el boot).

---

## 5. Uso de la app

| Control | Qué hace |
|---|---|
| **URL del servidor** | Dirección del endpoint `/track` (Cloudflare o LAN). Se guarda automáticamente. |
| **Intervalo** | Cada cuánto pide el GPS una posición: 1/2/5/10/30/60 s. Con 1 s se envía cada segundo; con intervalos mayores se envía cada posición nueva (y se reutiliza la última conocida al arrancar para no esperar el primer fix). |
| **INICIAR / DETENER** | Arranca o para el servicio en primer plano. Al iniciar pide permisos de ubicación y notificaciones. |
| **Estado** | "Enviando última posición…" mientras no hay envío; luego "Trackeando · último envío hace X". |
| **Último envío** | OK/FALLIDO + hora del último intento + nº de pendientes en cola. |
| **Ajustes de batería (Redmi)** | Abre Autostart de MIUI (con fallbacks: PowerSettings → Security Center → ajustes de optimización). En otros fabricantes abre la lista de optimización de batería. |
| **Solicitar exención** | Pide al sistema "No optimizar" para TrackCam. |

### Comportamiento técnico (resumen)
- **Foreground Service** con notificación persistente "TrackCam activo" (canal de prioridad baja),
  tipo `location` (obligatorio en Android 14), `START_STICKY` y `stopWithTask=false` (no muere
  al deslizar la app de recientes).
- **GPS**: FusedLocationProvider. Prioridad alta (GPS puro) con intervalos ≤ 5 s; modo
  equilibrado (ahorro) con intervalos ≥ 10 s.
- **Envío**: POST JSON inmediato en cada posición; OkHttp con timeout de 8 s; **1 reintento**
  tras fallo; si sigue fallando se re-encola (máx. 30 pendientes, descartando la más antigua);
  todo en corrutinas en `Dispatchers.IO` — nunca bloquea la UI.
- **Reinicio**: `BootReceiver` escucha `BOOT_COMPLETED`, `QUICKBOOT_POWERON` y
  `MY_PACKAGE_REPLACED`, y relanza el servicio si el usuario lo dejó activo.
- **Batería**: wake lock parcial mientras el servicio vive, y reutilización de la última
  posición conocida para no gastar batería esperando una fijación.

---

## 6. Solución de problemas

| Síntoma | Causa probable | Solución |
|---|---|---|
| "Último envío: FALLIDO" desde el móvil con datos | La URL no es accesible desde fuera | Comprueba el túnel Cloudflare (track.satetsunin.com) y que el servidor está arriba |
| FALLIDO solo con la URL LAN | El móvil no está en la misma red | Usa la URL de Cloudflare fuera de casa; la LAN solo funciona en el WiFi de casa |
| El envío va pero se corta con pantalla apagada | MIUI mata la app | Repasa la sección 4 completa (sobre todo 4.1 y 4.2) |
| Tras reiniciar el teléfono no se relanza | Autostart desactivado o "running" en false | Activa Autostart (4.2) y vuelve a pulsar INICIAR TRACKEO una vez |
| No llega ningún punto | Servidor apagado o puerto cerrado | `uvicorn backend.app:app --host 0.0.0.0 --port 8099` y `curl -X POST http://192.168.1.236:8099/track -d '{"lat":43.26,"lon":-2.93}'` |
