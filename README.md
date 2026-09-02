# TrackCam — Trackeo con captura de cámaras por proximidad

Sistema que registra la ubicación del usuario cada segundo (APK Android), cruza la posición con el
catálogo de **45.759 cámaras georreferenciadas de EuroCams** (SOLO esa base de datos, sin otras
fuentes), captura fotogramas según proximidad y genera **eventos por cámara**: vídeo + imágenes +
marcador en el track, visibles y descargables desde el navegador.

## Decisiones de Alvaro (consensuadas)

- **Nombre**: TrackCam
- **Captura**: el SERVIDOR hace el snapshot/fotograma cada **2 s SIN excepción** (todas las cámaras
  del radio, incluidas las live de Bilbao). Sin reglas especiales para cámaras fijas.
- **Radios**: 1,5 km → cámara "activa" · 500 m → captura continua 2 s · 100 m → evento
  (se conserva 20 s antes + 40 s después)
- **Almacenamiento**: en el ordenador; temporales se borran si no se pasa a <100 m; al crear el
  vídeo se guardan también las imágenes; track dibujado en el mapa
- **App móvil**: APK propia, contacto continuo con el servidor, mitigación batería Redmi desde el inicio
- **Túnel**: Cloudflare (track.satetsunin.com → 127.0.0.1:8099)
- **Vídeo**: ffmpeg (instalado) — cada evento → MP4
- **Web**: mapa Leaflet con track + marcadores de evento + control de almacenamiento

## Arquitectura

```
[APK Android (GPS 1 s · 24 h)] ──Cloudflare tunnel──► [FastAPI :8099]
                                                            │
    /track (lat, lon, ts, acc) → SQLite (tracks.db)        ▼
    Motor de proximidad: grid hash 45.759 cams (EuroCams)  ▼
    1,5 km → activa · 500 m → captura 2 s · 100 m → evento ▼
    ffmpeg JPEG→MP4 → data/eventos/<id>/ (vídeo + imágenes)
    Web UI :8099: mapa + eventos + control de almacenamiento
```

## Estructura

```
trackcam/
├── backend/app.py          # FastAPI: /track, /api/track, /api/eventos, /api/almacenamiento, web
├── backend/grid.py         # índice geo (grid hash) sobre la BD de EuroCams
├── backend/captura.py      # motor de captura (ring buffers, eventos, ffmpeg)
├── web/index.html          # mapa Leaflet
├── data/                   # tracks.db, temps/, eventos/ (gitignored)
├── apk/                    # APK Android (Fase 3)
├── docs/                   # Plan-TrackCam-v1.0.pdf
└── scripts/generar_plan.py
```

## Puesta en marcha

```bash
cd ~/Escritorio/proyectos/trackcam
python3 -m venv venv && source venv/bin/activate
pip install fastapi uvicorn
uvicorn backend.app:app --host 0.0.0.0 --port 8099
```

## Fases

- **F1 (MVP)**: receptor /track + SQLite + grid hash + captura 2 s con ring buffers + eventos ffmpeg + mapa web
- **F2**: panel de almacenamiento (temps/guardado, cuotas, descarga, borrado), ajustes, reproductor
- **F3**: APK Kotlin (24 h, auto-restart, mitigación Redmi, intervalo configurable) + video track
- **F4**: pulido, exportaciones, multi-día

## Mitigación batería Redmi (desde el inicio)

- Foreground service con notificación persistente
- `START_STICKY` + arranque tras reinicio
- Guía de exención: Ajustes → Batería → No restringir + Autostart (permisos de la app)
- Intervalo de GPS configurable (1 s en ruta, más amplio en reposo)
