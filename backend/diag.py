#!/usr/bin/env python3
"""F5.36 — DIAGNÓSTICO COMPLETO de TrackCam.

Motor de comprobación de TODO el sistema: servicios, red, túneles, móvil,
ingesta, motor de captura, datos y map-matching. Devuelve un JSON con semáforos
por bloque (ok/aviso/fallo), los valores medidos y una lista de PROBLEMAS con su
severidad y la ACCIÓN que los arregla.

Diseño:
  * Cada chequeo es una función que devuelve (estado, valor, detalle).
  * Los chequeos se ejecutan EN PARALELO (hilos) con timeouts cortos: el panel
    se refresca a menudo y no puede tardar.
  * `estado`: "ok" | "aviso" | "fallo" | "sin_datos".
  * Las acciones de reparación están en una LISTA BLANCA: nunca se ejecuta un
    comando que venga del cliente tal cual.

Se importa desde backend/app.py (endpoints /api/diag y /api/diag/accion).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # .../trackcam
DB = os.path.join(BASE, "data", "tracks.db")
AJUSTES = os.path.join(BASE, "data", "ajustes.json")
MUERTAS = os.path.join(BASE, "data", "muertas.json")
DIR_EVENTOS = os.path.join(BASE, "data", "eventos")
DIR_CACHE = os.path.join(BASE, "data", "cache")

# Servicios del sistema que vigila el panel (nombre systemd --user → etiqueta)
SERVICIOS = {
    "trackcam-api": "Backend de ingesta (:8099)",
    "trackcam-vision": "Backend de visión/panel (:8100)",
    "trackcam-mapmatching": "Ajuste a las calles, GraphHopper (:8098)",
    "cloudflared-trackcam": "Túnel trackcam.satetsunin.com",
    "cloudflared-webcast": "Túnel tv.satetsunin.com",
    "cloudflared-indice": "Túnel todo.satetsunin.com",
}

# Acciones permitidas (lista blanca). El cliente solo manda la CLAVE.
ACCIONES_PERMITIDAS = {
    "reiniciar_servicio": "Reinicia un servicio systemd de la lista vigilada",
    "reintentar_tuneles": "Reinicia los tres túneles de Cloudflare",
    "limpiar_duplicados": "Borra filas repetidas exactas de la tabla tracks",
    "comprobar_bd": "Pasa un PRAGMA quick_check y compacta si hace falta",
    "limpiar_cache": "Borra de la caché las cámaras caducadas (retención)",
}

TIMEOUT = 6.0            # tiempo máximo por comando externo
SEG_MOVIL_AVISO = 900    # 15 min sin recibir puntos = aviso
SEG_MOVIL_FALLO = 2700   # 45 min = fallo
INTERVALO_ESPERADO_MAX = 12.0   # s; por encima, la captura va lenta (bug v1.15)


def _run(cmd: list[str], timeout: float = TIMEOUT) -> tuple[int, str]:
    """Ejecuta un comando y devuelve (código, salida). Nunca lanza."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:                                  # noqa: BLE001
        return 1, f"{type(e).__name__}: {e}"


def _db(ro: bool = True) -> sqlite3.Connection:
    if ro:
        return sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
    return sqlite3.connect(DB, timeout=10)


def _fich(seg: float) -> str:
    """Duración legible."""
    seg = float(seg)
    if seg < 90:
        return f"{seg:.0f} s"
    if seg < 5400:
        return f"{seg/60:.0f} min"
    if seg < 172800:
        return f"{seg/3600:.1f} h"
    return f"{seg/86400:.1f} días"


def _peso(n: int) -> str:
    for u, d in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= d:
            return f"{n/d:.2f} {u}"
    return f"{n} B"


def _tam_dir(path: str) -> tuple[int, int]:
    """(bytes, nº de ficheros) de un directorio, sin seguir enlaces."""
    total = 0
    n = 0
    for raiz, _dirs, ficheros in os.walk(path, onerror=lambda e: None):
        for f in ficheros:
            try:
                total += os.path.getsize(os.path.join(raiz, f))
                n += 1
            except OSError:
                pass
    return total, n


# ─────────────────────────── CHEQUEOS ───────────────────────────

def chk_servicios() -> dict:
    """Estado de cada servicio vigilado."""
    items = []
    peor = "ok"
    for unidad, etiqueta in SERVICIOS.items():
        rc, out = _run([
            "systemctl", "--user", "show", unidad,
            "-p", "ActiveState", "-p", "SubState", "-p", "NRestarts",
            "-p", "ExecMainStartTimestamp",
        ])
        d = dict(
            linea.split("=", 1) for linea in out.strip().splitlines() if "=" in linea
        )
        activo = d.get("ActiveState", "?")
        sub = d.get("SubState", "?")
        reinicios = int(d.get("NRestarts") or 0)
        arranque = d.get("ExecMainStartTimestamp", "")
        m = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", arranque)
        desde = ""
        if m:
            try:
                t0 = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
                desde = f"desde hace {_fich(time.time()-t0)}"
            except Exception:                               # noqa: BLE001
                desde = ""
        if activo == "active" and sub == "running":
            est = "ok"
        elif activo == "activating":
            est = "aviso"          # típico de un restart loop
        else:
            est = "fallo"
        if est == "fallo":
            peor = "fallo"
        elif est == "aviso" and peor == "ok":
            peor = "aviso"
        valor = f"{activo}/{sub}"
        if desde:
            valor += f" · {desde}"
        if reinicios:
            valor += f" · {reinicios} reinicios"
        items.append({
            "nombre": etiqueta, "unidad": unidad, "estado": est, "valor": valor,
            "accion": None if est == "ok" else f"reiniciar_servicio:{unidad}",
        })
    return {"id": "servicios", "nombre": "Servicios del sistema",
            "estado": peor, "resumen": f"{sum(1 for i in items if i['estado']=='ok')}/{len(items)} activos",
            "items": items}


def chk_red() -> dict:
    """Ruta por defecto, DNS, WARP e IP pública."""
    items = []
    peor = "ok"

    rc, out = _run(["ip", "route", "get", "1.1.1.1"])
    por_warp = "CloudflareWARP" in out
    dev = ""
    m = re.search(r"dev (\S+)", out)
    if m:
        dev = m.group(1)
    items.append({
        "nombre": "Ruta por defecto", "estado": "aviso" if por_warp else "ok",
        "valor": dev or "sin ruta",
        "detalle": ("¡Todo el tráfico sale por Cloudflare WARP! Es lo que dejó sin "
                    "servicio a los túneles el 20-09; ya está desactivado.") if por_warp else "",
    })
    if por_warp:
        peor = "aviso"

    try:
        with open("/etc/resolv.conf") as f:
            conf = f.read()
        warp_dns = "cloudflare-warp" in conf.lower()
        ns = re.findall(r"^nameserver (\S+)", conf, re.M)
        items.append({
            "nombre": "DNS del sistema", "estado": "aviso" if warp_dns else "ok",
            "valor": ", ".join(ns[:3]) or "—",
            "detalle": "Lo estaba gestionando WARP (causa de cortes)" if warp_dns else "",
        })
        if warp_dns:
            peor = "aviso"
    except OSError as e:
        items.append({"nombre": "DNS del sistema", "estado": "sin_datos", "valor": str(e)})

    rc, out = _run(["warp-cli", "--accept-tos", "status"], timeout=4)
    conectado = "Connected" in out
    sin_demonio = "No such file or directory" in out or "daemon" in out and rc != 0
    if sin_demonio:
        valor = "desactivado (no hay demonio)"
    elif out.strip():
        valor = out.strip().splitlines()[0]
    else:
        valor = "no instalado"
    items.append({
        "nombre": "Cloudflare WARP", "estado": "aviso" if conectado else "ok",
        "valor": valor,
        "detalle": "Conectado: puede tumbar los túneles (ver historial)" if conectado else "desactivado, como debe estar",
    })
    if conectado:
        peor = "aviso"

    t0 = time.time()
    rc, out = _run(["getent", "hosts", "trackcam.satetsunin.com"], timeout=5)
    ms = (time.time() - t0) * 1000
    ok = rc == 0 and bool(out.strip())
    items.append({
        "nombre": "Resolución de trackcam.satetsunin.com", "estado": "ok" if ok else "fallo",
        "valor": f"{ms:.0f} ms" if ok else "no resuelve",
    })
    if not ok:
        peor = "fallo"
    return {"id": "red", "nombre": "Red del PC", "estado": peor,
            "resumen": "sin WARP" if not conectado and not por_warp else "revisar", "items": items}


def chk_tuneles() -> dict:
    """Acceso público real de cada dominio + errores recientes del túnel."""
    dominios = [
        ("trackcam.satetsunin.com", "cloudflared-trackcam", (200, 301, 302, 401, 403)),
        ("tv.satetsunin.com", "cloudflared-webcast", (200, 301, 302)),
        ("todo.satetsunin.com", "cloudflared-indice", (200, 301, 302)),
    ]
    items = []
    peor = "ok"

    def uno(dom, unidad, validos):
        rc, out = _run([
            "curl", "-s", "-o", "/dev/null", "-m", "10",
            "-w", "%{http_code} %{time_total}", f"https://{dom}",
        ], timeout=14)
        partes = out.strip().split()
        code = partes[0] if partes else "000"
        t = f"{float(partes[1]):.2f} s" if len(partes) > 1 else "—"
        try:
            es_ok = int(code) in validos
        except ValueError:
            es_ok = False
        rc2, err = _run([
            "journalctl", "--user", "-u", unidad, "--since", "24 hours ago",
            "--no-pager", "-p", "err",
        ], timeout=8)
        n_err = len([l for l in err.splitlines() if l.strip() and "cloudflared" in l])
        return {
            "nombre": dom, "estado": "ok" if es_ok else "fallo",
            "valor": f"HTTP {code} en {t}" + (f" · {n_err} errores en 24 h" if n_err else " · sin errores"),
            "detalle": "" if es_ok else "El dominio no responde con normalidad (¿530?)",
            "accion": None if es_ok else f"reiniciar_servicio:{unidad}",
        }

    with ThreadPoolExecutor(max_workers=3) as ex:
        futuros = [ex.submit(uno, *d) for d in dominios]
        items = [f.result() for f in futuros]
    if any(i["estado"] == "fallo" for i in items):
        peor = "fallo"
    return {"id": "tuneles", "nombre": "Túneles y acceso público", "estado": peor,
            "resumen": f"{sum(1 for i in items if i['estado']=='ok')}/{len(items)} responden", "items": items}


def chk_movil() -> dict:
    """Lo que se puede saber del móvil desde el servidor."""
    items = []
    peor = "ok"
    ahora = time.time()
    con = _db()
    try:
        ult = con.execute("SELECT MAX(ts) FROM tracks WHERE user_id=1").fetchone()[0]
        hace = ahora - ult if ult else None
        ts_min = con.execute(
            "SELECT COUNT(*) FROM tracks WHERE user_id=1 AND ts>?", (ahora-600,)).fetchone()[0]
        largos = con.execute(
            "SELECT COUNT(*) FROM tracks WHERE user_id=1 AND ts>? AND ts<?",
            (ahora-7200, ahora-600)).fetchone()[0]
        # mediana del intervalo de la última hora (proxy de la frecuencia real)
        ts = [r[0] for r in con.execute(
            "SELECT ts FROM tracks WHERE user_id=1 AND ts>? ORDER BY ts", (ahora-3600,))]
        med = None
        if len(ts) > 3:
            ds = sorted(b-a for a, b in zip(ts, ts[1:]))
            med = ds[len(ds)//2]
        acc = con.execute(
            "SELECT acc FROM tracks WHERE user_id=1 AND ts>? ORDER BY ts", (ahora-3600,)).fetchall()
        accs = sorted(a[0] for a in acc if a[0])
        acc_med = accs[len(accs)//2] if accs else None
        # actividad del móvil (permiso MIUI)
        act = con.execute(
            "SELECT COUNT(*) FROM tracks WHERE user_id=1 AND ts>? AND COALESCE(act,'')<>''",
            (ahora-86400,)).fetchone()[0]
        tot = con.execute(
            "SELECT COUNT(*) FROM tracks WHERE user_id=1 AND ts>?", (ahora-86400,)).fetchone()[0]
        wifi = con.execute(
            "SELECT COUNT(*) FROM tracks WHERE user_id=1 AND ts>? AND COALESCE(wifi_hue,'')<>''",
            (ahora-86400,)).fetchone()[0]
        # cola atrasada: puntos que llegan con más de 10 min de retraso
        late = con.execute(
            "SELECT COUNT(*) FROM tracks WHERE user_id=1 AND ts>? AND ts>? AND ts<?",
            (ahora-3600, ahora-604800, ahora-600)).fetchone()[0]
        dev = con.execute(
            "SELECT dev, dev_id FROM tracks WHERE user_id=1 ORDER BY ts DESC LIMIT 1").fetchone()
    except Exception as e:                                  # noqa: BLE001
        con.close()
        return {"id": "movil", "nombre": "Tu móvil", "estado": "sin_datos",
                "resumen": f"error: {e}", "items": []}
    con.close()

    if hace is None:
        items.append({"nombre": "Último punto", "estado": "sin_datos", "valor": "nunca"})
        peor = "sin_datos"
    else:
        est = "ok" if hace < SEG_MOVIL_AVISO else ("aviso" if hace < SEG_MOVIL_FALLO else "fallo")
        items.append({
            "nombre": "Último punto recibido", "estado": est, "valor": f"hace {_fich(hace)}",
            "detalle": "" if est == "ok" else "El móvil lleva demasiado tiempo sin entregar: "
                       "mira si tiene cobertura o si el sistema le ha cerrado la app",
        })
        if est != "ok":
            peor = est

    if med is not None:
        est = "ok" if med <= INTERVALO_ESPERADO_MAX else "aviso"
        items.append({
            "nombre": "Frecuencia de captura (última hora)", "estado": est,
            "valor": f"un punto cada {med:.1f} s",
            "detalle": "" if est == "ok" else
            "Va lenta: en un corte de red la app antigua perdía 16 de cada 17 puntos "
            "(arreglado en la v1.15: comprueba que la tienes instalada)",
        })
        if est != "ok" and peor == "ok":
            peor = "aviso"

    items.append({
        "nombre": "Puntos recibidos (10 min)", "estado": "ok" if ts_min > 0 else "aviso",
        "valor": f"{ts_min} puntos",
    })
    if acc_med:
        est = "ok" if acc_med <= 30 else "aviso"
        items.append({
            "nombre": "Precisión GPS (mediana, 1 h)", "estado": est,
            "valor": f"{acc_med:.0f} m",
            "detalle": "" if est == "ok" else "Señal floja: en interior o con el móvil tapado",
        })
        if est != "ok" and peor == "ok":
            peor = "aviso"

    pct_act = (100.0*act/tot) if tot else 0.0
    items.append({
        "nombre": "Actividad del móvil (permiso MIUI)", "estado": "ok" if pct_act > 50 else "aviso",
        "valor": f"{pct_act:.0f} % de los puntos",
        "detalle": "" if pct_act > 50 else "Permiso de \"actividad física\" denegado: "
                   "concederlo en Ajustes → Aplicaciones → TrackCam → Permisos",
    })
    if pct_act <= 50 and peor == "ok":
        peor = "aviso"

    items.append({
        "nombre": "Contexto wifi", "estado": "ok" if wifi and wifi > 0 else "sin_datos",
        "valor": f"{wifi} puntos con huella",
    })
    if late:
        items.append({
            "nombre": "Puntos entregados con retraso (1 h)", "estado": "ok", "valor": f"{late}",
            "detalle": "Redis de un corte previo: la cola offline los conservó",
        })
    if dev:
        items.append({"nombre": "Dispositivo", "estado": "ok",
                      "valor": f"{dev[0]} · {dev[1] or 'sin id'}"})
    return {"id": "movil", "nombre": "Tu móvil", "estado": peor,
            "resumen": f"último punto hace {_fich(hace)}" if hace else "sin datos", "items": items}


def chk_ingesta() -> dict:
    """Cómo está recibiendo el servidor: ritmo, duplicados y rechazos."""
    items = []
    peor = "ok"
    ahora = time.time()
    con = _db()
    try:
        n1 = con.execute("SELECT COUNT(*) FROM tracks WHERE ts>?", (ahora-60,)).fetchone()[0]
        n5 = con.execute("SELECT COUNT(*) FROM tracks WHERE ts>?", (ahora-300,)).fetchone()[0]
        dup = con.execute(
            "SELECT COALESCE(SUM(c-1),0) FROM (SELECT COUNT(*) c FROM tracks "
            "GROUP BY user_id,ts,lat,lon HAVING c>1)").fetchone()[0]
        filas = con.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    except Exception as e:                                  # noqa: BLE001
        con.close()
        return {"id": "ingesta", "nombre": "Ingesta", "estado": "fallo",
                "resumen": f"error: {e}", "items": []}
    con.close()

    items.append({"nombre": "Puntos en el último minuto", "estado": "ok", "valor": f"{n1}",
                  "detalle": f"{n5/5:.1f}/min de media en 5 min"})

    rc, err = _run(["journalctl", "--user", "-u", "trackcam-api", "--since", "1 hour ago",
                    "--no-pager"], timeout=8)
    n_err = len([l for l in err.splitlines() if " 500 " in l or "Internal Server Error" in l])
    n_429 = len([l for l in err.splitlines() if " 429 " in l])
    n_200 = len([l for l in err.splitlines() if "POST /track" in l and " 200 " in l])
    items.append({
        "nombre": "Errores 500 (1 h)", "estado": "ok" if n_err == 0 else "fallo",
        "valor": str(n_err), "detalle": "" if n_err == 0 else "La ingesta está fallando: mira el log",
    })
    if n_err:
        peor = "fallo"
    items.append({
        "nombre": "Rechazos por ritmo (1 h)", "estado": "ok" if n_429 == 0 else "aviso",
        "valor": str(n_429),
        "detalle": "" if n_429 == 0 else "El guardarraíl anti-bucle ha cortado envíos",
    })
    items.append({"nombre": "Envíos aceptados (1 h)", "estado": "ok", "valor": str(n_200)})
    items.append({
        "nombre": "Duplicados exactos en la BD", "estado": "ok" if dup == 0 else "aviso",
        "valor": str(dup),
        "detalle": "" if dup == 0 else "Se limpian con la acción de reparación",
        "accion": None if dup == 0 else "limpiar_duplicados",
    })
    if dup:
        peor = "aviso" if peor == "ok" else peor
    items.append({"nombre": "Filas en la tabla de puntos", "estado": "ok", "valor": f"{filas:,}"})
    return {"id": "ingesta", "nombre": "Ingesta de datos", "estado": peor,
            "resumen": f"{n1}/min · {n_200} aceptados · {n_err} errores", "items": items}


def chk_motor() -> dict:
    """Motor de captura: eventos, cámaras muertas y caché."""
    items = []
    peor = "ok"
    ahora = time.time()
    hoy = time.strftime("%Y-%m-%d")
    con = _db()
    try:
        ev_hoy = con.execute(
            "SELECT COUNT(*) FROM eventos WHERE date(ts_inicio,'unixepoch','localtime')=?",
            (hoy,)).fetchone()[0]
        ev_total = con.execute("SELECT COUNT(*) FROM eventos").fetchone()[0]
        con_cam = con.execute(
            "SELECT COUNT(DISTINCT cam_id) FROM eventos WHERE date(ts_inicio,'unixepoch','localtime')=?",
            (hoy,)).fetchone()[0]
        ult_ev = con.execute("SELECT MAX(ts_inicio) FROM eventos").fetchone()[0]
    except Exception as e:                                  # noqa: BLE001
        con.close()
        return {"id": "motor", "nombre": "Motor de captura", "estado": "sin_datos",
                "resumen": f"error: {e}", "items": []}
    con.close()

    items.append({"nombre": "Eventos hoy", "estado": "ok", "valor": f"{ev_hoy}",
                  "detalle": f"{con_cam} cámaras distintas"})
    items.append({"nombre": "Eventos totales", "estado": "ok", "valor": f"{ev_total:,}"})
    if ult_ev:
        hace = ahora - ult_ev
        est = "ok" if hace < 6*3600 else "aviso"
        items.append({"nombre": "Última captura", "estado": est, "valor": f"hace {_fich(hace)}",
                      "detalle": "" if est == "ok" else "Ninguna cámara capturada en horas (¿no te has movido?)"})

    try:
        with open(MUERTAS) as f:
            muertas = json.load(f)
        lista = muertas if isinstance(muertas, list) else muertas.get("camaras", [])
        items.append({"nombre": "Cámaras marcadas como caídas", "estado": "ok", "valor": str(len(lista))})
    except Exception:                                       # noqa: BLE001
        items.append({"nombre": "Cámaras marcadas como caídas", "estado": "sin_datos", "valor": "—"})

    try:
        with open(AJUSTES) as f:
            aj = json.load(f)
        cuota_ev = float(aj.get("cuota_eventos_gb", 15)) * (1 << 30)
        cuota_ca = float(aj.get("cuota_cache_gb", 30)) * (1 << 30)
        b_ev, n_ev = _tam_dir(DIR_EVENTOS)
        b_ca, n_ca = _tam_dir(DIR_CACHE)
        pct_ev = 100.0*b_ev/cuota_ev
        est = "ok" if pct_ev < 80 else "aviso"
        items.append({
            "nombre": "Almacén de eventos", "estado": est,
            "valor": f"{_peso(b_ev)} de {_peso(int(cuota_ev))} ({pct_ev:.0f} %)",
            "detalle": f"{n_ev:,} ficheros · " + ("" if est == "ok" else "cerca del límite"),
        })
        if est != "ok":
            peor = "aviso"
        items.append({
            "nombre": "Caché de cámaras", "estado": "ok",
            "valor": f"{_peso(b_ca)} de {_peso(int(cuota_ca))} ({100.0*b_ca/cuota_ca:.0f} %)",
            "detalle": f"{n_ca:,} ficheros · retención {aj.get('retencion_cache_dias','?')} días",
        })
    except Exception as e:                                  # noqa: BLE001
        items.append({"nombre": "Almacenamiento", "estado": "sin_datos", "valor": f"error: {e}"})
    return {"id": "motor", "nombre": "Motor de captura", "estado": peor,
            "resumen": f"{ev_hoy} eventos hoy", "items": items}


def chk_datos() -> dict:
    """Salud de la base de datos y del disco."""
    items = []
    peor = "ok"
    try:
        b = os.path.getsize(DB)
    except OSError:
        b = 0
    items.append({"nombre": "Tamaño de la base", "estado": "ok", "valor": _peso(b)})

    con = _db()
    t0 = time.time()
    try:
        integ = con.execute("PRAGMA quick_check").fetchone()[0]
    except Exception as e:                                  # noqa: BLE001
        integ = f"error: {e}"
    ms = (time.time()-t0) * 1000
    ok = integ == "ok"
    items.append({
        "nombre": "Integridad (quick_check)", "estado": "ok" if ok else "fallo",
        "valor": f"{integ} en {ms:.0f} ms",
    })
    if not ok:
        peor = "fallo"
    try:
        por_usuario = con.execute(
            "SELECT u.username, COUNT(t.rowid) FROM usuarios u "
            "LEFT JOIN tracks t ON t.user_id=u.id GROUP BY u.id"
        ).fetchall()
        items.append({"nombre": "Puntos por usuario", "estado": "ok",
                      "valor": " · ".join(f"{n}: {c:,}" for n, c in por_usuario)})
    except Exception:                                       # noqa: BLE001
        pass
    try:
        n_ses = con.execute("SELECT COUNT(*) FROM sesiones").fetchone()[0]
        items.append({"nombre": "Sesiones activas", "estado": "ok", "valor": str(n_ses)})
    except Exception:                                       # noqa: BLE001
        pass
    con.close()

    try:
        du = shutil.disk_usage(BASE)
        pct = 100.0*du.used/du.total
        est = "ok" if pct < 90 else "aviso"
        items.append({"nombre": "Disco (/)", "estado": est,
                      "valor": f"{_peso(du.free)} libres de {_peso(du.total)} ({pct:.0f} % usado)"})
        if est != "ok" and peor == "ok":
            peor = "aviso"
    except OSError:
        pass
    try:
        with open("/proc/uptime") as f:
            up = float(f.read().split()[0])
        items.append({"nombre": "El PC lleva encendido", "estado": "ok", "valor": _fich(up)})
    except Exception:                                       # noqa: BLE001
        pass
    return {"id": "datos", "nombre": "Datos y sistema", "estado": peor,
            "resumen": _peso(b), "items": items}


def chk_mapmatching() -> dict:
    """El motor de ajuste a las calles (GraphHopper :8098)."""
    items = []
    t0 = time.time()
    rc, out = _run(["curl", "-s", "-o", "/dev/null", "-m", "6",
                    "-w", "%{http_code} %{time_total}", "http://127.0.0.1:8098/match"], timeout=10)
    partes = out.strip().split()
    code = partes[0] if partes else "000"
    t = f"{float(partes[1]):.2f} s" if len(partes) > 1 else "—"
    vivo = code in ("405", "400", "200")
    items.append({
        "nombre": "Motor GraphHopper (:8098)", "estado": "ok" if vivo else "fallo",
        "valor": f"HTTP {code} en {t}" if vivo else f"sin respuesta ({code})",
        "detalle": "405 = vivo y esperando traza GPX" if code == "405" else ("reiniciado" if vivo else "caído"),
        "accion": None if vivo else "reiniciar_servicio:trackcam-mapmatching",
    })
    return {"id": "mapmatching", "nombre": "Ajuste a las calles", "estado": "ok" if vivo else "fallo",
            "resumen": "vivo" if vivo else "caído", "items": items}


# ─────────────────────── PROBLEMAS Y ACCIONES ───────────────────────

def problemas(bloques: list[dict]) -> list[dict]:
    """Traduce los semáforos en una lista de problemas accionables."""
    out = []
    for b in bloques:
        for it in b.get("items", []):
            est = it.get("estado")
            if est in ("ok", "sin_datos"):
                continue
            sev = "alta" if est == "fallo" else "media"
            out.append({
                "severidad": sev,
                "bloque": b["nombre"],
                "titulo": it["nombre"],
                "detalle": it.get("detalle") or it.get("valor", ""),
                "accion": it.get("accion"),
            })
    orden = {"alta": 0, "media": 1, "baja": 2}
    out.sort(key=lambda p: orden.get(p["severidad"], 3))
    return out


def diagnosticar() -> dict:
    """Ejecuta TODOS los chequeos en paralelo y devuelve el informe completo."""
    chequeos = [chk_servicios, chk_red, chk_tuneles, chk_movil,
                chk_ingesta, chk_motor, chk_datos, chk_mapmatching]
    bloques = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for f in [ex.submit(c) for c in chequeos]:
            try:
                bloques.append(f.result())
            except Exception as e:                          # noqa: BLE001
                bloques.append({"id": "error", "nombre": "Chequeo con error",
                                "estado": "fallo", "resumen": f"{type(e).__name__}: {e}",
                                "items": []})
    problemas_ = problemas(bloques)
    if any(p["severidad"] == "alta" for p in problemas_):
        glob = "fallo"
    elif problemas_:
        glob = "aviso"
    else:
        glob = "ok"
    return {
        "cuando": time.time(),
        "hora": time.strftime("%H:%M:%S"),
        "estado_global": glob,
        "bloques": bloques,
        "problemas": problemas_,
        "acciones_disponibles": ACCIONES_PERMITIDAS,
    }


# ─────────────────────────── REPARACIÓN ───────────────────────────

def ejecutar_accion(accion: str, parametro: str = "") -> dict:
    """Ejecuta una acción de la lista blanca. Devuelve {ok, salida}."""
    if accion == "reiniciar_servicio":
        if parametro not in SERVICIOS:
            return {"ok": False, "salida": f"servicio no vigilado: {parametro}"}
        rc, out = _run(["systemctl", "--user", "restart", parametro], timeout=30)
        time.sleep(1.5)
        rc2, est = _run(["systemctl", "--user", "is-active", parametro])
        return {"ok": rc2 == 0, "salida": f"{parametro} → {est.strip() or 'desconocido'}",
                "detalle": out.strip()[:300]}

    if accion == "reintentar_tuneles":
        salidas = []
        for u in ("cloudflared-trackcam", "cloudflared-webcast", "cloudflared-indice"):
            _run(["systemctl", "--user", "restart", u], timeout=30)
            salidas.append(u)
        time.sleep(6)
        estados = {u: _run(["systemctl", "--user", "is-active", u])[1].strip() for u in salidas}
        return {"ok": all(v == "active" for v in estados.values()),
                "salida": " · ".join(f"{k.split('-')[-1]}: {v}" for k, v in estados.items())}

    if accion == "limpiar_duplicados":
        con = _db(ro=False)
        try:
            n = con.execute(
                "DELETE FROM tracks WHERE rowid NOT IN "
                "(SELECT MIN(rowid) FROM tracks GROUP BY user_id,ts,lat,lon)"
            ).rowcount
            con.commit()
        finally:
            con.close()
        return {"ok": True, "salida": f"{n} filas duplicadas borradas"}

    if accion == "comprobar_bd":
        con = _db(ro=False)
        try:
            t0 = time.time()
            r = con.execute("PRAGMA quick_check").fetchone()[0]
            con.execute("PRAGMA optimize")
            con.commit()
        finally:
            con.close()
        return {"ok": r == "ok", "salida": f"quick_check: {r} en {time.time()-t0:.1f} s"}

    if accion == "limpiar_cache":
        rc, out = _run(["systemctl", "--user", "restart", "trackcam-api"], timeout=30)
        return {"ok": True, "salida": "reiniciado el motor (rehace la caché según retención)"}

    return {"ok": False, "salida": f"acción desconocida: {accion}"}
