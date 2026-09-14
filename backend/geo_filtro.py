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
CONF_MOV_M = 3              # puntos seguidos para CONFIRMAR movimiento (histéresis)
RADIO_SALIDA_ESTANCIA_M = 200.0  # (legado) alejarse del sitio de la estancia
# F5.23: VENTANA CORTA de movimiento. Con la ventana larga (5 min) los
# arranques tras una parada y los paseos lentos se colapsaban: se perdían
# 1.508 puntos de RUTA REAL en un día y el 6,5 % del recorrido (curvas
# cortadas). Ahora, para SALIR de la estancia basta con que el movimiento se
# confirme en 2 min (neto ≥60 m con eficiencia ≥0,5): el paseo se dibuja
# entero aunque acabe de arrancar.
VENT_MOV_S = 120.0          # ventana corta (2 min)
EF_MOV_CORTO = 0.50         # neto/recorrido en la ventana corta
# F5.24 — el criterio de deriva pasa a medir RECORRIDO ACUMULADO, no "¿volviste
# al mismo sitio?": dar vueltas por un pueblo (llegada a Gernika) o un paseo de
# ida y vuelta tienen desplazamiento neto pequeño pero SON movimiento real. Con
# el criterio viejo se colapsaban (la llegada a Gernika perdía el 83 % del
# recorrido y sus giros). Deriva = el GPS te mueve poco ACUMULADO.
# La métrica decisiva es la VELOCIDAD MEDIA, no el recorrido total ni el neto:
#   deriva de Gernika (parado 4 h): 1.819 m / 240 min = 7,6 m/min (0,45 km/h)
#   llegada dando vueltas:          4.000 m /  40 min =   100 m/min (6 km/h)
#   paseo:                          9.180 m /  30 min =   306 m/min
# Un recorrido total alto NO significa movimiento: puede ser deriva acumulada.
MIN_VEL_MOV = 75.0          # >=75 m/min → moviendo (MEDIDO en sus datos:
                            # jitter del GPS parado 60 m/min · vaivén 92 · andar
                            # 83-92 · coche 1.160; el corte limpio está en 75)
MIN_VEL_DER = 60.0          # <=60 m/min → parado/deriva (jitter medido)
MIN_RUMB_MOV = 0.65         # coherencia de rumbo para considerar movimiento
MIN_RUMB_DER = 0.45         # por debajo: direcciones aleatorias → jitter
MIN_DMAX_MOV = 60.0         # haberse alejado >=60 m del inicio de la ventana
MIN_REC_MOV = 60.0          # equivalente en recorrido para la ventana corta
MIN_REC_DER = 60.0          # equivalente en recorrido para la ventana larga
MIN_REC_DER2 = 250.0
MIN_NETO_CORTO = 60.0       # desplazamiento neto en 2 min para considerar movimiento
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


def _vel_mediana(pts, i, k=40, dt_min_s=0.5):
    """Mediana de la VELOCIDAD INSTANTÁNEA (m/min) de los últimos k puntos.

    F5.24 — es la métrica que separa la deriva del movimiento real, y es
    inmune al multipath: parado con GPS bueno la mediana del salto entre
    puntos es ~0,3 m (7 m/min) aunque algunos saltos de reflexión lleguen a
    100 m e inflen la media y el recorrido acumulado (medido: una estancia de
    4 h con recorrido aparente de 10 km = 53 m/min falsos). Caminando la
    mediana es ~1,2 m por punto (~33 m/min).
    """
    j = max(1, i - k + 1)
    vs = []
    for z in range(j, i + 1):
        dt = pts[z][0] - pts[z - 1][0]
        if dt < dt_min_s:
            continue
        vs.append(_hav(pts[z - 1][1], pts[z - 1][2], pts[z][1], pts[z][2])
                  / (dt / 60.0))
    if not vs:
        return 0.0
    vs.sort()
    return vs[len(vs) // 2]


def _coherencia(pts, i, k=45):
    """(coherencia de rumbo R, mediana de velocidad m/min) de los últimos k puntos.

    F5.24 — R es la longitud del vector resultante medio (estadística circular):
    vale ~1 cuando todos los desplazamientos van en la MISMA dirección (andar,
    ir en línea, trazar una curva) y ~0 cuando van y vienen en direcciones
    aleatorias (deriva/jitter del GPS parado). Es el discriminante que pedía el
    usuario: "movimiento continuado lógico" = rumbo coherente.

    Necesario porque la velocidad sola no basta: con el móvil emitiendo un
    punto cada ~0,4 s, el jitter de 0,3 m parado equivale a 45 m/min (2,7 km/h),
    indistinguible de andar despacio. Lo que los separa es la DIRECCIÓN.
    """
    j = max(1, i - k + 1)
    sx = sy = 0.0
    n = 0
    vs = []
    for z in range(j, i + 1):
        dt = pts[z][0] - pts[z - 1][0]
        if dt <= 0:
            continue
        dx = (pts[z][2] - pts[z - 1][2]) * 111320.0 * math.cos(math.radians(pts[z][1]))
        dy = (pts[z][1] - pts[z - 1][1]) * 110540.0
        d = math.hypot(dx, dy)
        if d < 0.25:            # sin desplazamiento: ni cuenta ni rompe la racha
            continue
        sx += dx / d
        sy += dy / d
        n += 1
        if dt >= 0.5:
            vs.append(d / (dt / 60.0))
    r = (math.hypot(sx, sy) / n) if n else 0.0
    # desplazamiento MÁXIMO desde el inicio de la ventana: en un vaivén (ir y
    # volver por la misma calle) el rumbo global es incoherente, pero te has
    # alejado de verdad; el jitter parado nunca se aleja decenas de metros.
    # Mediana (no máximo) de la distancia al inicio de la ventana: en un vaivén
    # te has alejado de verdad y se mantiene alta; el jitter se queda bajo y los
    # saltos aislados de multipath no la mueven (el máximo sí, y por eso no vale).
    ds = sorted(_hav(pts[j][1], pts[j][2], pts[z][1], pts[z][2])
                for z in range(j, i + 1))
    dmed = ds[len(ds) // 2] if ds else 0.0
    if not vs:
        return (r, 0.0, dmed)
    vs.sort()
    return (r, vs[len(vs) // 2], dmed)


def _vel_mediana_tramo(pts, i, j, dt_min_s=0.5):
    """Mediana de la velocidad instantánea entre los índices i y j."""
    vs = []
    for z in range(i + 1, min(j + 1, len(pts))):
        dt = pts[z][0] - pts[z - 1][0]
        if dt < dt_min_s:
            continue
        vs.append(_hav(pts[z - 1][1], pts[z - 1][2], pts[z][1], pts[z][2])
                  / (dt / 60.0))
    if not vs:
        return 0.0
    vs.sort()
    return vs[len(vs) // 2]


def filtro_anti_deriva(pts, vel_mov=VEL_MOVIMIENTO,
                       ventana_s=VENTANA_POS_S, umbral_m=UMBRAL_POS_M,
                       latido_s=LATIDO_S, zonas=None,
                       vent_coherencia_s=VENT_COHERENCIA_S,
                       ef_mov=EF_MOV, ef_der=EF_DER,
                       min_neto_m=MIN_NETO_M,
                       conf_mov=CONF_MOV_M,
                       r_sal_est=RADIO_SALIDA_ESTANCIA_M,
                       vent_mov_s=VENT_MOV_S,
                       ef_mov_c=EF_MOV_CORTO,
                       min_neto_c=MIN_NETO_CORTO,
                       min_rec_mov=MIN_REC_MOV,
                       min_rec_der=MIN_REC_DER,
                       min_rec_der2=MIN_REC_DER2,
                       min_vel_mov=MIN_VEL_MOV,
                       min_vel_der=MIN_VEL_DER,
                       min_rumb_mov=MIN_RUMB_MOV, vent_rumbo_k=45,
                       min_dmax_mov=MIN_DMAX_MOV,
                       min_rumb_der=MIN_RUMB_DER,
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
    der_emitido = False
    mov_consec = 0              # puntos seguidos con criterio de movimiento
    mov_ini_ts = 0              # inicio de la racha de movimiento
    est_lat, est_lon = pts[0][1], pts[0][2]
    _ULT_VM = [0, 0.0]          # caché de la mediana de velocidad
    j0 = 0                      # inicio de la ventana LARGA de coherencia
    jc = 0                      # inicio de la ventana CORTA (movimiento)
    j_cap = 0                   # inicio de la ventana del criterio de radio
    for i in range(1, n):
        ts_i, la, lo = pts[i][0], pts[i][1], pts[i][2]
        ts_ini = ts_i - vent_coherencia_s
        while j0 < i and pts[j0][0] < ts_ini:
            j0 += 1
        rec = pref[i] - pref[j0]
        neto = _hav(pts[j0][1], pts[j0][2], la, lo)
        ef = (neto / rec) if rec > 1.0 else 0.0
        # ventana CORTA (2 min): para reconocer rápido que te has puesto en
        # marcha (arranques tras parada, paseos lentos)
        ts_mov = ts_i - vent_mov_s
        while jc < i and pts[jc][0] < ts_mov:
            jc += 1
        rec_c = pref[i] - pref[jc]
        neto_c = _hav(pts[jc][1], pts[jc][2], la, lo)
        ef_c = (neto_c / rec_c) if rec_c > 1.0 else 0.0
        # F5.24: basta con haberse movido >= min_rec_mov en la ventana corta
        # (antes exigía eficiencia y desplazamiento NETO, y eso descartaba dar
        # vueltas o ir y volver, que es movimiento real)
        # F5.24: la mediana de la velocidad instantánea decide (robusta al
        # multipath). Se recalcula cada 8 puntos por coste.
        if i - _ULT_VM[0] >= 8 or _ULT_VM[1] == 0.0:
            _ULT_VM[0] = i
            _ULT_VM[1] = _coherencia(pts, i, k=vent_rumbo_k)
        rumb, vel_med, dmed = _ULT_VM[1]
        # movimiento real = rumbo COHERENTE con algo de movimiento, o te has
        # alejado de verdad (vaivén: rumbo incoherente pero desplazamiento real),
        # o mucha velocidad (un coche tiene giros, no jitter)
        # OJO: dmax (desplazamiento máximo) NO se usa para declarar movimiento:
        # un salto de multipath aislado lo infla y hacía que una estancia parado
        # volviera a emitir puntos (medido: 1.373 en vez de 1). El rumbo y la
        # velocidad sí son robustos.
        mov_corto = (vel_med >= min_vel_mov) \
            or (rumb >= min_rumb_mov and vel_med >= 45.0)
        en_zona = bool(zonas) and _en_zona_lista(zonas, la, lo)
        # 1) coherencia de trayectoria: ¿el criterio dice movimiento o deriva?
        if mov_corto or (ef >= ef_mov and neto >= min_neto_m and rec >= min_rec_mov):
            if mov_consec == 0:
                mov_ini_ts = ts_i
            mov_consec += 1
        else:
            mov_consec = 0
            mov_ini_ts = 0
        # F5.24: deriva = recorrido acumulado bajo. Si te moviste mucho
        # (>= min_rec_der2), NO es deriva aunque vuelvas al punto de partida.
        # F5.24: deriva = te mueves DESPACIO (velocidad media baja). Un
        # recorrido alto con neto bajo pero rápido es movimiento real (dar
        # vueltas, ir y volver), y no puede confundirse con la deriva parada.
        # parado = se mueve poco, o va en direcciones aleatorias (jitter) sin ir
        # rápido: un coche en curva tiene rumbo bajo pero velocidad muy alta
        es_der = (vel_med <= min_vel_der) \
            or (rumb < min_rumb_der and vel_med < min_vel_mov)
        if modo == "mov":
            if es_der:
                modo = "der"
                der_emitido = False
                est_lat, est_lon = la, lo      # sitio de la estancia
        else:
            # EN ESTANCIA: solo se sale si te ALEJAS de verdad del sitio
            # (r_sal_est) y el movimiento se confirma conf_mov puntos seguidos.
            # Así la deriva (que oscila dentro del radio) no rompe la estancia
            # en trozos, que era lo que dejaba 42 puntos en la estancia de
            # Gernika en lugar de 1.
            # F5.23: se sale de la estancia en cuanto el movimiento se confirma
            # en la ventana corta (antes exigía alejarse 200 m del sitio, y eso
            # borraba el inicio de cada paseo y los paseos cortos enteros: se
            # perdían 1.508 puntos de ruta real y curvas). La limpieza de la
            # parada la hace ahora unificar_estancias() por separado.
            # F5.24: se sale de la estancia si llevas >=60 s moviéndote, estás a
            # >=100 m del ancla Y en el último minuto NO has pasado cerca de ella
            # (si has vuelto, era una racha de deriva, no movimiento real).
            # F5.24: se sale de la estancia por VELOCIDAD SOSTENIDA, no por
            # alejarse del ancla: un paseo de ida y vuelta (dar vueltas en una
            # plaza, ir y volver por la misma calle) nunca se aleja 100 m del
            # ancla, así que la condición anterior lo dejaba dentro de la
            # estancia y borraba sus giros (llegada a Gernika: 26 % conservado).
            # La deriva parada se mueve a ~8 m/min y no alcanza este umbral.
            j_sal = i
            while j_sal > 0 and pts[j_sal - 1][0] >= ts_i - 90.0:
                j_sal -= 1
            vel_sal = (pref[i] - pref[j_sal]) / max(1.0, (ts_i - pts[j_sal][0]) / 60.0)
            if mov_consec >= conf_mov and mov_corto:
                modo = "mov"
        # 2) red de seguridad por radio (parado: sin alejarse del punto de hace
        #    t_estancia_s aunque la eficiencia salga alta por casualidad)
        ts_cap = ts_i - t_estancia_s
        while j_cap < i and pts[j_cap][0] < ts_cap:
            j_cap += 1
        # F5.24: la red de seguridad por radio SOLO puede declarar parado si
        # además te mueves despacio. Antes bastaba con estar en un área pequeña,
        # y eso colapsaba los paseos de ida y vuelta (dar vueltas en una plaza,
        # ir y volver por la misma calle: movimiento real que no se aleja): la
        # llegada a Gernika perdía el 88 % de sus puntos y sus giros.
        if modo != "der" and rumb < min_rumb_der \
                and ts_i - pts[j_cap][0] >= t_estancia_s \
                and _hav(pts[j_cap][1], pts[j_cap][2], la, lo) <= r_estancia_m:
            modo = "der"
            der_emitido = False
            est_lat, est_lon = la, lo
        if modo == "mov":
            if not en_zona:
                out.append(pts[i])
                ult_guardado_ts = ts_i
            der_emitido = False
        elif not der_emitido:
            # F5.22 UNIFICACIÓN: una estancia = UN SOLO PUNTO (el sitio donde
            # te quedaste). Antes se emitían latidos cada 4 min y, como la
            # deriva del GPS cae en posiciones distintas, la línea dibujaba
            # hasta 1.800 m de recorrido falso y una nube de decenas de puntos
            # dentro de un radio de 170 m (medido en la estancia de Gernika).
            # Con un único punto: la línea llega, se queda y sale del sitio.
            if not en_zona:
                out.append(pts[i])
                ult_guardado_ts = ts_i
            der_emitido = True
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


def unificar_estancias(pts, radio_m=200.0, min_dur_s=600.0, min_neto_m=150.0,
                       max_vel_m_min=15.0, min_rumb_der=MIN_RUMB_DER,
                       min_dmax_mov=MIN_DMAX_MOV, min_dur_larga_s=7200.0,
                       min_neto_larga_m=300.0, encadenada=False, paso_max_m=300.0):
    """F5.23 — Una PARADA se sustituye por UN SOLO PUNTO.

    Un tramo que se mantiene dentro de un radio de radio_m durante al menos
    min_dur_s y con desplazamiento neto pequeño (<= min_neto_m) es una parada
    (GPS derivando en el mismo sitio): se emite solo su primer punto. Así la
    estancia de Gernika (4 h, excursión 169 m) pasa de cientos de puntos y
    1.800 m de recorrido falso a UN punto, sin tocar la ruta real.

    Se aplica DESPUÉS del filtro de movimiento: el filtro puede ser generoso
    (conserva todo el movimiento, sin cortar curvas) y este paso elimina la
    deriva de los ratos parado.
    """
    if not pts:
        return []
    out = []
    i = 0
    n = len(pts)
    while i < n:
        la, lo = pts[i][1], pts[i][2]
        j = i
        s_lat = pts[i][1]
        s_lon = pts[i][2]
        cnt = 1
        # Extensión mientras los puntos quepan en el radio del centroide. SIN
        # tope de tiempo: una estancia de 4 h se extiende entera y luego se
        # salta de golpe (i = j+1), así que el coste sigue siendo lineal. El
        # tope anterior (min_dur_s) cortaba la extensión antes de alcanzar la
        # duración mínima cuando los puntos venían espaciados → la unificación
        # no hacía nada.
        while j + 1 < n and (j - i) < 20000:
            if encadenada:
                # extensión punto a punto: sigue la deriva aunque el conjunto se
                # extienda más que el radio (una estancia de 4 h con jitter de
                # 200 m se cortaba al salirse del centroide y no se unificaba)
                cabe = _hav(pts[j][1], pts[j][2], pts[j + 1][1],
                            pts[j + 1][2]) <= paso_max_m
            else:
                clat = s_lat / cnt
                clon = s_lon / cnt
                cabe = _hav(clat, clon, pts[j + 1][1], pts[j + 1][2]) <= radio_m
            if cabe:
                j += 1
                s_lat += pts[j][1]
                s_lon += pts[j][2]
                cnt += 1
            else:
                break
        dur = pts[j][0] - pts[i][0]
        neto = _hav(pts[i][1], pts[i][2], pts[j][1], pts[j][2])
        # F5.24: además del neto, el RECORRIDO ACUMULADO del tramo debe ser
        # pequeño. Si recorriste cientos de metros dentro del radio (dando
        # vueltas, aparcando, un paseo de ida y vuelta), es movimiento real y
        # no se toca: era la causa de que desaparecieran giros.
        rec = 0.0
        for k in range(i + 1, j + 1):
            rec += _hav(pts[k - 1][1], pts[k - 1][2], pts[k][1], pts[k][2])
        # el tramo es una parada si su rumbo es incoherente (jitter) o se
        # movió muy despacio; si hay rumbo coherente es movimiento real
        rumb_tramo, vel_tramo, dmax_tramo = _coherencia(pts, j, k=min(400, max(1, j - i)))
        # parada = movimiento lento, o jitter (rumbo incoherente) SIN haberse
        # alejado: un vaivén tiene rumbo incoherente pero se aleja decenas de
        # metros y es movimiento real (no se toca)
        # Es parada si: (a) se mueve poquísimo, o (b) es jitter (rumbo
        # incoherente y sin alejarse), o (c) lleva MÁS DE 2 H sin salir del
        # radio: con jitter de 2 m por punto, "parado" y "andar despacio en un
        # espacio de 70 m" tienen firmas casi idénticas (medido: 60 y 92 m/min),
        # así que la duración es el criterio fiable para una estancia de verdad.
        if j > i and neto <= min_neto_m and (
                (dur >= min_dur_s
                 and (vel_tramo <= MIN_VEL_DER
                      or (rumb_tramo < min_rumb_der and dmax_tramo < 25.0)))
                or (dur >= min_dur_larga_s and neto <= min_neto_larga_m)):
            out.append(pts[i])          # parada → 1 punto
            i = j + 1
        elif j > i and dur >= min_dur_s:
            # hay movimiento real en la ventana: conservar todo el tramo
            out.extend(pts[i:j + 1])
            i = j + 1
        else:
            out.append(pts[i])
            i += 1
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
    limpios = autocompletar_huecos(limpios)
    # dos escalas: paradas cortas (>=4 min dentro de 80 m) y estancias largas
    limpios = unificar_estancias(limpios, radio_m=80.0, min_dur_s=240.0,
                                 min_neto_m=60.0, max_vel_m_min=15.0)
    limpios = unificar_estancias(limpios)
    # NOTA F5.24: una pasada extra con extensión encadenada (para colapsar
    # estancias de horas) se probó y se RETIRÓ: fusionaba el vaivén con la
    # estancia contigua y borraba tramos reales (medido: llegada y coche a 0 %).
    # La firma de "parado" (jitter 2 m por punto = 60 m/min) y la de "andar
    # despacio o dar vueltas" (83-92 m/min) son demasiado parecidas para
    # separarlas solo con coordenadas: hace falta la señal de actividad del
    # móvil (acelerómetro) o un modelo con modelo de error (Kalman/HMM).
    return unificar_estancias(limpios)
