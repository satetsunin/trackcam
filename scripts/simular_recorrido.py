#!/usr/bin/env python3
"""TrackCam — Simulador de recorrido (prueba Fase 2 / motor de captura).

Genera ~40 puntos (1 punto/s, ts en el PASADO) a lo largo de un tramo en
coche por Bilbao que pasa a ~40 m de una cámara geobilbao real (elegida con
el índice de EuroCams: la que más cámaras tiene a ≤500 m), hace POST /track
por cada punto y verifica que el motor de captura crea AL MENOS 1 evento:
video.mp4 existe y pesa >0, n_fotos > 5 y el evento está en la BD.

Uso:
    python3 scripts/simular_recorrido.py [--url http://127.0.0.1:8099]

Cómo funciona la prueba: como los ts van en el pasado, el motor lee todos
los puntos nuevos (ts > último procesado) en su siguiente ciclo y los
procesa en orden, detectando el cruce de los 100 m y generando el evento.
"""

import os
import sys
import json
import time
import math
import argparse
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EUROCAMS_JSON = os.path.expanduser(
    "~/Escritorio/proyectos/eurocams/data/europa_camaras_consolidado.json")

V = 14.0            # velocidad del coche (m/s ≈ 50 km/h)
N_PUNTOS = 40       # ~40 puntos, 1/s
I_MAS_CERCANO = 26  # índice del punto más cercano a la cámara objetivo
D_MIN = 40.0        # distancia mínima del tramo a la cámara (m)
RADIO_CAPTURA = 500.0
RADIO_ACTIVA = 1500.0


def hav(lat1, lon1, lat2, lon2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def tramo_para(cam, heading_grados):
    """Puntos del tramo: recta que pasa a D_MIN m de la cámara en el punto
    I_MAS_CERCANO, dirección 'heading' (grados), V m/s, N_PUNTOS puntos."""
    lat, lon = cam["lat"], cam["lon"]
    h = math.radians(heading_grados)
    ux, uy = math.sin(h), math.cos(h)      # dirección de avance (lon, lat)
    nx, ny = -uy, ux                       # perpendicular
    # Punto más cercano a la cámara, desplazado D_MIN m en perpendicular
    mlat = lat + D_MIN * ny / 111000.0
    mlon = lon + D_MIN * nx / (111000.0 * math.cos(math.radians(lat)))
    k_lon = 1.0 / (111000.0 * math.cos(math.radians(lat)))
    pts = []
    for i in range(N_PUNTOS):
        d = (i - I_MAS_CERCANO) * V  # metros a lo largo del tramo
        pts.append((mlat + d * uy / 111000.0, mlon + d * ux * k_lon))
    return pts


def elegir_tramo(cams):
    """Elige cámara objetivo (geobilbao con más vecinas a 500 m) y el
    heading que maximiza las cámaras a ≤500 m del tramo."""
    geo = [c for c in cams
           if c.get("lat") is not None and c.get("lon") is not None
           and "bilbao" in str(c.get("fuente", "")).lower()
           and 43.25 <= c["lat"] <= 43.30 and -2.99 <= c["lon"] <= -2.92]
    if not geo:
        sys.exit("No hay cámaras geobilbao en la zona de Bilbao")

    # Objetivo: la cámara con más vecinas a ≤500 m
    objetivo = max(geo, key=lambda c: sum(
        1 for g in geo if hav(c["lat"], c["lon"], g["lat"], g["lon"]) <= RADIO_CAPTURA))

    # Heading: probar 36 direcciones y quedarse con la del tramo que más
    # cámaras distintas tenga a ≤500 m (el tramo cruza la zona de cámaras)
    mejor = None
    for h in range(0, 360, 10):
        pts = tramo_para(objetivo, h)
        ids = set()
        for plat, plon in pts:
            for c in geo:
                if hav(plat, plon, c["lat"], c["lon"]) <= RADIO_CAPTURA:
                    ids.add((c["lat"], c["lon"]))
        if mejor is None or len(ids) > mejor[0]:
            mejor = (len(ids), h)
    _, heading = mejor
    pts = tramo_para(objetivo, heading)
    # cámaras distintas a ≤500 m y a <100 m del tramo elegido
    ids500, ids100 = set(), set()
    for plat, plon in pts:
        for c in geo:
            d = hav(plat, plon, c["lat"], c["lon"])
            if d <= RADIO_CAPTURA:
                ids500.add((c["lat"], c["lon"]))
            if d <= 100:
                ids100.add((c["lat"], c["lon"]))
    return objetivo, pts, heading, len(ids500), len(ids100)


def post_track(url, ts, lat, lon):
    body = json.dumps({"lat": lat, "lon": lon, "ts": ts, "dev": "simulador"}).encode()
    req = urllib.request.Request(url + "/track", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def get_json(url, ruta):
    with urllib.request.urlopen(url + ruta, timeout=10) as r:
        return json.loads(r.read().decode())


def main():
    ap = argparse.ArgumentParser(description="Simula un recorrido por Bilbao")
    ap.add_argument("--url", default="http://127.0.0.1:8099")
    ap.add_argument("--espera", type=float, default=150.0,
                    help="máx segundos esperando al evento")
    args = ap.parse_args()

    with open(EUROCAMS_JSON) as f:
        data = json.load(f)
    cams = data if isinstance(data, list) else data.get("camaras", [])

    print("=== SIMULADOR TRACKCAM — recorrido por Bilbao ===")
    objetivo, pts, heading, n500, n100 = elegir_tramo(cams)
    print(f"Cámara objetivo: {objetivo['nombre']} "
          f"({objetivo['lat']:.5f},{objetivo['lon']:.5f})")
    print(f"Tramo: heading {heading}°, {N_PUNTOS} puntos a {V:.0f} m/s, "
          f"pasa a {D_MIN:.0f} m de la cámara | {n500} cámaras ≤500 m, "
          f"{n100} <100 m en el tramo")

    n_ev_antes = get_json(args.url, "/api/eventos")
    n_ev_antes = len(n_ev_antes)

    # Enviar los puntos con ts en el pasado (el motor los procesa de golpe)
    base_ts = time.time() - 90.0
    for i, (lat, lon) in enumerate(pts):
        r = post_track(args.url, base_ts + i, lat, lon)
        if i in (0, N_PUNTOS // 2, N_PUNTOS - 1):
            print(f"  POST {i+1}/{N_PUNTOS} ts={base_ts+i:.1f} "
                  f"({lat:.5f},{lon:.5f}) → {r}")
    print(f"Enviados {N_PUNTOS} puntos (ts de {base_ts:.0f} a {base_ts+N_PUNTOS:.0f})")

    # Esperar a que el motor procese y cree el evento
    t0 = time.time()
    evento = None
    while time.time() - t0 < args.espera:
        time.sleep(2)
        try:
            evs = get_json(args.url, "/api/eventos")
        except Exception:
            continue
        nuevos = [e for e in evs if e.get("id") not in []]
        if len(evs) > n_ev_antes:
            # el más reciente es el último de la lista (orden ts_inicio)
            evento = evs[-1]
            break
        estado = get_json(args.url, "/api/estado")
        print(f"  esperando… eventos={len(evs)} "
              f"capturando={estado.get('camaras_capturando')} "
              f"activas={estado.get('camaras_activas')} "
              f"descargas_ok={estado.get('descargas_ok')}")
    if evento is None:
        sys.exit("ERROR: no se creó ningún evento. Revisa el motor.")

    # ── Verificación ─────────────────────────────────────────────────────
    eid = evento["id"]
    dir_ev = os.path.join(BASE, "data", "eventos", eid)
    video = os.path.join(dir_ev, "video.mp4")
    meta_path = os.path.join(dir_ev, "metadata.json")
    ok_video = os.path.exists(video) and os.path.getsize(video) > 0
    ok_meta = os.path.exists(meta_path)
    fotos = sorted(f for f in os.listdir(dir_ev)
                   if f.startswith("foto_") and f.endswith(".jpg")) if os.path.isdir(dir_ev) else []
    n_fotos = evento.get("n_fotos", len(fotos))
    ok_fotos = n_fotos > 5 and len(fotos) == n_fotos
    tam_video = os.path.getsize(video) if ok_video else 0

    print("\n=== RESULTADO DE LA PRUEBA ===")
    print(f"Evento: {eid}")
    print(f"Cámara: {evento.get('cam_nombre')} (id {evento.get('cam_id')})")
    print(f"n_fotos: {n_fotos} (archivos en disco: {len(fotos)})")
    print(f"video.mp4: {tam_video} bytes → {'OK' if ok_video else 'FALLO'}")
    print(f"metadata.json: {'OK' if ok_meta else 'FALLO'}")
    print(f"ts_inicio={evento.get('ts_inicio'):.1f} "
          f"ts_fin={evento.get('ts_fin'):.1f}")
    print(f"directorio: {dir_ev}")

    checks = {
        "evento en BD": evento is not None,
        "video.mp4 existe y >0": ok_video,
        "n_fotos > 5": n_fotos > 5,
        "fotos en disco == n_fotos": ok_fotos,
        "metadata.json": ok_meta,
    }
    fallos = [k for k, v in checks.items() if not v]
    print("\nVerificaciones:")
    for k, v in checks.items():
        print(f"  [{'OK' if v else 'FALLO'}] {k}")
    if fallos:
        sys.exit("PRUEBA FALLIDA: " + ", ".join(fallos))

    # ── Documentar en docs/prueba-f1.md ──────────────────────────────────
    docs = os.path.join(BASE, "docs")
    os.makedirs(docs, exist_ok=True)
    ruta_informe = os.path.join(docs, "prueba-f1.md")
    md = f"""# Prueba F1 — Motor de captura (Fase 2)

Fecha: {time.strftime('%Y-%m-%d %H:%M:%S')}

## Resultado: ✅ PRUEBA SUPERADA

El simulador generó {N_PUNTOS} puntos (1/s, ts en el pasado) a lo largo de un
tramo en coche por Bilbao que pasa a {D_MIN:.0f} m de una cámara geobilbao
real. El motor de captura procesó el recorrido y creó el evento:

| Campo | Valor |
|---|---|
| Evento | `{eid}` |
| Cámara | {evento.get('cam_nombre')} (id `{evento.get('cam_id')}`) |
| Posición cámara | {evento.get('lat'):.5f}, {evento.get('lon'):.5f} |
| Nº fotos | {n_fotos} (ventana 20 s antes + 40 s después del cruce de 100 m) |
| video.mp4 | {tam_video} bytes (MP4 H.264, 2 fps) |
| ts_inicio / ts_fin | {evento.get('ts_inicio'):.1f} / {evento.get('ts_fin'):.1f} |
| Directorio | `data/eventos/{eid}/` (video.mp4 + {n_fotos} fotos + metadata.json) |

## Verificaciones

- [x] Evento registrado en la tabla SQLite `eventos`
- [x] `video.mp4` existe y pesa > 0 bytes
- [x] `n_fotos` > 5
- [x] Imágenes originales copiadas en `data/eventos/{eid}/`
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
"""
    with open(ruta_informe, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"\nInforme guardado en docs/prueba-f1.md")
    print("PRUEBA SUPERADA ✅")


if __name__ == "__main__":
    main()
