# Prueba F1/F2 — Motor de captura configurable

Fecha: 2026-09-02 (actualizada tras F2)

## Resultado: ✅ PRUEBA SUPERADA

El simulador genera 40 puntos (1/s, ts en el pasado) a lo largo de un tramo en
coche por Bilbao que pasa a ~40 m de cámaras geobilbao reales. El motor de
captura procesa el recorrido y crea eventos:

| Campo | Valor |
|---|---|
| Eventos creados | **8** (BERNAOLA ×5, Camino Morgan, Frank Gehry...) |
| Fotos por evento | 12-17 (ventana 20 s antes + 40 s después del cruce de 100 m) |
| video.mp4 | MP4 H.264, 2 fps, 8,5 s |
| Descargas | 531 ok / 1 fallo (reintento vía proxy EuroCams) |
| Directorio | `data/eventos/<eid>/` (video.mp4 + fotos + metadata.json) |

## F2 — Configuración dinámica (verificada)

- `GET /api/ajustes` → configuración actual del motor
- `POST /api/ajustes` → actualiza en caliente y persiste en `data/ajustes.json`
- Parámetros: radios (activa 1500 / captura 500 / evento 100 m), intervalo de
  captura (2 s), ventanas (20+40 s), buffer (90 s), fps vídeo, **cuotas**
  (eventos 20 GB / temporales 500 MB)
- **Poda por cuota verificada**: cuota de 0,0001 GB → el motor borró los 8
  eventos antiguos + carpetas automáticamente (~30 s)
- Tras la prueba de poda se restauró la configuración por defecto y se
  regeneraron 8 eventos (evidencia en BD)

## Cómo reproducirlo

```bash
cd /home/alvaro/Escritorio/proyectos/trackcam
python3 -m uvicorn backend.app:app --host 0.0.0.0 --port 8099   # backend
python3 scripts/simular_recorrido.py                             # prueba
```
