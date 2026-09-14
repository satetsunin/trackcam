#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Exporta una traza de TrackCam (data/tracks.db) a GPX para map-matching.

La base de datos NO se modifica: se abre en modo solo lectura (mode=ro).

OJO CON LA ZONA HORARIA
    La columna `ts` guarda epoch en UTC, pero el intervalo que ve el usuario en
    la app está en hora local (CEST = UTC+2 en septiembre). Por eso la traza
    "13:40-14:20" de la app son las 11:40-12:20 UTC. Con --tz las horas que
    pases se interpretan como hora local y se convierten a UTC solas.

Uso:
    python3 exportar-traza-gpx.py --user 1 --desde "2026-09-13 13:40" \\
        --hasta "2026-09-13 14:20" --tz +2 --out traza.gpx
Opciones:
    --user ID      usuario (por defecto 1)
    --desde/--hasta  "YYYY-MM-DD HH:MM" en hora local (según --tz)
    --tz N         desplazamiento respecto a UTC en horas (por defecto +2 para CEST)
    --min-acc M    descarta puntos con precisión peor que M metros (por defecto 0 = no filtra)
    --out FICHERO  destino GPX (por defecto traza.gpx)
    --json FICHERO si se indica, escribe además el payload JSON para /match
"""
import argparse
import datetime
import json
import math
import sqlite3
import sys
from pathlib import Path

DB_DEFECTO = Path(__file__).resolve().parent.parent / "data" / "tracks.db"
R_TIERRA = 6371000.0


def haversine_m(a, b):
    """a, b = (lat, lon) -> metros."""
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (math.sin((la2 - la1) / 2) ** 2
         + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
    return 2 * R_TIERRA * math.asin(min(1.0, math.sqrt(h)))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=str(DB_DEFECTO))
    p.add_argument("--user", default="1")
    p.add_argument("--desde", required=True)
    p.add_argument("--hasta", required=True)
    p.add_argument("--tz", type=float, default=2.0, help="horas de desplazamiento local (CEST=+2)")
    p.add_argument("--min-acc", type=float, default=0.0, help="descarta puntos con acc peor que esto (m)")
    p.add_argument("--out", default="traza.gpx")
    p.add_argument("--json", default=None, help="escribe además el payload JSON para /match")
    args = p.parse_args()

    def a_epoch(s):
        dt = datetime.datetime.strptime(s, "%Y-%m-%d %H:%M")
        dt = dt.replace(tzinfo=datetime.timezone.utc) - datetime.timedelta(hours=args.tz)
        return dt.timestamp()

    t0, t1 = a_epoch(args.desde), a_epoch(args.hasta)
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    filas = con.execute(
        "SELECT ts, lat, lon, acc FROM tracks "
        "WHERE user_id=? AND ts>=? AND ts<=? "
        "AND lat IS NOT NULL AND lon IS NOT NULL ORDER BY ts",
        (args.user, t0, t1)).fetchall()
    if args.min_acc > 0:
        filas = [f for f in filas if (f[3] or 0) <= args.min_acc]
    if not filas:
        sys.exit("Sin puntos en ese intervalo. ¿Usuario o zona horaria incorrectos?")

    coords = [(f[1], f[2]) for f in filas]
    dist = sum(haversine_m(a, b) for a, b in zip(coords, coords[1:]))
    print(f"{len(filas)} puntos | {dist/1000:.2f} km | "
          f"{datetime.datetime.utcfromtimestamp(filas[0][0])} -> "
          f"{datetime.datetime.utcfromtimestamp(filas[-1][0])} UTC")

    with open(args.out, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<gpx version="1.1" creator="TrackCam" '
                'xmlns="http://www.topografix.com/GPX/1/1">\n<trk><trkseg>\n')
        for ts, lat, lon, _acc in filas:
            t = datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ")
            f.write(f'<trkpt lat="{lat}" lon="{lon}"><time>{t}</time></trkpt>\n')
        f.write("</trkseg></trk></gpx>\n")
    print("GPX:", args.out)

    if args.json:
        payload = {"profile": "car",
                   "gps_accuracy": 80,
                   "points_encoded": False,
                   "points": [[f[1], f[2]] for f in filas]}
        Path(args.json).write_text(json.dumps(payload), encoding="utf-8")
        print("JSON:", args.json)


if __name__ == "__main__":
    main()
