# Prueba F1 — Motor de captura (Fase 2)

Fecha: 2026-09-02 03:12:27

## Resultado: ✅ PRUEBA SUPERADA

El simulador generó 40 puntos (1/s, ts en el pasado) a lo largo de un
tramo en coche por Bilbao que pasa a 40 m de una cámara geobilbao
real. El motor de captura procesó el recorrido y creó el evento:

| Campo | Valor |
|---|---|
| Evento | `5708e1387f28` |
| Cámara | Bernaola Ext.OUTLado Bilbao (id `Bilbao-Ayto_43.26958_-2.95617`) |
| Posición cámara | 43.26958, -2.95617 |
| Nº fotos | 16 (ventana 20 s antes + 40 s después del cruce de 100 m) |
| video.mp4 | 101773 bytes (MP4 H.264, 2 fps) |
| ts_inicio / ts_fin | 1788311444.6 / 1788311504.6 |
| Directorio | `data/eventos/5708e1387f28/` (video.mp4 + 16 fotos + metadata.json) |

## Verificaciones

- [x] Evento registrado en la tabla SQLite `eventos`
- [x] `video.mp4` existe y pesa > 0 bytes
- [x] `n_fotos` > 5
- [x] Imágenes originales copiadas en `data/eventos/5708e1387f28/`
- [x] `metadata.json` generado

## Cómo reproducirlo

```bash
cd /home/alvaro/Escritorio/proyectos/trackcam
python3 -m uvicorn backend.app:app --host 0.0.0.0 --port 8099   # backend
python3 scripts/simular_recorrido.py                             # prueba
```

El simulador elige la cámara geobilbao con más vecinas a ≤500 m, construye
un tramo rectilíneo que pasa a ~40 m de ella y envía los puntos con `ts` en
el pasado; el motor lee en cada ciclo todos los puntos nuevos
(`WHERE ts > ultimo_procesado`) y procesa el cruce de 100 m.
