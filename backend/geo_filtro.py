# -*- coding: utf-8 -*-
"""Filtros correctores de GPS (anti-deriva y autocompletado).

Lógica pura (sin IO) para poder testearla.

Criterios (recalibrados 2026-09-05 con datos reales de Bilbao, Xiaomi
M2004J19C — el Redmi reporta vel GPS 0 o fantasma casi siempre, así que la
velocidad NO es un discriminador fiable de movimiento):

  - El discriminador es el DESPLAZAMIENTO REAL EN VENTANA: se compara cada
    punto con el de ~VENTANA_POS_S (60 s) antes. Si entre ambos hay >=
    UMBRAL_POS_M (15 m), hubo movimiento real en ese minuto → el punto se
    guarda SIEMPRE (línea continua en viajes, incluso en coche).
  - Sin movimiento (parado/deriva): el GPS deriva 5-30 m dibujando garabatos.
    Solo se guarda un LATIDO cada LATIDO_S (120 s) para no perder el hilo
    temporal del track (y no romper la línea con cientos de puntos de deriva).
  - La vel GPS (> VEL_MOVIMIENTO) solo REFUERZA el movimiento cuando el
    receptor sí la reporta; nunca descarta por vel baja (Redmi = vel 0).
  - Autocompletado: huecos de 2-90 s (pérdida breve de GPS: túneles, calles
    estrechas, app en segundo plano un momento) se interpolan en línea recta
    si los extremos no están a distancia absurda (no es un salto real).
    Huecos mayores NO se inventan (el móvil estuvo congelado/apagado).
"""
import math

VEL_MOVIMIENTO = 1.2        # m/s: refuerzo (si el receptor la reporta)
VENTANA_POS_S = 60.0        # ventana de comparación de posición (anti-deriva)
UMBRAL_POS_M = 15.0         # desplazamiento en la ventana = movimiento real
LATIDO_S = 120.0            # parado: guardar 1 punto cada 2 min (hilo)
HUECO_MAX_S = 90.0          # huecos <= 90 s se autocompletan
INTERP_S = 2.0              # paso de interpolación
DIST_SALTO_MAX_M = 600.0    # si los extremos están a más de 600 m no interpolar


def _hav(lat1, lon1, lat2, lon2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def _interpolar(a, b, frac):
    return (a[0] + (b[0] - a[0]) * frac,
            a[1] + (b[1] - a[1]) * frac,
            a[2] + (b[2] - a[2]) * frac)


def filtro_anti_deriva(pts, vel_mov=VEL_MOVIMIENTO,
                       ventana_s=VENTANA_POS_S, umbral_m=UMBRAL_POS_M,
                       latido_s=LATIDO_S, zonas=None):
    """pts: [(ts, lat, lon, acc, vel), ...] cronológico.

    Devuelve los puntos que representan movimiento real (o latidos cada
    latido_s cuando estás parado, para no perder el hilo).

    Discriminador por POSICIÓN en ventana (no por vel GPS — el Redmi la
    reporta 0 o fantasma): si el punto actual está a >= umbral_m del punto
    de hace ~ventana_s segundos, hubo movimiento real → guardar. Si no,
    deriva/parado → solo latidos espaciados.

    zonas: lista de zonas {lat, lon, radio_m}. Un punto PARADO que cae en
    una zona NO se guarda (ni latido): la zona de no-monitorización elimina
    la deriva en casa/bar. El MOVIMIENTO real dentro de una zona SÍ se
    guarda (si sales andando de casa, la línea arranca en tu puerta).
    """
    if not pts:
        return []
    n = len(pts)
    # ancla: el primer punto se conserva (para no perder el arranque del
    # track) salvo que caiga en una zona de no-monitorización: si empiezas
    # a grabar en casa, la línea debe arrancar donde sales de la zona.
    out = [] if (zonas and _en_zona_lista(zonas, pts[0][1], pts[0][2])) else [pts[0]]
    ult_guardado_ts = pts[0][0]
    j = 0                   # índice del punto ~ventana_s atrás
    for i in range(1, n):
        ts_i = pts[i][0]
        # avanzar j hasta el último punto con ts <= ts_i - ventana
        while j < i - 1 and pts[j + 1][0] <= ts_i - ventana_s:
            j += 1
        ref = pts[j] if pts[j][0] <= ts_i - ventana_s else pts[0]
        d = _hav(ref[1], ref[2], pts[i][1], pts[i][2])
        vel = pts[i][4] if len(pts[i]) > 4 and pts[i][4] else 0
        if d >= umbral_m or (vel and vel > vel_mov):
            # movimiento real en la ventana (o vel GPS alta como refuerzo)
            out.append(pts[i])
            ult_guardado_ts = ts_i
        elif ts_i - ult_guardado_ts >= latido_s:
            # parado: latido de presencia para mantener el hilo temporal,
            # salvo si estamos dentro de una zona de no-monitorización
            if not zonas or not _en_zona_lista(zonas, pts[i][1], pts[i][2]):
                out.append(pts[i])
                ult_guardado_ts = ts_i
    return out


def _en_zona_lista(zonas, lat, lon):
    """True si (lat, lon) cae en alguna zona de la lista."""
    for z in zonas:
        if _hav(z["lat"], z["lon"], lat, lon) <= z["radio_m"] + 2.0:
            return True
    return False


def filtro_saltos(pts, salto_m=100.0, vuelta_m=60.0):
    """Elimina SALTOS DE RED: puntos aislados que se van lejos y vuelven.

    Patrón MIUI/Doze del Redmi al perder GPS: la posición salta a una
    torre de telefonía (100-500 m) durante 1-2 fixes y vuelve. Un punto i
    es salto si está lejos de su anterior Y de su siguiente, pero el
    anterior y el siguiente están cerca entre sí (fue y volvió).
    El movimiento real NO se toca: en coche los puntos avanzan (el
    anterior y el siguiente también están lejos entre sí).
    """
    if len(pts) < 3:
        return list(pts)
    out = []
    for i, p in enumerate(pts):
        if 0 < i < len(pts) - 1:
            a, b = pts[i - 1], pts[i + 1]
            d_ant = _hav(a[1], a[2], p[1], p[2])
            d_sig = _hav(p[1], p[2], b[1], b[2])
            d_ab = _hav(a[1], a[2], b[1], b[2])
            if d_ant > salto_m and d_sig > salto_m and d_ab < vuelta_m:
                continue  # salto de ida y vuelta → descartar
        out.append(p)
    return out


def colapsar_estancias(pts, radio_m=40.0, tiempo_s=480.0):
    """pts: [(ts, lat, lon, ...)...] ya filtrados.

    Una ESTANCIA = serie de puntos que se mantiene dentro de un radio de
    radio_m (40 m) durante al menos tiempo_s (8 min) — estás parado en un
    sitio (bar, casa, visita) y el GPS deriva alrededor. Se colapsa a UN
    punto: la línea llega al sitio, se queda, y al salir arranca desde ahí.
    Robusto a la deriva errática: no mira velocidades entre consecutivos,
    solo si todo el grupo cabe en la burbuja.
    """
    if len(pts) < 3:
        return list(pts)
    out = []
    i = 0
    n = len(pts)
    while i < n:
        ref = pts[i]
        j = i
        # extender mientras los puntos sigan dentro del radio de la burbuja
        while j + 1 < n:
            d = _hav(ref[1], ref[2], pts[j + 1][1], pts[j + 1][2])
            if d <= radio_m:
                j += 1
            else:
                break
        dur = pts[j][0] - pts[i][0]
        if dur >= tiempo_s and j > i:
            # estancia larga en la burbuja → un solo punto (el primero)
            out.append(ref)
            i = j + 1
        else:
            out.append(pts[i])
            i += 1
    return out


def autocompletar_huecos(pts, hueco_max=HUECO_MAX_S, paso=INTERP_S,
                         salto_max=DIST_SALTO_MAX_M,
                         vel_min=0.8, vel_max=30.0):
    """pts: [(ts, lat, lon), ...] cronológico. Interpola huecos cortos.

    Solo rellena cuando entre los extremos hubo desplazamiento real: la
    velocidad implícita (distancia/tiempo) debe estar entre vel_min y
    vel_max m/s. Así NO se interpolan los latidos de presencia (parado en
    casa: vel implícita ~0) ni saltos absurdos, pero sí una pérdida breve de
    GPS mientras se caminaba/conducía.
    """
    if len(pts) < 2:
        return list(pts)
    out = [pts[0]]
    for i in range(1, len(pts)):
        a, b = out[-1], pts[i]
        dt = b[0] - a[0]
        d = _hav(a[1], a[2], b[1], b[2])
        v_imp = (d / dt) if dt > 0 else 0
        if (paso < dt <= hueco_max and d <= salto_max
                and vel_min <= v_imp <= vel_max):
            n = int(dt // paso)
            for k in range(1, n):
                out.append(_interpolar(a, b, k / n))
        out.append(b)
    return out


def limpiar_track(filas, zonas=None):
    """Pipeline completo. filas: [(ts, lat, lon, acc, vel), ...] cronológico.
    Devuelve [(ts, lat, lon), ...] filtrado + colapsado + autocompletado.

    zonas: lista de zonas {lat, lon, radio_m} (no-monitorización). El
    anti-deriva no deja latidos dentro de zona; las estancias largas fuera
    de zona se colapsan a entrada+salida.
    """
    pts = [(r[0], r[1], r[2],
            r[3] if len(r) > 3 else 0,
            r[4] if len(r) > 4 else 0) for r in filas]
    pts = filtro_saltos(pts)                 # 1º: saltos de red (ida y vuelta)
    limpios = filtro_anti_deriva(pts, zonas=zonas)   # 2º: anti-deriva + zonas
    limpios = colapsar_estancias([(p[0], p[1], p[2]) for p in limpios])
    return autocompletar_huecos(limpios)
