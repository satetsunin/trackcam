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
# F5.18 — criterio de ANCLA (estancia vs movimiento) que sustituye en la
# práctica al umbral de 15 m/60 s: parado en la calle el GPS deriva más que
# eso y el filtro antiguo pintaba garabatos (Gernika: 1.931 puntos en 4 h
# quieto). Ver filtro_anti_deriva().
RADIO_ESTANCIA_M = 110.0    # red de seguridad por radio (además de coherencia)
T_ESTANCIA_S = 240.0        # 4 min dentro del radio → estancia
LATIDO_ESTANCIA_S = 240.0   # en estancia: 1 punto cada 4 min
# F5.20 — CRITERIO DE COHERENCIA DE TRAYECTORIA (distinguir deriva GPS de
# movimiento real, aunque sea lento): eficiencia = neto/recorrido en ventana.
VENT_COHERENCIA_S = 300.0   # ventana de análisis (5 min)
EF_MOV = 0.40               # neto/recorrido >= 0,40 y neto >= 50 m → moviendo
EF_DER = 0.25               # neto/recorrido < 0,25 → deriva errática (parado)
MIN_NETO_M = 50.0           # desplazamiento neto mínimo para hablar de movimiento
RADIO_SALIDA_M = 130.0      # (legado del criterio de radio)
CONF_SALIDA = 4             # (legado)
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
                       latido_s=LATIDO_S, zonas=None,
                       vent_coherencia_s=VENT_COHERENCIA_S,
                       ef_mov=EF_MOV, ef_der=EF_DER,
                       min_neto_m=MIN_NETO_M,
                       latido_est_s=LATIDO_ESTANCIA_S,
                       r_estancia_m=RADIO_ESTANCIA_M,
                       t_estancia_s=T_ESTANCIA_S):
    """pts: [(ts, lat, lon, acc, vel), ...] cronológico.

    Devuelve los puntos que representan movimiento real (y latidos muy
    espaciados cuando estás quieto).

    F5.20 — CRITERIO DE COHERENCIA DE TRAYECTORIA (sustituye al de radio):
    el usuario: «patrones erráticos quieren decir que es problema de recepción
    del GPS; si detecta que voy en línea o haciendo curvas es movimiento real,
    aunque sea muy lento». Es exactamente esto:

        eficiencia = desplazamiento NETO / recorrido ACUMULADO
        (en una ventana de vent_coherencia_s)

      · Deriva GPS (parado): los puntos van y vuelven alrededor del mismo
        sitio → recorrido largo con neto pequeño → eficiencia ~0,1-0,2.
      · Movimiento real (línea recta o curvas suaves): avanzas hacia algún
        sitio → eficiencia ~0,6-1,0 AUNQUE vayas lento (andando despacio la
        eficiencia sigue siendo alta porque el neto crece con el recorrido).
      · Desplazamiento incoherente (saltos de red): eficiencia baja.

    Umbrales con histéresis (ef_mov / ef_der) para no oscilar en los bordes.
    Verificado con datos reales: estancia de 4 h en Gernika (recorrido de
    deriva grande, neto 62 m) → 157 puntos servidos en 4 h (antes 449 con el
    criterio de radio y 1.931 con el antiguo), conservando el 81 % del
    movimiento real de los paseos.

    zonas: lista de zonas {lat, lon, radio_m}. Un punto PARADO que cae en
    una zona NO se guarda (ni latido): la zona de no-monitorización elimina
    la deriva en casa/bar. El MOVIMIENTO real dentro de una zona SÍ se
    guarda (si sales andando de casa, la línea arranca en tu puerta).
    """
    if not pts:
        return []
    n = len(pts)
    # recorrido acumulado entre puntos consecutivos (para la eficiencia)
    pref = [0.0] * n
    for k in range(1, n):
        pref[k] = pref[k - 1] + _hav(pts[k - 1][1], pts[k - 1][2],
                                     pts[k][1], pts[k][2])
    _en_z0 = bool(zonas) and _en_zona_lista(zonas, pts[0][1], pts[0][2])
    out = [] if _en_z0 else [pts[0]]
    ult_guardado_ts = pts[0][0]
    modo = "mov"
    j0 = 0                      # inicio de la ventana de coherencia
    j_cap = 0                   # inicio de la ventana del criterio de radio
    for i in range(1, n):
        ts_i, la, lo = pts[i][0], pts[i][1], pts[i][2]
        ts_ini = ts_i - vent_coherencia_s
        while j0 < i and pts[j0][0] < ts_ini:
            j0 += 1
        rec = pref[i] - pref[j0]
        neto = _hav(pts[j0][1], pts[j0][2], la, lo)
        ef = (neto / rec) if rec > 1.0 else 0.0
        en_zona = bool(zonas) and _en_zona_lista(zonas, la, lo)
        # 1) coherencia de trayectoria
        if ef >= ef_mov and neto >= min_neto_m:
            modo = "mov"
        elif ef < ef_der or rec < 30.0 or neto < min_neto_m:
            # deriva/parado: además de la eficiencia baja, si en 5 min NO te has
            # desplazado ni MIN_NETO_M metros estás quieto (la deriva "en línea"
            # puede dar eficiencia alta en tramos cortos). Verificado: esto
            # quita los tramos densos dentro de una estancia (mediana de
            # separación 5 s → 241 s) sin perder movimiento.
            modo = "der"
        # 2) red de seguridad por radio (parado: sin alejarse del ancla en
        #    t_estancia_s aunque la eficiencia salga alta por casualidad)
        ts_cap = ts_i - t_estancia_s
        while j_cap < i and pts[j_cap][0] < ts_cap:
            j_cap += 1
        if _hav(pts[j_cap][1], pts[j_cap][2], la, lo) <= r_estancia_m:
            if modo != "der" and ts_i - pts[j_cap][0] >= t_estancia_s:
                modo = "der"
        if modo == "mov":
            if not en_zona:
                out.append(pts[i])
                ult_guardado_ts = ts_i
        elif ts_i - ult_guardado_ts >= latido_est_s and not en_zona:
            # estancia: 1 punto cada ~4 min (deriva colapsada)
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
    sitio (bar, casa, visita) y el GPS deriva alrededor. Se colapsa a
    ENTRADA + SALIDA (el primero y el último de la serie, con su ts real):
    la línea llega al sitio, el tramo corto entre ambos representa la
    estancia, y la salida enlaza con el movimiento que reanudas. Emitir
    solo 1 punto dejaría un hueco visual entre la llegada y la salida.
    Robusto a la deriva errática: no mira velocidades entre consecutivos,
    solo si todo el grupo cabe en la burbuja.

    RENDIMIENTO (F5.16): con series densas (el móvil manda ~2 pts/s) este
    bucle es el 99 % del tiempo del pipeline (165 s con 150k puntos). Se usa
    distancia EQUIRECTANGULAR (sin trig por comparación salvo 1 cos al mover
    el centroide) — error <0,1 % a escala de 40 m, resultado equivalente.
    """
    if len(pts) < 3:
        return list(pts)
    n = len(pts)
    ts_l = [p[0] for p in pts]
    lat_l = [p[1] for p in pts]
    lon_l = [p[2] for p in pts]
    out = []
    i = 0
    while i < n:
        # centroide acumulado de la serie (la deriva de 2 h puede recorrer
        # ~40 m; fijar el ref en el primer punto partiría la estancia en 2)
        clat = lat_l[i]; clon = lon_l[i]; cnt = 1
        j = i
        _cosf = math.cos(math.radians(clat)) * 111320.0
        _ky = 110540.0
        _r2 = radio_m * radio_m
        # extender mientras el punto quepa en la burbuja del centroide
        while j + 1 < n:
            dy = (lat_l[j + 1] - clat) * _ky
            dx = (lon_l[j + 1] - clon) * _cosf
            if dx * dx + dy * dy <= _r2:
                j += 1
                clat = (clat * cnt + lat_l[j]) / (cnt + 1)
                clon = (clon * cnt + lon_l[j]) / (cnt + 1)
                cnt += 1
                _cosf = math.cos(math.radians(clat)) * 111320.0
            else:
                break
        dur = ts_l[j] - ts_l[i]
        if dur >= tiempo_s and j > i:
            # estancia larga en la burbuja → entrada y salida (con ts real)
            out.append(pts[i])
            out.append(pts[j])
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


def adelgazar(pts, min_dt):
    """Conserva 1 punto por cada min_dt s (el primero de cada tramo).
    Usado solo en series enormes, donde el colapso de estancias se dispara."""
    if min_dt <= 1 or len(pts) < 2:
        return list(pts)
    out = [pts[0]]
    ult = pts[0][0]
    for p in pts[1:]:
        if p[0] - ult >= min_dt:
            out.append(p)
            ult = p[0]
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
    limpios = [(p[0], p[1], p[2]) for p in limpios]
    # RENDIMIENTO (F5.16): el colapso de estancias cuesta ~n·k (k = puntos en
    # una burbuja de 8 min). Con rangos enormes («todo»: semanas, 150k+ pts)
    # eran 90 s y el mapa se quedaba cargando. Se adelgaza la serie con un
    # espaciado mínimo creciente con n: solo afecta al DIBUJO del rango largo
    # (el frontend ya submuestrea) — la distancia de paso de cada evento y el
    # reproductor /video trabajan por día, con series pequeñas y sin adelgazar.
    n = len(limpios)
    if n > 60000:
        dt_min = 3.0 if n <= 120000 else (5.0 if n <= 240000 else 8.0)
        limpios = adelgazar(limpios, dt_min)
    limpios = colapsar_estancias(limpios)
    return autocompletar_huecos(limpios)
