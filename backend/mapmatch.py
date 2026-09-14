# -*- coding: utf-8 -*-
"""Map-matching: pega la traza GPS a la red viaria (GraphHopper `/match`).

La idea: los puntos crudos del GPS tienen ruido y "esquinas" falsas; el
map-matching (modelo HMM sobre el grafo de carreteras) devuelve la geometría
siguiendo las calles reales. Lo usará el endpoint de la API (p. ej.
`/api/track`) como capa adicional OPCIONAL.

Diseño DEFENSIVO por requisito del proyecto: si el motor de map-matching no
está levantado, tarda, devuelve un error o una respuesta inesperada, TODAS las
funciones devuelven `None`/vomitario y NUNCA lanzan excepción. El mapa debe
seguir funcionando con la línea GPS normal aunque el motor esté caído.

Módulo autocontenido: solo biblioteca estándar (`urllib`, `json`, `xml`), sin
dependencias nuevas.

Contrato público
----------------
    def matchear(puntos, perfil="car", url=None, timeout=20.0) -> dict | None
    def puntos_a_gpx(puntos, nombre="trackcam") -> str

`puntos`: lista cronológica de tuplas ``(ts, lat, lon, ...)`` (ts en segundos
epoch; el resto de campos se ignoran). Devuelve::

    {
      "coords": [[lon, lat], ...],  # geometría pegada a las calles (GeoJSON)
      "n_in": int,                  # puntos de entrada
      "n_matcheados": int,          # puntos que entraron en algún tramo válido
      "dist_m": float,              # distancia total matcheada (metros)
      "matchings": int,             # nº de tramos/trazos que resolvió el motor
    }

Variable de entorno ``TRACKCAM_MM_URL`` para apuntar al servicio (por defecto
``http://127.0.0.1:8098/match``). También se puede pasar `url=` explícita.

Huecos: si la traza tiene saltos de tiempo > ``HUECO_MAX_S`` (10 min) entre
puntos consecutivos, se parte en tramos y se llama al motor una vez por tramo
(el equivalente cliente de ``gaps=split`` de OSRM / ``split`` de otros
motores); las geometrías se concatenan sin duplicar el punto de unión.

Ejemplo de uso desde el endpoint (adelgazando antes para no matar al motor):

    from backend import mapmatch

    sel = geo_filtro.filtro_anti_deriva(crudos)         # puntos ya filtrados
    mm = mapmatch.matchear([(p[0], p[1], p[2]) for p in sel], perfil="car")
    if mm:
        # mm['coords'] ya es una lista [lon,lat] lista para LineString GeoJSON
        return {"type": "Feature",
                "geometry": {"type": "LineString", "coordinates": mm["coords"]},
                "properties": {"match": True, **{k: mm[k] for k in
                               ("n_in", "n_matcheados", "dist_m", "matchings")}}}
    # sin motor: el mapa sigue con la polilínea GPS normal
"""
from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from xml.sax.saxutils import escape

# ── Configuración ───────────────────────────────────────────────────────────
# URL del servicio local de map-matching (GraphHopper `/match`). Configurable
# por entorno para no tocar código al cambiar de puerto/servidor.
MM_URL = os.environ.get("TRACKCAM_MM_URL", "http://127.0.0.1:8098/match")
# F5.25 — gps_accuracy: MEDIDO contra el motor real, no es opcional.
# Con el valor por defecto del motor, la traza de 46 km de alvaro volvía con
# 835 km (18x de zigzag, 23.532 vértices). Con 15 m → 11x; con 50 → 1,72x;
# con 60-120 m → 0,99-1,06x. Sin este parámetro el map-matching es INSERVIBLE.
MM_GPS_ACC_MIN = 60.0        # suelo
MM_GPS_ACC_MAX = 120.0       # techo
MM_GPS_ACC_DEF = 80.0        # si no hay dato de precisión en los puntos
VENTANA_VEL = 60             # ventana de búsqueda del punto GPS más cercano

# Salto de tiempo (s) que corta la traza en tramos independientes. GraphHopper
# no tiene un `gaps=split` como OSRM, así que el corte se hace aquí: un hueco
# de >10 min suele ser el móvil congelado/apagado y unir los extremos por
# carretera inventaría un recorrido que no existió.
HUECO_MAX_S = 600.0

# Un tramo con demasiados puntos hace que GraphHopper agote nodos/tiempo. Por
# encima de este tope se trocea por número de puntos (los trozos se concatenan
# igual que los tramos por hueco).
MAX_PTS_TRAMO = 3000

# Perfil de GraphHopper por defecto.
PERFIL_DEF = "car"

# Timeout HTTP por defecto (s). El map-matching es más caro que /route.
TIMEOUT_DEF = 20.0

_RADIO_TIERRA_M = 6371000.0


def _ts_a_iso(ts) -> str:
    """Epoch (s) → ISO 8601 UTC con Z, como espera el <time> del GPX."""
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _campos_punto(p):
    """Extrae (ts, lat, lon) de un punto admisible.

    Acepta tuplas/listas ``(ts, lat, lon, ...)`` y, por tolerancia, dicts con
    claves ``ts``/``time``, ``lat``/``latitude``, ``lon``/``lng``/``longitude``.
    Devuelve ``None`` si el punto no es válido (nunca lanza).
    """
    try:
        if isinstance(p, dict):
            ts = p.get("ts", p.get("time"))
            lat = p.get("lat", p.get("latitude"))
            lon = p.get("lon", p.get("lng", p.get("longitude")))
            if ts is None or lat is None or lon is None:
                return None
        else:
            ts, lat, lon = p[0], p[1], p[2]
        ts = float(ts)
        lat = float(lat)
        lon = float(lon)
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            return None
        if not math.isfinite(ts):
            return None
        return ts, lat, lon
    except (TypeError, ValueError, IndexError, KeyError):
        return None


def puntos_a_gpx(puntos, nombre="trackcam") -> str:
    """Genera un GPX 1.1 con un <trkpt> por punto, con su <time> en ISO 8601 Z.

    GraphHopper `/match` solo acepta la traza como GPX por POST. Los puntos
    inválidos se saltan; si no queda ninguno devuelve un GPX vacío (el motor
    lo rechazará y `matchear` devolverá None, sin romper nada).
    """
    partes = ['<?xml version="1.0" encoding="UTF-8"?>',
              '<gpx version="1.1" creator="TrackCam"',
              ' xmlns="http://www.topografix.com/GPX/1/1">',
              '<trk><name>%s</name><trkseg>' % escape(str(nombre))]
    for p in (puntos or []):
        c = _campos_punto(p)
        if c is None:
            continue
        ts, lat, lon = c
        partes.append(
            '<trkpt lat="%.7f" lon="%.7f"><time>%s</time></trkpt>'
            % (lat, lon, _ts_a_iso(ts)))
    partes.append('</trkseg></trk></gpx>')
    return "\n".join(partes)


def _partir_en_tramos(puntos):
    """Parte la lista cronológica en tramos: corta en huecos > HUECO_MAX_S.

    Además trocea los tramos con más de MAX_PTS_TRAMO puntos. Devuelve una
    lista de listas (nunca vacía si `puntos` tiene al menos un punto válido).
    """
    validos = []
    for p in puntos:
        c = _campos_punto(p)
        if c is not None:
            validos.append((p, c[0]))
    if not validos:
        return []

    tramos = []
    actual = [validos[0][0]]
    for p, ts in validos[1:]:
        if ts - actual_ts(actual[-1]) > HUECO_MAX_S:
            tramos.append(actual)
            actual = [p]
        else:
            actual.append(p)
    tramos.append(actual)

    # Troceado por tamaño (defensivo para trazas de miles de puntos).
    troceados = []
    for t in tramos:
        if len(t) <= MAX_PTS_TRAMO:
            troceados.append(t)
        else:
            for i in range(0, len(t), MAX_PTS_TRAMO):
                troceados.append(t[i:i + MAX_PTS_TRAMO])
    return troceados


def actual_ts(punto):
    """ts de un punto ya validado (helper interno, no forma parte del contrato)."""
    c = _campos_punto(punto)
    return c[0] if c else 0.0


def _dist_m(a, b):
    """Distancia haversine (m) entre dos [lon, lat]."""
    lon1, lat1 = a
    lon2, lat2 = b
    la1, lo1, la2, lo2 = map(math.radians, (lat1, lon1, lat2, lon2))
    h = (math.sin((la2 - la1) / 2.0) ** 2
         + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2.0) ** 2)
    return 2.0 * _RADIO_TIERRA_M * math.asin(min(1.0, math.sqrt(h)))


def _longitud(coords) -> float:
    """Longitud (m) de una polilínea [[lon,lat], ...]."""
    return sum(_dist_m(coords[i - 1], coords[i]) for i in range(1, len(coords)))


def _decodificar_polyline(txt, precision=1e5):
    """Fallo de seguridad: decodifica la polilínea de GraphHopper si el motor
    ignorase `points_encoded=false`. Devuelve [[lon,lat],...] o None.

    GraphHopper codifica (lat, lon) con factor 1e5 por defecto.
    """
    try:
        coords = []
        lat = lon = 0
        i = 0
        while i < len(txt):
            for idx in (0, 1):
                shift = result = 0
                while True:
                    b = ord(txt[i]) - 63
                    i += 1
                    result |= (b & 0x1F) << shift
                    shift += 5
                    if b < 0x20:
                        break
                d = ~(result >> 1) if (result & 1) else (result >> 1)
                if idx == 0:
                    lat += d
                else:
                    lon += d
            coords.append([lon / precision, lat / precision])
        return coords or None
    except Exception:
        return None


def _coords_de_path(path):
    """Extrae la geometría [[lon,lat],...] de un path de GraphHopper.

    Con `points_encoded=false` llega como GeoJSON LineString
    (``points: {type, coordinates}``). Se aceptan también listas directas y la
    polilínea codificada como último recurso. Devuelve None si no hay nada.
    """
    try:
        pts = path.get("points")
        cs = None
        if isinstance(pts, dict):
            cs = pts.get("coordinates")
        elif isinstance(pts, list):
            cs = pts
        elif isinstance(pts, str):
            return _decodificar_polyline(pts)
        if not isinstance(cs, list) or not cs:
            return None
        out = []
        for c in cs:
            if (isinstance(c, (list, tuple)) and len(c) >= 2):
                out.append([float(c[0]), float(c[1])])
        return out or None
    except Exception:
        return None


def _llamar_motor(gpx, perfil, url, timeout, gps_accuracy=MM_GPS_ACC_DEF):
    """POST del GPX a `/match`. Devuelve el dict JSON o None. Nunca lanza.

    `gps_accuracy` (metros) es CRÍTICO: sin él el motor asume una precisión muy
    fina y engancha los saltos de ruido del GPS produciendo zigzag (medido: 18x
    la distancia real en una traza de 46 km).
    """
    base = url or MM_URL
    try:
        q = urllib.parse.urlencode({"profile": perfil or PERFIL_DEF,
                                    "points_encoded": "false",
                                    "gps_accuracy": "%.0f" % float(gps_accuracy)})
        full = base + ("&" if "?" in base else "?") + q
        datos = gpx.encode("utf-8")
        req = urllib.request.Request(
            full, data=datos, method="POST",
            headers={"Content-Type": "application/gpx+xml",
                     "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=float(timeout or TIMEOUT_DEF)) as r:
            cuerpo = r.read()
        return json.loads(cuerpo.decode("utf-8", "replace"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            ValueError, TypeError):
        return None
    except Exception:
        return None


def _precision_de(puntos):
    """Mediana de la precisión (acc) de los puntos, acotada a [MIN, MAX].

    Los puntos son (ts, lat, lon, acc, ...). Si no hay dato utilizable se usa
    MM_GPS_ACC_DEF. Acotar evita los dos extremos malos: valores diminutos
    producen zigzag y valores enormes hacen que el motor no enganche nada.
    """
    vals = []
    for p in puntos or []:
        try:
            a = float(p[3])
        except (IndexError, TypeError, ValueError):
            continue
        if 0.5 <= a <= 5000:
            vals.append(a)
    if not vals:
        return MM_GPS_ACC_DEF
    vals.sort()
    med = vals[len(vals) // 2]
    return max(MM_GPS_ACC_MIN, min(MM_GPS_ACC_MAX, med))


def _alinear(vels, n):
    """Ajusta la lista de velocidades a exactamente n elementos (rellena None)."""
    v = list(vels or [])
    if len(v) < n:
        v.extend([None] * (n - len(v)))
    return v[:n]


def _vels_por_vertice(coords, puntos, vels):
    """Velocidad (km/h) de cada vértice = la del punto GPS más cercano.

    F5.25b — para pintar la línea matcheada con el COLOR DE LA VELOCIDAD (igual
    que la ruta GPS). La geometría y la traza avanzan en paralelo a lo largo del
    recorrido, así que basta con dos punteros (coste lineal) en vez de comparar
    cada vértice con todos los puntos.
    """
    if not vels or not puntos:
        return [None] * len(coords)
    n = len(puntos)
    k = 0
    out = []
    for c in coords:
        # PITFALL (medido): con descenso local (avanzar solo si el punto
        # siguiente está más cerca) el índice se quedaba atascado al principio
        # y TODA la línea heredaba la velocidad de los primeros metros (medido:
        # 1.199 vértices de un trayecto a 69 km/h salían a 1,2 km/h). Hay que
        # buscar el mínimo en una VENTANA hacia delante, sin retroceder: la
        # geometría y la traza avanzan en el mismo sentido.
        fin = min(n, k + VENTANA_VEL)
        mejor = k
        dmin = _dist2(c[0], c[1], puntos[k][2], puntos[k][1])
        for j in range(k + 1, fin):
            d = _dist2(c[0], c[1], puntos[j][2], puntos[j][1])
            if d < dmin:
                dmin = d
                mejor = j
        k = mejor
        v = vels[k] if k < len(vels) else None
        out.append(round(float(v), 1) if isinstance(v, (int, float)) else None)
    return out


def _dist2(lon1, lat1, lon2, lat2):
    """Distancia al cuadrado aproximada (equirectangular, escala local)."""
    dx = (lon1 - lon2) * 111320.0 * math.cos(math.radians((lat1 + lat2) / 2.0))
    dy = (lat1 - lat2) * 110540.0
    return dx * dx + dy * dy


def matchear(puntos, perfil="car", url=None, timeout=20.0, vels=None):
    """Pega una traza GPS a la red viaria vía el motor de map-matching.

    Parámetros
    ----------
    puntos : list
        Cronológica, tuplas ``(ts, lat, lon, ...)`` (ts epoch en segundos).
    perfil : str
        Perfil del motor (``"car"`` por defecto; ``"bike"``/``"foot"`` si el
        servicio los tiene cargados).
    url : str | None
        Endpoint `/match`. Por defecto ``TRACKCAM_MM_URL``.
    timeout : float
        Segundos por petición (por tramo).
    vels : list | None
        Velocidad (km/h) de cada punto, para devolver `vels` por vértice y poder
        pintar la línea con el color de la velocidad.

    Devuelve
    --------
    dict con ``coords`` ([[lon,lat],...] listo para GeoJSON), ``n_in``,
    ``n_matcheados``, ``dist_m`` y ``matchings``; o ``None`` si el motor no
    responde / falla / no devuelve geometría. NUNCA lanza excepción.

    `n_matcheados` = puntos de entrada incluidos en algún tramo que el motor
    resolvió (los de un tramo fallido no cuentan). `matchings` = nº de
    trayectorias devueltas por el motor.
    """
    try:
        pts = list(puntos or [])
        if not pts:
            return None

        tramos = _partir_en_tramos(pts)
        if not tramos:
            return None

        coords = []
        vv_out = []
        n_pts_prev = 0
        n_mat = 0
        dist = 0.0
        matchings = 0

        gps_acc = _precision_de(pts)
        for tramo in tramos:
            gpx = puntos_a_gpx(tramo)
            js = _llamar_motor(gpx, perfil, url, timeout, gps_accuracy=gps_acc)
            if not isinstance(js, dict):
                continue
            paths = js.get("paths")
            if not isinstance(paths, list):
                continue
            resuelto = False
            for path in paths:
                if not isinstance(path, dict):
                    continue
                cs = _coords_de_path(path)
                if not cs:
                    continue
                resuelto = True
                matchings += 1
                # F5.25b: velocidad de cada vértice por reparto PROPORCIONAL a lo
                # largo del tramo. La proximidad espacial fallaba cuando la
                # geometría es más densa que la traza (medido: en un rango largo
                # todos los vértices heredaban velocidades de los primeros
                # metros y la línea entera salía azul/verde en vez de naranja).
                if vels:
                    m_pts = len(tramo)
                    m_c = len(cs)
                    for _j in range(m_c):
                        _idx = n_pts_prev + int(round(_j * (m_pts - 1) / max(1, m_c - 1)))
                        _v = vels[_idx] if 0 <= _idx < len(vels) else None
                        vv_out.append(round(float(_v), 1) if isinstance(_v, (int, float)) else None)
                # Concatenar sin duplicar el punto de unión entre tramos.
                if coords and cs[0] == coords[-1]:
                    coords.extend(cs[1:])
                else:
                    coords.extend(cs)
                d = path.get("distance")
                if isinstance(d, (int, float)) and math.isfinite(float(d)) and d >= 0:
                    dist += float(d)
                else:
                    dist += _longitud(cs)
            if resuelto:
                n_mat += len(tramo)
            n_pts_prev += len(tramo)

        if not coords:
            return None
        return {"coords": coords,
                # las velocidades tienen que ir SIEMPRE alineadas con la
                # geometría (el frontend colorea por índice); si por lo que sea
                # falta alguno se rellena con None en vez de perder el color
                "vels": _alinear(vv_out, len(coords)),
                "n_in": len(pts),
                "n_matcheados": n_mat,
                "dist_m": round(dist, 1),
                "matchings": matchings}
    except Exception:
        # Red de seguridad total: el mapa no puede romperse por el matching.
        return None
