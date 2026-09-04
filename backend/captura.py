#!/usr/bin/env python3
"""TrackCam — Motor de captura MULTI-USUARIO (Fase 5).

Hilo daemon que cada ~2 s lee las posiciones nuevas del track (SQLite) y
captura fotogramas de las cámaras de EuroCams cercanas a CADA usuario:

  · ≤1500 m → cámara "activa": se marca y se toma 1 snapshot de contexto.
  · ≤500  m → captura continua cada 2 s a un ring buffer en
              data/temps/<USER>/CAM_ID/ (poda automática).
  · ≤100  m → EVENTO: ventana [entrada-60 s, salida+60 s] (configurable),
              cubriendo la estancia completa del usuario dentro del radio.
              Se monta un vídeo MP4 H.264 (2 fps) con las fotos de la ventana
              y se guardan también las imágenes originales en
              data/eventos/<USER>/EVENTO_ID/.

Cada usuario tiene estado, buffers y eventos separados: nadie ve lo de otro.

La descarga de imágenes intenta primero la URL directa (con Referer) y si
falla (403/404/error) usa el proxy de EuroCams
http://127.0.0.1:8000/api/img?u=... con reintento.

NO importa app.py (evita import circular): recibe por constructor las
funciones de app.py que necesita (get_db, cams_cerca).
"""

import os
import re
import json
import time
import uuid
import shutil
import sqlite3
import logging
import subprocess
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("trackcam.captura")

# Especificación de Alvaro (F5): 60 s antes + estancia + 60 s después
INTERVALO_S = 2.0           # ciclo del motor
RADIO_ACTIVA = 1500.0       # m → cámara activa (snapshot de contexto)
RADIO_CAPTURA = 500.0       # m → captura continua (ring buffer)
RADIO_EVENTO = 100.0        # m → evento
RADIO_PASADA = 60.0         # F5.9: dist. máx. (m) para que un evento cuente
                            # como "pasada real" (verde en mapa / cronología).
                            # Configurable en ajustes (radio_pasada_m).
BUFFER_S = 1800.0           # profundidad ring buffer (s) = 30 min — con el
                            # caché de 30 días como respaldo, el buffer amplio
                            # garantiza cinta completa incluso en pasadas lentas
VENTANA_ANTES = 60.0        # s antes de ENTRAR en los 100 m que se conservan
VENTANA_DESPUES = 60.0      # s después de SALIR de los 100 m que se conservan
FPS_VIDEO = 2
CUOTA_EVENTOS_GB = 15.0     # default F5
CUOTA_EVENTOS_GB_MAX = 50.0 # tope duro F5
CUOTA_CACHE_GB = 30.0       # caché persistente (dedup) — F5.3
CUOTA_CACHE_GB_MAX = 50.0
RETENCION_CACHE_DIAS = 30.0 # días que permanecen las imágenes en caché
UMBRAL_DEDUP = 4            # bits de diferencia dHash → misma imagen
CUOTA_TEMPS_MB = 500.0
POOL_HILOS = 12
TIMEOUT_DESCARGA = 5
PROXY_IMAGEN = "http://127.0.0.1:8000/api/img?u="
FFMPEG = "/usr/bin/ffmpeg"

# Tiempo real (s) sin puntos de un usuario tras el que se finaliza su estado
INACTIVIDAD_S = 20.0

EST_INACTIVA = "inactiva"
EST_ACTIVA = "activa"
EST_CAPTURANDO = "capturando"
EST_EVENTO = "evento"     # dentro de 100 m o esperando ventana post-salida


def sanitizar_id(cid: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_-]", "_", str(cid))
    return s[:80] or "cam"


def es_imagen(datos: bytes) -> bool:
    if not datos:
        return False
    if datos[:3] == b"\xff\xd8\xff":      # JPEG
        return True
    if datos[:4] == b"\x89PNG":
        return True
    if datos[:3] == b"GIF":
        return True
    if datos[:4] == b"RIFF" and datos[8:12] == b"WEBP":
        return True
    if datos[:2] == b"BM":
        return True
    return False


def dhash_bytes(datos: bytes) -> int:
    """Hash perceptual (dHash 9x8) de una imagen → entero de 64 bits.

    Reduce la imagen a 9x8 píxeles en gris y compara cada píxel con su
    vecino derecho: 1 si es más claro, 0 si más oscuro. Dos fotos de la
    misma escena dan hashes casi idénticos aunque la compresión difiera.
    Devuelve -1 si no se puede procesar.
    """
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(datos)).convert("L").resize((9, 8),
                                                                Image.LANCZOS)
        px = list(img.getdata())
        h = 0
        for y in range(8):
            for x in range(8):
                h = (h << 1) | (1 if px[y * 9 + x] > px[y * 9 + x + 1] else 0)
        return h
    except Exception:
        return -1


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def haversine(lat1, lon1, lat2, lon2):
    import math as _m
    R = 6371000.0
    p1, p2 = _m.radians(lat1), _m.radians(lat2)
    dp = _m.radians(lat2 - lat1); dl = _m.radians(lon2 - lon1)
    a = _m.sin(dp/2)**2 + _m.cos(p1)*_m.cos(p2)*_m.sin(dl/2)**2
    return 2*R*_m.asin(_m.sqrt(a))


def en_zona(zonas, lat, lon, margen=2.0) -> bool:
    """True si (lat,lon) cae dentro de alguna zona (radio + margen)."""
    for z in zonas or []:
        try:
            if haversine(lat, lon, float(z["lat"]), float(z["lon"])) \
                    <= float(z["radio_m"]) + margen:
                return True
        except (KeyError, TypeError, ValueError):
            continue
    return False


class MotorCaptura:
    def __init__(self, db_path, data_dir, get_db_fn, cams_cerca_fn,
                 intervalo=INTERVALO_S):
        self.db_path = db_path
        self.data_dir = data_dir
        self.temps_dir = os.path.join(data_dir, "temps")
        self.eventos_dir = os.path.join(data_dir, "eventos")
        self.cache_dir = os.path.join(data_dir, "cache")
        self.get_db = get_db_fn
        self.cams_cerca = cams_cerca_fn
        self.intervalo = intervalo
        os.makedirs(self.temps_dir, exist_ok=True)
        os.makedirs(self.eventos_dir, exist_ok=True)
        os.makedirs(self.cache_dir, exist_ok=True)

        self.cfg_file = os.path.join(data_dir, "ajustes.json")
        self.cfg = {
            "radio_activa_m": RADIO_ACTIVA,
            "radio_captura_m": RADIO_CAPTURA,
            "radio_evento_m": RADIO_EVENTO,
            "intervalo_captura_s": INTERVALO_S,
            "ventana_antes_s": VENTANA_ANTES,
            "ventana_despues_s": VENTANA_DESPUES,
            "buffer_s": BUFFER_S,
            "fps_video": FPS_VIDEO,
            "cuota_eventos_gb": CUOTA_EVENTOS_GB,       # default 15
            "cuota_eventos_gb_max": CUOTA_EVENTOS_GB_MAX,  # tope duro 50
            "cuota_cache_gb": CUOTA_CACHE_GB,           # default 30 (F5.3)
            "cuota_cache_gb_max": CUOTA_CACHE_GB_MAX,
            "retencion_cache_dias": RETENCION_CACHE_DIAS,  # 30 días
            "umbral_dedup": UMBRAL_DEDUP,               # bits dHash
            "cuota_temps_mb": CUOTA_TEMPS_MB,
            "radio_pasada_m": RADIO_PASADA,  # F5.9: dist. máx. para contar "pasada real"
        }
        self._cargar_cfg()

        self.lock = __import__("threading").Lock()
        self.cams = {}          # user_id -> {cid: estado}
        self.ultimo_ts = {}     # user_id -> último ts procesado
        self.ultima_llegada = {}  # user_id -> time.time() del último lote suyo
        self.descargas_ok = 0
        self.descargas_fallo = 0
        self.eventos_creados = 0
        self._fallos_cam = {}   # cid -> fallos de descarga seguidos

        # Zonas de no-monitorización por usuario (F5.8): caché {uid: {ts, zonas}}
        # refrescada cada ~10 s desde la BD. El motor NO procesa puntos que
        # caen dentro de una zona (casa/bar): sin activación, captura ni evento.
        self._zonas_cache = {}      # str(uid) -> {ts: epoch, zonas: [...]}
        self._zonas_ttl = 10.0
        self._en_zona_user = {}     # str(uid) -> True (transición zona)

        # Cámaras detectadas como MUERTAS al intentar capturar (placeholder
        # real o descarga fallida repetida). Persistente: el mapa las pinta
        # ⚪ gris. El id es el ORIGINAL (con puntos, mismo que eventos/BD).
        self.muertas_file = os.path.join(data_dir, "muertas.json")
        self.muertas = {}       # cid -> {ts, motivo}
        self._cargar_muertas()

        self.pool = ThreadPoolExecutor(max_workers=POOL_HILOS)
        self.pendientes = {}    # (user_id, cid) -> lista de futures

        self._hilo = None
        self._stop = __import__("threading").Event()
        self._ciclos_cuota = 0

    # ── configuración (F2/F5) ──────────────────────────────────────────
    def _cargar_cfg(self):
        try:
            with open(self.cfg_file, encoding="utf-8") as f:
                datos = json.load(f)
            self.cfg.update({k: v for k, v in datos.items()
                             if k in self.cfg})
        except Exception:
            pass

    def _guardar_cfg(self):
        try:
            with open(self.cfg_file, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    # ── registro de cámaras MUERTAS (placeholder / fallo al capturar) ──
    def _cargar_muertas(self):
        try:
            with open(self.muertas_file, encoding="utf-8") as f:
                datos = json.load(f)
            if isinstance(datos, dict):
                self.muertas = datos
        except Exception:
            self.muertas = {}

    def _guardar_muertas(self):
        try:
            with open(self.muertas_file, "w", encoding="utf-8") as f:
                json.dump(self.muertas, f, ensure_ascii=False, indent=1)
        except OSError:
            pass

    def _marcar_muerta(self, cid, motivo):
        """Registra una cámara como muerta (placeholder real o fallo)."""
        with self.lock:
            self.muertas[str(cid)] = {"ts": time.time(), "motivo": motivo}
            self._guardar_muertas()

    def _marcar_viva(self, cid):
        """Si la cámara vuelve a servir imagen real, sale de la lista."""
        with self.lock:
            if str(cid) in self.muertas:
                del self.muertas[str(cid)]
                self._guardar_muertas()

    def _es_placeholder(self, datos: bytes) -> bool:
        """True si los bytes son el JPEG de error CONOCIDO de una fuente.

        Solo tamaños verificados (geobilbao 11015/3915/0 B). Un tamaño grande
        aunque sea una imagen fija NO es placeholder (refresco lento ≠ muerta).
        """
        return len(datos) in (11015, 3915, 0)

    def actualizar_cfg(self, cambios: dict) -> dict:
        validos = {k: v for k, v in cambios.items() if k in self.cfg}
        # La cuota de eventos no puede superar el tope duro
        if "cuota_eventos_gb" in validos:
            tope = float(self.cfg.get("cuota_eventos_gb_max", 50.0))
            validos["cuota_eventos_gb"] = min(float(validos["cuota_eventos_gb"]), tope)
        if "cuota_cache_gb" in validos:
            tope = float(self.cfg.get("cuota_cache_gb_max", 50.0))
            validos["cuota_cache_gb"] = min(float(validos["cuota_cache_gb"]), tope)
        for k, v in validos.items():
            if k == "fps_video":
                self.cfg[k] = max(1, int(v))
            else:
                self.cfg[k] = float(v)
        self._guardar_cfg()
        return dict(self.cfg)

    # ── arranque / parada ──────────────────────────────────────────────
    def start(self):
        if self._hilo and self._hilo.is_alive():
            return
        # No reprocesar puntos anteriores al arranque
        try:
            con = self.get_db()
            filas = con.execute(
                "SELECT user_id, MAX(ts) FROM tracks GROUP BY user_id").fetchall()
            con.close()
            self.ultimo_ts = {u: float(t or 0.0) for u, t in filas}
        except Exception:
            self.ultimo_ts = {}
        self._hilo = __import__("threading").Thread(
            target=self._loop, daemon=True, name="motor-captura")
        self._hilo.start()
        print(f"[captura] motor multi-usuario arrancado "
              f"({len(self.ultimo_ts)} usuarios previos)")

    def stop(self):
        self._stop.set()

    # ── bucle principal ────────────────────────────────────────────────
    def _loop(self):
        while not self._stop.is_set():
            try:
                self._ciclo()
            except Exception as e:
                print(f"[captura] error en ciclo: {e}")
            self._stop.wait(self.intervalo)

    # ── zonas de no-monitorización (F5.8) ─────────────────────────────
    def _zonas_de(self, user_id):
        """Zonas del usuario con caché corta (la tabla cambia poco)."""
        uid = str(user_id)
        c = self._zonas_cache.get(uid)
        if c and time.time() - c["ts"] < self._zonas_ttl:
            return c["zonas"]
        try:
            con = self.get_db()
            filas = con.execute(
                "SELECT id, nombre, lat, lon, radio_m FROM zonas "
                "WHERE user_id=? ORDER BY id", (int(user_id),)).fetchall()
            con.close()
            zonas = [{"id": r[0], "nombre": r[1], "lat": r[2], "lon": r[3],
                      "radio_m": r[4]} for r in filas]
        except Exception:
            zonas = []
        self._zonas_cache[uid] = {"ts": time.time(), "zonas": zonas}
        return zonas

    def _salir_de_zonas(self, user_id):
        """El usuario está DENTRO de una zona: si venía de fuera (evento en
        curso), finaliza y limpia. Solo actúa en la transición fuera→dentro
        (flag _en_zona_user) para no repetir trabajo en cada punto."""
        uid = str(user_id)
        if self._en_zona_user.get(uid):
            return  # ya estábamos en zona: nada que hacer
        self._en_zona_user[uid] = True
        print(f"[captura] user={uid} dentro de zona de no-monitorización — "
              f"sin activación ni captura")
        with self.lock:
            cams_u = self.cams.get(uid, {})
            for cid, est in list(cams_u.items()):
                if est["estado"] != EST_INACTIVA:
                    if est["entrada_ts"] is not None:
                        self._finalizar_evento(uid, cid, est)
                    self._cortar_buffer(uid, cid)
                    self._reset_estado(est)

    def _ciclo(self):
        con = self.get_db()
        filas = con.execute(
            "SELECT user_id, ts, lat, lon FROM tracks "
            "WHERE user_id IS NOT NULL ORDER BY ts ASC").fetchall()
        con.close()

        # Procesar solo lo nuevo por usuario
        nuevos = [f for f in filas if f[1] > self.ultimo_ts.get(f[0], 0.0)]
        by_user = {}
        for user_id, ts, lat, lon in nuevos:
            by_user.setdefault(user_id, []).append((ts, lat, lon))
            self.ultimo_ts[user_id] = max(self.ultimo_ts.get(user_id, 0.0), ts)

        for user_id, pts in by_user.items():
            pts.sort(key=lambda x: x[0])
            zonas = self._zonas_de(user_id)
            for ts, lat, lon in pts:
                try:
                    if zonas and en_zona(zonas, lat, lon):
                        # Dentro de una zona de no-monitorización (casa/bar):
                        # el usuario está parado y su GPS deriva — NO se activan
                        # cámaras ni se captura. Si venía de un evento en curso
                        # (cruzó el borde de la zona), se finaliza y se limpia.
                        self._salir_de_zonas(user_id)
                        continue
                    # Punto FUERA de zona → el usuario está monitorizable
                    self._en_zona_user[str(user_id)] = False
                    self._procesar_punto(user_id, ts, lat, lon)
                except Exception as e:
                    print(f"[captura] error punto user={user_id} ts={ts}: {e}")
            self.ultima_llegada[user_id] = time.time()
            self._poda_global(user_id)

        # Inactividad por usuario (no recibió puntos en INACTIVIDAD_S reales)
        ahora = time.time()
        with self.lock:
            activos = [u for u, cams_u in self.cams.items()
                       if any(e["estado"] != EST_INACTIVA for e in cams_u.values())]
        for u in activos:
            if ahora - self.ultima_llegada.get(u, 0) > INACTIVIDAD_S:
                self._cortar_por_inactividad(u)

        # Poda por cuotas (cada ~15 ciclos)
        self._ciclos_cuota += 1
        if self._ciclos_cuota >= 15:
            self._ciclos_cuota = 0
            self._poda_cuotas()

    # ── poda por cuotas (F2/F5) ────────────────────────────────────────
    def _tam_dir(self, p):
        t = 0
        for root, _, files in os.walk(p):
            for f in files:
                try:
                    t += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        return t

    def _poda_cuotas(self):
        # Temporales
        try:
            cuota_t = self.cfg["cuota_temps_mb"] * 1e6
            tam_t = self._tam_dir(self.temps_dir)
            if tam_t > cuota_t and os.path.isdir(self.temps_dir):
                dirs = sorted(
                    (os.path.join(self.temps_dir, d) for d in
                     os.listdir(self.temps_dir)
                     if os.path.isdir(os.path.join(self.temps_dir, d))),
                    key=lambda p: os.path.getmtime(p))
                for d in dirs:
                    if tam_t <= cuota_t:
                        break
                    for f in os.listdir(d):
                        try:
                            os.remove(os.path.join(d, f))
                            tam_t -= os.path.getsize(os.path.join(d, f))
                        except OSError:
                            pass
                    try:
                        os.rmdir(d)
                    except OSError:
                        pass
        except Exception as e:
            print(f"[captura] poda temps: {e}")

        # Eventos: si supera la cuota configurada (≤50 GB tope) borra antiguos
        try:
            cuota_e = min(self.cfg["cuota_eventos_gb"],
                          self.cfg["cuota_eventos_gb_max"]) * 1e9
            tam_e = self._tam_dir(self.eventos_dir)
            if tam_e > cuota_e:
                con = self.get_db()
                filas = con.execute(
                    "SELECT id, user_id FROM eventos ORDER BY ts_inicio ASC").fetchall()
                con.close()
                for eid, user_id in filas:
                    if tam_e <= cuota_e:
                        break
                    carpeta = os.path.join(self.eventos_dir,
                                           str(user_id), eid)
                    tam_e -= self._tam_dir(carpeta) if os.path.isdir(carpeta) else 0
                    shutil.rmtree(carpeta, ignore_errors=True)
                    con = self.get_db()
                    con.execute("DELETE FROM eventos WHERE id=?", (eid,))
                    con.commit()
                    con.close()
                    print(f"[captura] cuota: evento antiguo {eid} borrado")
        except Exception as e:
            print(f"[captura] poda eventos: {e}")

        # Caché persistente (F5.3): primero retención por antigüedad (días),
        # luego cuota de tamaño (borra lo más antiguo primero).
        try:
            dias = float(self.cfg.get("retencion_cache_dias", 30.0))
            limite_ts = time.time() - dias * 86400
            cuota_c = min(self.cfg.get("cuota_cache_gb", 30.0),
                          self.cfg.get("cuota_cache_gb_max", 50.0)) * 1e9
            tam_c = self._tam_dir(self.cache_dir)
            if os.path.isdir(self.cache_dir):
                # 1) retención: borrar fotos más viejas que N días
                for root, _, files in os.walk(self.cache_dir):
                    for fn in files:
                        if not (fn.startswith("foto_") and fn.endswith(".jpg")):
                            continue
                        try:
                            fts = float(fn[5:-4])
                        except ValueError:
                            continue
                        if fts < limite_ts:
                            p = os.path.join(root, fn)
                            try:
                                tam_c -= os.path.getsize(p)
                                os.remove(p)
                            except OSError:
                                pass
                # 2) cuota: si aún supera, borrar por mtime ascendente
                if tam_c > cuota_c:
                    todos = []
                    for root, _, files in os.walk(self.cache_dir):
                        for fn in files:
                            p = os.path.join(root, fn)
                            try:
                                todos.append((os.path.getmtime(p), p,
                                              os.path.getsize(p)))
                            except OSError:
                                pass
                    todos.sort()
                    for _, p, sz in todos:
                        if tam_c <= cuota_c:
                            break
                        try:
                            os.remove(p)
                            tam_c -= sz
                        except OSError:
                            pass
        except Exception as e:
            print(f"[captura] poda cache: {e}")

    # ── procesado de un punto ──────────────────────────────────────────
    def _procesar_punto(self, user_id, ts, lat, lon):
        with self.lock:
            r_act = self.cfg["radio_activa_m"]
            r_cap = self.cfg["radio_captura_m"]
            r_ev = self.cfg["radio_evento_m"]
            cams_u = self.cams.setdefault(str(user_id), {})
            c1500 = self.cams_cerca(lat, lon, r_act)
            ids_ahora = set()
            for dist, cam in c1500:
                cid = self._cid(cam)
                ids_ahora.add(cid)
                est = cams_u.setdefault(cid, self._estado_nuevo(cam))
                est["dist_prev"] = est["ultima_dist"]
                est["ultima_dist"] = dist
                # F5.9: menor distancia alcanzada en esta pasada (para saber
                # si de verdad pasaste por delante o solo "rozaste" el radio)
                if est["dist_min"] is None or dist < est["dist_min"]:
                    est["dist_min"] = dist
                estado_ant = est["estado"]

                if dist <= r_ev:
                    # Entrada en el radio de evento (viene de >100 m)
                    if est["entrada_ts"] is None and (
                            est["dist_prev"] is None or est["dist_prev"] > r_ev):
                        est["entrada_ts"] = ts
                        est["salida_ts"] = None
                        print(f"[captura] ENTRADA 100m user={user_id} "
                              f"cámara {cid} ts={ts:.1f}")
                    est["estado"] = EST_EVENTO
                    est["en_post"] = False
                    self._encolar_captura(user_id, cid, cam, ts, est)
                elif dist <= r_cap:
                    # Salió de los 100 m: registrar salida, seguir capturando
                    # la ventana posterior (60 s) y finalizar al completarla
                    if estado_ant == EST_EVENTO and est["entrada_ts"] is not None:
                        if est["salida_ts"] is None:
                            est["salida_ts"] = ts
                            est["en_post"] = True
                            print(f"[captura] SALIDA 100m user={user_id} "
                                  f"cámara {cid} ts={ts:.1f} (post 60s)")
                        if ts - est["salida_ts"] >= self.cfg["ventana_despues_s"]:
                            self._finalizar_evento(user_id, cid, est)
                            est["en_post"] = False
                    est["estado"] = EST_EVENTO if est.get("en_post") else EST_CAPTURANDO
                    self._encolar_captura(user_id, cid, cam, ts, est)
                else:  # ≤1500 m: activa
                    if estado_ant == EST_EVENTO and est["entrada_ts"] is not None:
                        self._finalizar_evento(user_id, cid, est)
                    est["estado"] = EST_ACTIVA
                    if not est["ctx_hecho"]:
                        est["ctx_hecho"] = True
                        self._encolar_descarga(user_id, cid, cam, ts, tipo="ctx")

            # Cortar cámaras de ESTE usuario que ya no están en el radio
            for cid, est in list(cams_u.items()):
                if est["estado"] != EST_INACTIVA and cid not in ids_ahora:
                    if est["entrada_ts"] is not None:
                        self._finalizar_evento(user_id, cid, est)
                    self._cortar_buffer(user_id, cid)
                    self._reset_estado(est)

    # ── gestión de buffers ─────────────────────────────────────────────
    def _dir_buffer(self, user_id, cid):
        return os.path.join(self.temps_dir, sanitizar_id(str(user_id)),
                            sanitizar_id(cid))

    def _estado_nuevo(self, cam):
        return {
            "cam": cam,
            "estado": EST_INACTIVA,
            "ultima_dist": None,
            "dist_prev": None,
            "ultima_captura_ts": None,
            "entrada_ts": None,
            "salida_ts": None,
            "en_post": False,
            "ctx_hecho": False,
            "dist_min": None,          # F5.9: menor distancia alcanzada (m)
        }

    def _reset_estado(self, est):
        est["estado"] = EST_INACTIVA
        est["ultima_dist"] = None
        est["dist_prev"] = None
        est["ultima_captura_ts"] = None
        est["entrada_ts"] = None
        est["salida_ts"] = None
        est["en_post"] = False
        est["ctx_hecho"] = False
        est["dist_min"] = None

    def _poda_buffer(self, user_id, cid, _est=None):
        """Poda el buffer temporal SOLO cuando es seguro hacerlo.

        Regla (corrige pérdida de pasadas):
        - EST_EVENTO / post (usuario dentro del evento o finalizando):
          NO se poda NADA — la pasada está ocurriendo y sus fotos son la
          única copia hasta que el evento se materialice. Si el evento se
          descarta o tarda, las fotos se conservan (las limpia _cortar_buffer
          al alejarse o la poda por cuota global).
        - EST_CAPTURANDO (100-500 m, sin evento aún): poda a la ventana que
          un futuro evento podría necesitar (buffer_s, con margen amplio).
        - EST_ACTIVA (>500 m) / INACTIVA: sin evento posible → cortar todo.

        IMPORTANTE: se llama desde hilos del pool (descargas) que NO pueden
        tomar self.lock (lo retiene _procesar_punto durante la finalización
        de eventos → deadlock). Por eso el estado se pasa como argumento
        (_est) cuando el llamador ya lo tiene, o se lee SIN lock aquí (la
        lectura de un dict es atómica en CPython; el riesgo de una decisión
        de poda con 1 ciclo de retraso es despreciable).
        """
        dir_buf = self._dir_buffer(user_id, cid)
        if not os.path.isdir(dir_buf):
            return
        # Estado actual de la cámara: usar el pasado o leer sin lock
        est = _est if _est is not None else (
            (self.cams.get(str(user_id)) or {}).get(cid))
        estado = est["estado"] if est else EST_INACTIVA
        entrada_ts = est.get("entrada_ts") if est else None
        en_post = est.get("en_post") if est else False
        if estado == EST_EVENTO or en_post:
            return  # pasada en curso o finalizando: no tocar
        if estado in (EST_ACTIVA, EST_INACTIVA) or est is None:
            # Sin evento posible: el buffer ya no servirá → vaciarlo entero
            try:
                for fn in os.listdir(dir_buf):
                    p = os.path.join(dir_buf, fn)
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            except OSError:
                pass
            return
        # EST_CAPTURANDO: podar a la ventana máxima + margen generoso.
        # Si hay entrada reciente (evento que acaba de empezar), no bajar
        # de (entrada - ventana_antes - margen) para no comer el inicio.
        limite = self.ultimo_ts.get(str(user_id), 0.0) - self.cfg["buffer_s"]
        if entrada_ts is not None:
            protege = entrada_ts - self.cfg["ventana_antes_s"] - 15.0
            limite = min(limite, protege)
        for fn in os.listdir(dir_buf):
            if fn.startswith("foto_") and fn.endswith(".jpg"):
                try:
                    fts = float(fn[5:-4])
                except ValueError:
                    continue
                if fts < limite:
                    try:
                        os.remove(os.path.join(dir_buf, fn))
                    except OSError:
                        pass

    def _poda_global(self, user_id):
        with self.lock:
            cams_u = self.cams.get(str(user_id), {})
            for cid, est in cams_u.items():
                self._poda_buffer(user_id, cid, _est=est)

    def _cortar_buffer(self, user_id, cid):
        dir_buf = self._dir_buffer(user_id, cid)
        if os.path.isdir(dir_buf):
            shutil.rmtree(dir_buf, ignore_errors=True)

    # ── descargas ──────────────────────────────────────────────────────
    def _cid(self, cam):
        base = cam.get("id") or "%s_%.5f_%.5f" % (
            cam.get("fuente", "cam"), cam["lat"], cam["lon"])
        return str(base)

    def _encolar_descarga(self, user_id, cid, cam, ts, tipo,
                          forzar_cache=False):
        fut = self.pool.submit(self._descargar_y_guardar, user_id, cid,
                               cam, ts, tipo, forzar_cache)
        self.pendientes.setdefault((str(user_id), cid), []).append(fut)

    def _encolar_captura(self, user_id, cid, cam, ts, est):
        inter = self.cfg["intervalo_captura_s"]
        ult = est.get("ultima_captura_ts")
        if ult is not None and (ts - ult) < (inter - 0.1):
            return
        est["ultima_captura_ts"] = ts
        # Durante una pasada real (dentro de 100 m, o en post-evento) el
        # caché guarda TODAS las fotos sin dedup (cinta completa por
        # coincidencia de timestamp); fuera de la pasada, dedup normal.
        en_pasada = (est["estado"] == EST_EVENTO) or bool(est.get("en_post"))
        self._encolar_descarga(user_id, cid, cam, ts, tipo="captura",
                               forzar_cache=en_pasada)

    def _descargar_y_guardar(self, user_id, cid, cam, ts, tipo,
                             forzar_cache=False):
        try:
            datos = self._descargar_url(cam["url"])
        except Exception:
            datos = None
        if not datos:
            self.descargas_fallo += 1
            # fallo repetido (3+) ⇒ cámara inalcanzable → muerta
            nf = self._fallos_cam.get(cid, 0) + 1
            self._fallos_cam[cid] = nf
            if nf >= 3:
                self._marcar_muerta(cid, "fallo descarga")
            return
        self.descargas_ok += 1
        self._fallos_cam[cid] = 0
        # Si la fuente vuelve a servir imagen REAL (no placeholder) → viva
        if not self._es_placeholder(datos):
            self._marcar_viva(cid)
        # Buffer temporal (alimenta eventos con su ventana)
        dir_buf = self._dir_buffer(user_id, cid)
        os.makedirs(dir_buf, exist_ok=True)
        ruta = os.path.join(dir_buf, "foto_%s.jpg" % ts)
        i = 1
        while os.path.exists(ruta):
            ruta = os.path.join(dir_buf, "foto_%s_%d.jpg" % (ts, i))
            i += 1
        try:
            with open(ruta, "wb") as f:
                f.write(datos)
        except OSError:
            return
        self._poda_buffer(user_id, cid)
        # Caché persistente (F5.3): con dedup fuera de la pasada; SIN dedup
        # (cinta completa) durante la pasada → el caché de 30 días puede
        # reconstruir el evento por coincidencia de timestamp.
        if tipo == "captura":
            self._cache_dedup(user_id, cid, ts, datos, forzar=forzar_cache)

    def _dir_cache(self, user_id, cid):
        return os.path.join(self.cache_dir, sanitizar_id(str(user_id)),
                            sanitizar_id(cid))

    def _cache_dedup(self, user_id, cid, ts, datos, forzar=False):
        """Guarda en el caché persistente si el dHash difiere del último.

        El umbral (cfg['umbral_dedup'], 4 bits por defecto) decide si una
        imagen es 'la misma' (ruido/compresión) o un cambio real de escena.

        forzar=True (pasada en curso, dentro de 100 m): guarda SIEMPRE, sin
        dedup. Así el caché de 30 días contiene la cinta COMPLETA de la
        pasada (foto_<ts>.jpg por cada captura) y puede reconstruir el
        evento por coincidencia temporal aunque el buffer temporal falle.
        """
        try:
            if not forzar:
                h = dhash_bytes(datos)
                if h < 0:
                    return
                clave = (str(user_id), cid)
                with self.lock:
                    ultimo = getattr(self, "_ultimo_hash", {}).get(clave)
                    if ultimo is not None and hamming(h, ultimo) <= int(
                            self.cfg.get("umbral_dedup", 4)):
                        return  # misma imagen → descartar
                    if not hasattr(self, "_ultimo_hash"):
                        self._ultimo_hash = {}
                    self._ultimo_hash[clave] = h
            dir_c = self._dir_cache(user_id, cid)
            os.makedirs(dir_c, exist_ok=True)
            ruta = os.path.join(dir_c, "foto_%s.jpg" % ts)
            n = 1
            while os.path.exists(ruta):
                ruta = os.path.join(dir_c, "foto_%s_%d.jpg" % (ts, n))
                n += 1
            with open(ruta, "wb") as f:
                f.write(datos)
        except Exception:
            pass

    def _descargar_url(self, url):
        datos = self._fetch(url, referer=self._referer(url))
        if datos is None:
            proxy_url = PROXY_IMAGEN + urllib.parse.quote(url, safe="")
            datos = self._fetch(proxy_url, referer="http://127.0.0.1:8000/")
        return datos

    def _referer(self, url):
        try:
            return "https://%s/" % urllib.parse.urlparse(url).netloc
        except Exception:
            return "https://www.geobilbao.eus/"

    def _fetch(self, url, referer):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) TrackCam/1.0",
                "Referer": referer,
                "Accept": "image/*,*/*;q=0.8",
            })
            with urllib.request.urlopen(req, timeout=TIMEOUT_DESCARGA) as r:
                datos = r.read()
            if not es_imagen(datos):
                return None
            return datos
        except Exception:
            return None

    def _esperar_descargas(self, user_id, cid, timeout=10.0):
        futs = self.pendientes.pop((str(user_id), cid), [])
        fin = time.time() + timeout
        for f in futs:
            try:
                f.result(timeout=max(0.1, fin - time.time()))
            except Exception:
                pass

    # ── eventos ────────────────────────────────────────────────────────
    def _fotos_ventana(self, user_id, cid, entrada_ts, salida_ts):
        """Fotos en [entrada - antes, (salida o entrada+despues) + despues].

        Primero busca en el buffer temporal; si no hay suficientes, usa el
        CACHÉ persistente (30 días) como respaldo: el caché guarda la cinta
        completa de la pasada (foto_<ts>.jpg sin dedup desde F5.4), así que
        el evento se reconstruye por COINCIDENCIA TEMPORAL aunque el buffer
        se haya perdido/podado. Devuelve lista [(ts, ruta)] ordenada.
        """
        antes = self.cfg["ventana_antes_s"]
        despues = self.cfg["ventana_despues_s"]
        ini = entrada_ts - antes
        fin = (salida_ts if salida_ts else entrada_ts) + despues

        def _listar(dirp):
            out = []
            if os.path.isdir(dirp):
                for fn in os.listdir(dirp):
                    if not (fn.startswith("foto_") and fn.endswith(".jpg")):
                        continue
                    try:
                        fts = float(fn[5:-4])
                    except ValueError:
                        continue
                    if ini <= fts <= fin:
                        out.append((fts, os.path.join(dirp, fn)))
            out.sort()
            return out

        fotos = _listar(self._dir_buffer(user_id, cid))
        if len(fotos) >= 3:
            return fotos
        # Respaldo: caché persistente (cinta completa de la pasada)
        cache = _listar(self._dir_cache(user_id, cid))
        if len(cache) > len(fotos):
            print(f"[captura] evento reconstruido desde CACHÉ "
                  f"({len(cache)} fotos, buffer tenía {len(fotos)}) "
                  f"user={user_id} cámara {cid}")
            return cache
        return fotos

    def _finalizar_evento(self, user_id, cid, est):
        entrada = est.get("entrada_ts")
        if entrada is None:
            return
        cam = est["cam"]
        salida = est.get("salida_ts")
        self._esperar_descargas(user_id, cid, timeout=10.0)

        fotos = self._fotos_ventana(user_id, cid, entrada, salida)
        if len(fotos) < 3:
            est["entrada_ts"] = None
            est["salida_ts"] = None
            print(f"[captura] evento descartado (solo {len(fotos)} fotos) "
                  f"user={user_id} cámara {cid}")
            return

        # Placeholder REAL (imagen de error): solo si las fotos son el JPEG
        # de error CONOCIDO de la fuente (p.ej. geobilbao sirve un fijo de
        # 11015 B o 3915 B cuando la cámara está caída/placeholder). NO basta
        # con que todas las fotos tengan el mismo tamaño: las cámaras con
        # refresco lento (DGT 3 min, EJGV, windy...) sirven la MISMA imagen
        # real durante toda la pasada (20-40 s) y eso NO es un placeholder —
        # descartarlo borraría eventos legítimos. Comprobamos el tamaño de la
        # primera foto contra los tamaños de error conocidos.
        try:
            _ph_tams = {11015, 3915, 0}   # JPEG de error de geobilbao
            _tams = {os.path.getsize(p) for _, p in fotos}
            if len(_tams) == 1 and len(fotos) >= 3 \
                    and list(_tams)[0] in _ph_tams:
                est["entrada_ts"] = None
                est["salida_ts"] = None
                self._marcar_muerta(cid, "placeholder")   # ⚪ gris en el mapa
                print(f"[captura] evento descartado (placeholder real "
                      f"{list(_tams)[0]}B, {len(fotos)} fotos) "
                      f"user={user_id} cámara {cid}")
                return
        except OSError:
            pass

        eid = uuid.uuid4().hex[:12]
        dir_ev = os.path.join(self.eventos_dir, str(user_id), eid)
        os.makedirs(dir_ev, exist_ok=True)

        for i, (fts, src) in enumerate(fotos):
            shutil.copy2(src, os.path.join(dir_ev, "foto_%03d.jpg" % i))
        foto_ts = [fts for fts, _ in fotos]   # ts real de cada foto (para marcar el tramo de la pasada)

        n = len(fotos)
        video = os.path.join(dir_ev, "video.mp4")
        ok_video = self._hacer_video(dir_ev, video)
        video_rel = os.path.relpath(video, os.path.join(self.data_dir, "..")) \
            if ok_video else ""
        tam = sum(os.path.getsize(os.path.join(dir_ev, f))
                  for f in os.listdir(dir_ev) if f.endswith(".jpg"))
        if ok_video:
            tam += os.path.getsize(video)

        antes = self.cfg["ventana_antes_s"]
        despues = self.cfg["ventana_despues_s"]
        ts_ini = entrada - antes
        ts_fin = (salida if salida else entrada) + despues
        meta = {
            "id": eid,
            "user_id": str(user_id),
            "cam_id": cid,
            "cam_nombre": cam.get("nombre", "?"),
            "cam_fuente": cam.get("fuente", "?"),
            "lat": cam["lat"],
            "lon": cam["lon"],
            "url": cam.get("url", ""),
            "ts_entrada": entrada,
            "ts_salida": salida,
            "ts_inicio": ts_ini,
            "ts_fin": ts_fin,
            "foto_ts": foto_ts,
            "ventana_s": {"antes": antes, "despues": despues},
            "dist_min_m": est.get("dist_min"),   # F5.9: menor distancia real
            "n_fotos": n,
            "video": video_rel,
            "tam": tam,
            "fps": self.cfg["fps_video"],
            "creado": time.time(),
        }
        with open(os.path.join(dir_ev, "metadata.json"), "w",
                  encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        try:
            con = self.get_db()
            con.execute(
                "INSERT INTO eventos(user_id,id,cam_id,cam_nombre,lat,lon,"
                "ts_inicio,ts_fin,video,n_fotos,tam,dist_min_m) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(user_id), eid, cid, meta["cam_nombre"], cam["lat"],
                 cam["lon"], ts_ini, ts_fin, video_rel, n, tam,
                 est.get("dist_min")))
            con.commit()
            con.close()
        except sqlite3.Error as e:
            print(f"[captura] no se pudo registrar evento {eid}: {e}")

        est["entrada_ts"] = None
        est["salida_ts"] = None
        est["en_post"] = False
        est["ctx_hecho"] = True
        self.eventos_creados += 1
        print(f"[captura] EVENTO {eid} user={user_id} cámara {cid}: "
              f"{n} fotos, video={os.path.getsize(video) if ok_video else 0} B")

    def _hacer_video(self, dir_ev, video):
        fps = max(1, int(self.cfg["fps_video"]))
        base = [FFMPEG, "-y", "-loglevel", "error", "-framerate",
                str(fps), "-i", "foto_%03d.jpg"]
        for extra in ([], ["-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2"]):
            cmd = base + extra + [
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart", "video.mp4"]
            try:
                r = subprocess.run(cmd, cwd=dir_ev, timeout=120,
                                   capture_output=True)
                if r.returncode == 0 and os.path.exists(video) \
                        and os.path.getsize(video) > 0:
                    return True
            except Exception:
                pass
        return False

    def _cortar_por_inactividad(self, user_id):
        with self.lock:
            cams_u = self.cams.get(str(user_id), {})
            for cid, est in list(cams_u.items()):
                if est["estado"] != EST_INACTIVA:
                    if est["entrada_ts"] is not None:
                        self._finalizar_evento(user_id, cid, est)
                    self._cortar_buffer(user_id, cid)
                    self._reset_estado(est)
        try:
            fut = self.pool.submit(lambda: None)
            fut.result(timeout=30.0)
        except Exception:
            pass
        dir_u = os.path.join(self.temps_dir, sanitizar_id(str(user_id)))
        if os.path.isdir(dir_u):
            for nombre in os.listdir(dir_u):
                p = os.path.join(dir_u, nombre)
                if os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)

    # ── consultas para la API ──────────────────────────────────────────
    def buffers_info(self, user_id=None):
        with self.lock:
            items = list(self.cams.items())
        out = []
        for uid, cams_u in items:
            if user_id is not None and str(uid) != str(user_id):
                continue
            for cid, est in cams_u.items():
                if est["estado"] == EST_INACTIVA:
                    continue
                dir_buf = self._dir_buffer(uid, cid)
                n = tam = 0
                if os.path.isdir(dir_buf):
                    for fn in os.listdir(dir_buf):
                        if fn.endswith(".jpg"):
                            n += 1
                            try:
                                tam += os.path.getsize(os.path.join(dir_buf, fn))
                            except OSError:
                                pass
                out.append({
                    "user_id": str(uid),
                    "cam_id": cid,
                    "cam_nombre": est["cam"].get("nombre", "?"),
                    "estado": est["estado"],
                    "n_fotos": n,
                    "tam": tam,
                    "dist": round(est["ultima_dist"], 1) if est["ultima_dist"] else None,
                })
        out.sort(key=lambda x: (x["estado"], -x["n_fotos"]))
        return out

    def estado_actual(self):
        with self.lock:
            n_act = sum(1 for u in self.cams.values()
                        for e in u.values() if e["estado"] == EST_ACTIVA)
            n_cap = sum(1 for u in self.cams.values()
                        for e in u.values() if e["estado"] == EST_CAPTURANDO)
            n_ev = sum(1 for u in self.cams.values()
                       for e in u.values() if e["estado"] == EST_EVENTO)
        return {
            "camaras_activas": n_act,
            "camaras_capturando": n_cap,
            "eventos_pendientes": n_ev,
            "descargas_ok": self.descargas_ok,
            "descargas_fallo": self.descargas_fallo,
            "eventos_creados": self.eventos_creados,
            "usuarios_trackeando": len([u for u in self.cams
                                        if any(e["estado"] != EST_INACTIVA
                                               for e in self.cams[u].values())]),
        }
