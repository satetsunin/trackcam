# -*- coding: utf-8 -*-
"""Filtros correctores de GPS (anti-deriva y autocompletado).

Lógica pura (sin IO) para poder testearla.

Criterios (calibrados con datos reales de Bilbao, Xiaomi M2004J19C):
  - La velocidad GPS (vel, m/s) es el discriminador principal: si el receptor
    dice que te mueves a > VEL_MOVIMIENTO (1.2 m/s ≈ 4.3 km/h, andar normal),
    el punto es movimiento real y se guarda SIEMPRE (aunque esté cerca del
    anterior — vas despacio o el fix llega cada 1-2 s).
  - Con vel baja (parado): se aplica anti-deriva — el GPS parado deriva 5-30 m
    y generaría cientos de puntos inútiles. Solo se acepta si el punto se ha
    alejado >= umbral del último aceptado (umbral = max(MIN_MOVIMIENTO_M,
    FACTOR_PRECISION * acc), típicamente 13-20 m con GPS de 5-8 m) o si han
    pasado LATIDO_S (60 s) — latido de presencia para no perder el hilo.
  - Autocompletado: huecos de 2-90 s (pérdida breve de GPS: túneles, calles
    estrechas, app en segundo plano un momento) se interpolan en línea recta
    si los extremos no están a distancia absurda (no es un salto real).
    Huecos mayores NO se inventan (el móvil estuvo congelado/apagado).
"""
import math

VEL_MOVIMIENTO = 1.2        # m/s: por encima = movimiento real garantizado
MIN_MOVIMIENTO_M = 8.0      # anti-deriva: umbral mínimo
FACTOR_PRECISION = 2.5      # umbral = max(MIN_MOVIMIENTO, FACTOR * acc)
LATIDO_S = 60.0             # si llevas parado más de 60 s, guarda un punto
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
                       min_mov=MIN_MOVIMIENTO_M,
                       factor_prec=FACTOR_PRECISION, latido_s=LATIDO_S):
    """pts: [(ts, lat, lon, acc, vel), ...] cronológico.

    Devuelve los puntos que representan movimiento real o latidos.
    El primer punto siempre se conserva (ancla).
    """
    if not pts:
        return []
    out = [pts[0]]
    ult = pts[0]
    for p in pts[1:]:
        _, lat, lon = p[0], p[1], p[2]
        acc = p[3] if len(p) > 3 and p[3] else 0
        vel = p[4] if len(p) > 4 and p[4] is not None else 0
        if vel and vel > vel_mov:
            # Movimiento real según el propio receptor GPS → guardar siempre
            out.append(p)
            ult = p
            continue
        acc = acc if acc and acc > 0 else 5.0
        umbral = max(min_mov, factor_prec * acc)
        d = _hav(ult[1], ult[2], lat, lon)
        dt = p[0] - ult[0]
        if d >= umbral or dt >= latido_s:
            out.append(p)
            ult = p
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


def limpiar_track(filas):
    """Pipeline completo. filas: [(ts, lat, lon, acc, vel), ...] cronológico.
    Devuelve [(ts, lat, lon), ...] filtrado + autocompletado."""
    pts = [(r[0], r[1], r[2],
            r[3] if len(r) > 3 else 0,
            r[4] if len(r) > 4 else 0) for r in filas]
    limpios = filtro_anti_deriva(pts)
    return autocompletar_huecos([(p[0], p[1], p[2]) for p in limpios])
