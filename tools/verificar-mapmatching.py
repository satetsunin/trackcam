#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Verifica /match contra una traza GPX y saca las cuentas.

Uso:
    python3 verificar-mapmatching.py --url http://127.0.0.1:8098 --gpx traza-2026-09-13.gpx \
        [--gps-accuracy 80] [--tolerancia 25] [--guardar respuesta.json] [--probar-nearest]

Comprueba, con números reales:
  * HTTP y tamaño de la respuesta
  * nº de puntos de entrada vs. nº de puntos que quedan a <= tolerancia de la
    geometría matcheada (es decir, "matcheados")
  * distancia de la traza cruda frente a la distancia de la geometría devuelta
  * opcionalmente, que los vértices devueltos están SOBRE la red viaria
    (llamando a /nearest y viendo que la distancia es ~0)
"""
import argparse
import json
import math
import random
import re
import sys
import urllib.error
import urllib.request

R_TIERRA = 6371000.0


def haversine(a, b):
    """a, b = (lon, lat) -> metros."""
    lo1, la1 = a
    lo2, la2 = b
    la1, lo1, la2, lo2 = map(math.radians, (la1, lo1, la2, lo2))
    h = (math.sin((la2 - la1) / 2) ** 2
         + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
    return 2 * R_TIERRA * math.asin(min(1.0, math.sqrt(h)))


def longitud(coords):
    return sum(haversine(a, b) for a, b in zip(coords, coords[1:]))


def distancia_a_polilinea(p, coords):
    """Distancia en metros de un punto (lon,lat) a la polilínea (medida al
    segmento más cercano, no al vértice: la geometría matcheada viene
    simplificada y sus vértices pueden estar lejos aunque la línea pase justo
    por encima del punto)."""
    lo0, la0 = p
    kx = 111320.0 * math.cos(math.radians(la0))
    ky = 110540.0
    px, py = 0.0, 0.0
    mejor = float("inf")
    for a, b in zip(coords, coords[1:]):
        ax, ay = (a[0] - lo0) * kx, (a[1] - la0) * ky
        bx, by = (b[0] - lo0) * kx, (b[1] - la0) * ky
        dx, dy = bx - ax, by - ay
        if dx == 0.0 and dy == 0.0:
            d = math.hypot(px - ax, py - ay)
        else:
            t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
            d = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
        if d < mejor:
            mejor = d
    return mejor


def leer_gpx(path):
    txt = open(path, encoding="utf-8").read()
    pts = [(float(lon), float(lat)) for lat, lon in
           re.findall(r'<trkpt[^>]*lat="([-0-9.]+)"[^>]*lon="([-0-9.]+)"', txt)]
    return pts


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:8098")
    p.add_argument("--gpx", required=True)
    p.add_argument("--perfil", default="car")
    p.add_argument("--gps-accuracy", type=float, default=80)
    p.add_argument("--tolerancia", type=float, default=25.0,
                   help="m: un punto cuenta como matcheado si queda a menos de esto de la geometría")
    p.add_argument("--guardar", default=None)
    p.add_argument("--probar-nearest", action="store_true")
    args = p.parse_args()

    entrada = leer_gpx(args.gpx)
    q = f"profile={args.perfil}&points_encoded=false&gps_accuracy={args.gps_accuracy:g}"
    req = urllib.request.Request(f"{args.url}/match?{q}",
                                 data=open(args.gpx, "rb").read(),
                                 headers={"Content-Type": "application/gpx+xml"})
    try:
        r = urllib.request.urlopen(req, timeout=600)
        cuerpo = r.read()
        http = r.status
    except urllib.error.HTTPError as e:
        print("HTTP", e.code, e.read()[:400].decode("utf-8", "replace"))
        return 1
    print(f"POST {args.url}/match?{q}  ->  HTTP {http}  {len(cuerpo)} bytes")

    if args.guardar:
        open(args.guardar, "wb").write(cuerpo)
        print("respuesta guardada en", args.guardar)

    d = json.loads(cuerpo)
    cam = d["paths"][0]["points"]["coordinates"]
    mm = d.get("map_matching", {})
    largo_crudo = longitud(entrada)
    largo_cam = longitud(cam)
    print(f"puntos de entrada:        {len(entrada)}")
    print(f"vertices de la geometria: {len(cam)}")
    print(f"map_matching.original_distance: {mm.get('original_distance', float('nan'))/1000:.2f} km (traza cruda)")
    print(f"longitud real de la linea devuelta: {largo_cam/1000:.2f} km  "
          f"(traza cruda medida: {largo_crudo/1000:.2f} km, ratio {largo_cam/largo_crudo:.2f})")

    # ¿cada punto de entrada acaba sobre la geometría devuelta?
    ok = 0
    peor = 0.0
    for pto in entrada:
        dm = distancia_a_polilinea(pto, cam)
        peor = max(peor, dm)
        if dm <= args.tolerancia:
            ok += 1
    print(f"puntos de entrada a <= {args.tolerancia:g} m de la geometria: {ok}/{len(entrada)} "
          f"({100.0*ok/len(entrada):.1f}%)  peor caso: {peor:.1f} m")

    if args.probar_nearest:
        random.seed(7)
        def nearest(lat, lon):
            return json.loads(urllib.request.urlopen(
                f"{args.url}/nearest?point={lat},{lon}", timeout=30).read())
        ds = sorted(nearest(la, lo)["distance"] for lo, la in random.sample(cam, min(40, len(cam))))
        dr = sorted(nearest(la, lo)["distance"] for la, lo in random.sample([(p[1], p[0]) for p in entrada], 40))
        print(f"/nearest vertice matcheado -> red: min {ds[0]:.2f} m | mediana {ds[len(ds)//2]:.2f} m | max {ds[-1]:.2f} m")
        print(f"/nearest punto GPS crudo  -> red: mediana {dr[len(dr)//2]:.1f} m | max {dr[-1]:.1f} m")
        print("(si los vertices estan a ~0 m, la geometria devuelta va POR las calles)")

    if mm.get("distance") and mm.get("original_distance"):
        ratio = mm["distance"] / mm["original_distance"]
        print(f"ratio distancia matcheada/cruda: {ratio:.2f}"
              + ("   <-- OK" if 0.85 <= ratio <= 1.15 else "   <-- SOSPECHOSO: sube --gps-accuracy"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
