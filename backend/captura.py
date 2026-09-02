#!/usr/bin/env python3
"""TrackCam — Motor de captura (Fase 2).

Hilo daemon que cada ~2 s lee la última posición del track (SQLite) y captura
fotogramas de las cámaras de EuroCams cercanas:

  · ≤1500 m → cámara "activa": se marca y se toma 1 snapshot de contexto.
  · ≤500  m → captura continua cada 2 s a un ring buffer en data/temps/CAM_ID/
              (máx ~90 s de buffer, poda automática).
  · ≤100  m → EVENTO: se conserva la ventana 20 s ANTES + 40 s DESPUÉS del
              momento en que se cruzan los 100 m. Se monta un vídeo MP4 H.264
              (2 fps) con las fotos del buffer y se guardan también las
              imágenes originales en data/eventos/EVENTO_ID/.

La descarga de imágenes intenta primero la URL directa (con Referer) y si
falla (403/404/error) usa el proxy de EuroCams
http://127.0.0.1:8000/api/img?u=... con reintento. Robusto: un fallo de
descarga nunca rompe el motor.

NO importa app.py (evita import circular): recibe por constructor las
funciones de app.py que necesita (get_db, cams_cerca, haversine).
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

# Constantes de la especificación de Alvaro
INTERVALO_S = 2.0          # ciclo del motor
RADIO_ACTIVA = 1500.0      # m → cámara activa (snapshot de contexto)
RADIO_CAPTURA = 500.0      # m → captura continua (ring buffer)
RADIO_EVENTO = 100.0       # m → evento (vídeo + fotos)
BUFFER_S = 90.0            # profundidad máxima del ring buffer (segundos)
VENTANA_ANTES = 20.0       # s antes del cruce de 100 m que se conservan
VENTANA_DESPUES = 40.0     # s después del cruce de 100 m que se conservan
FPS_VIDEO = 2              # fotogramas por segundo del vídeo del evento
POOL_HILOS = 12            # descargas concurrentes máx
TIMEOUT_DESCARGA = 5       # s por descarga
PROXY_IMAGEN = "http://127.0.0.1:8000/api/img?u="
FFMPEG = "/usr/bin/ffmpeg"

# Estados posibles de una cámara
EST_INACTIVA = "inactiva"
EST_ACTIVA = "activa"          # ≤1500 m
EST_CAPTURANDO = "capturando"  # ≤500 m
EST_EVENTO = "evento"          # cruzó los 100 m (evento pendiente de montar)


def sanitizar_id(cid: str) -> str:
    """Convierte un id de cámara en un nombre de directorio seguro."""
    s = re.sub(r"[^A-Za-z0-9_-]", "_", str(cid))
    return s[:80] or "cam"


def es_imagen(datos: bytes) -> bool:
    """Comprueba el magic byte para descartar respuestas HTML de error."""
    if not datos:
        return False
    if datos[:3] == b"\xff\xd8\xff":      # JPEG
        return True
    if datos[:4] == b"\x89PNG":           # PNG
        return True
    if datos[:3] == b"GIF":               # GIF
        return True
    if datos[:4] == b"RIFF" and datos[8:12] == b"WEBP":  # WebP
        return True
    if datos[:2] == b"BM":                # BMP
        return True
    return False


class MotorCaptura:
    """Hilo daemon de captura de fotogramas + generación de eventos."""

    def __init__(self, db_path, data_dir, get_db_fn, cams_cerca_fn,
                 intervalo=INTERVALO_S):
        self.db_path = db_path
        self.data_dir = data_dir
        self.temps_dir = os.path.join(data_dir, "temps")
        self.eventos_dir = os.path.join(data_dir, "eventos")
        self.get_db = get_db_fn
        self.cams_cerca = cams_cerca_fn
        self.intervalo = intervalo
        os.makedirs(self.temps_dir, exist_ok=True)
        os.makedirs(self.eventos_dir, exist_ok=True)

        # Estado interno (protegido por self.lock)
        self.lock = threading_lock = __import__("threading").Lock()
        self.cams = {}          # cid -> dict de estado de la cámara
        self.ultimo_ts = 0.0    # último ts de track procesado por el motor
        self.descargas_ok = 0
        self.descargas_fallo = 0
        self.eventos_creados = 0

        # Pool de descargas (máx ~12 concurrentes)
        self.pool = ThreadPoolExecutor(max_workers=POOL_HILOS)
        self.pendientes = {}    # cid -> lista de futures (descargas en vuelo)

        # Hilo daemon
        self._hilo = None
        self._stop = __import__("threading").Event()

    # ── arranque / parada ────────────────────────────────────────────────
    def start(self):
        """Arranca el hilo daemon (idempotente)."""
        if self._hilo and self._hilo.is_alive():
            return
        # No reprocesar puntos anteriores al arranque
        try:
            con = self.get_db()
            fila = con.execute("SELECT MAX(ts) FROM tracks").fetchone()
            con.close()
            self.ultimo_ts = float(fila[0] or 0.0)
        except Exception:
            self.ultimo_ts = 0.0
        self._hilo = __import__("threading").Thread(
            target=self._loop, daemon=True, name="motor-captura")
        self._hilo.start()
        print(f"[captura] motor arrancado (ultimo_ts={self.ultimo_ts:.1f})")

    def stop(self):
        self._stop.set()

    # ── bucle principal ──────────────────────────────────────────────────
    def _loop(self):
        while not self._stop.is_set():
            try:
                self._ciclo()
            except Exception as e:
                print(f"[captura] error en ciclo: {e}")
            self._stop.wait(self.intervalo)

    def _ciclo(self):
        """Un ciclo: lee los puntos nuevos del track y los procesa en orden."""
        con = self.get_db()
        filas = con.execute(
            "SELECT ts, lat, lon FROM tracks WHERE ts > ? ORDER BY ts ASC",
            (self.ultimo_ts,)).fetchall()
        con.close()

        if filas:
            for ts, lat, lon in filas:
                try:
                    self._procesar_punto(ts, lat, lon)
                except Exception as e:
                    print(f"[captura] error en punto ts={ts}: {e}")
            # Poda de ring buffers (máx ~90 s) tras procesar el lote.
            # IMPORTANTE: los eventos pendientes NO se finalizan aquí aunque
            # el lote sea parcial (el track puede seguir enviando puntos en
            # el próximo ciclo); solo se finalizan en el cruce de salida, al
            # cortar por distancia o cuando el track se detiene.
            self._poda_global()
        else:
            # Sin puntos nuevos: el track se ha detenido. Finalizar eventos
            # pendientes (ventana recortada) y borrar los temporales de las
            # cámaras que nunca cruzaron los 100 m (poda).
            self._cortar_por_inactividad()

    # ── procesado de un punto del track ──────────────────────────────────
    def _procesar_punto(self, ts, lat, lon):
        with self.lock:
            c1500 = self.cams_cerca(lat, lon, RADIO_ACTIVA)
            ids_ahora = set()
            for dist, cam in c1500:
                cid = self._cid(cam)
                ids_ahora.add(cid)
                est = self.cams.setdefault(cid, self._estado_nuevo(cam))
                est["dist_prev"] = est["ultima_dist"]
                est["ultima_dist"] = dist
                estado_ant = est["estado"]

                if dist <= RADIO_EVENTO:
                    # Cruce de entrada a los 100 m (viene de >100 m)
                    if est["cruce_ts"] is None and (
                            est["dist_prev"] is None or est["dist_prev"] > RADIO_EVENTO):
                        est["cruce_ts"] = ts
                        print(f"[captura] CRUCE 100m cámara {cid} en ts={ts:.1f}")
                    est["estado"] = EST_EVENTO
                    self._encolar_captura(cid, cam, ts, est)
                elif dist <= RADIO_CAPTURA:
                    # Cruce de salida de los 100 m → montar el evento
                    if estado_ant == EST_EVENTO and est["cruce_ts"] is not None:
                        self._finalizar_evento(cid, est)
                    est["estado"] = EST_CAPTURANDO
                    self._encolar_captura(cid, cam, ts, est)
                else:  # ≤1500 m: activa + 1 snapshot de contexto
                    if estado_ant == EST_EVENTO and est["cruce_ts"] is not None:
                        self._finalizar_evento(cid, est)
                    est["estado"] = EST_ACTIVA
                    if not est["ctx_hecho"]:
                        est["ctx_hecho"] = True
                        self._encolar_descarga(cid, cam, ts, tipo="ctx")

            # Cortar las cámaras que ya no están en el radio de 1500 m
            for cid, est in list(self.cams.items()):
                if est["estado"] != EST_INACTIVA and cid not in ids_ahora:
                    if est["cruce_ts"] is not None:
                        self._finalizar_evento(cid, est)
                    self._cortar_buffer(cid)
                    self._reset_estado(est)
            self.ultimo_ts = ts

    # ── gestión de buffers ───────────────────────────────────────────────
    def _dir_buffer(self, cid):
        return os.path.join(self.temps_dir, sanitizar_id(cid))

    def _estado_nuevo(self, cam):
        return {
            "cam": cam,
            "estado": EST_INACTIVA,
            "ultima_dist": None,
            "dist_prev": None,
            "ultima_captura_ts": None,
            "cruce_ts": None,
            "ctx_hecho": False,
        }

    def _reset_estado(self, est):
        est["estado"] = EST_INACTIVA
        est["ultima_dist"] = None
        est["dist_prev"] = None
        est["ultima_captura_ts"] = None
        est["cruce_ts"] = None
        est["ctx_hecho"] = False

    def _poda_buffer(self, cid):
        """Borra las fotos del buffer de una cámara más viejas que BUFFER_S."""
        dir_buf = self._dir_buffer(cid)
        if not os.path.isdir(dir_buf):
            return
        limite = self.ultimo_ts - BUFFER_S
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

    def _poda_global(self):
        with self.lock:
            for cid in self.cams:
                self._poda_buffer(cid)

    def _cortar_buffer(self, cid):
        """Borra todos los temporales de una cámara (ya copiados al evento
        si lo hubo, o porque el usuario nunca llegó a <100 m)."""
        dir_buf = self._dir_buffer(cid)
        if os.path.isdir(dir_buf):
            shutil.rmtree(dir_buf, ignore_errors=True)

    # ── descargas ────────────────────────────────────────────────────────
    def _cid(self, cam):
        """Id estable de cámara. El JSON consolidado NO trae id, así que se
        sintetiza con fuente + coordenadas (único por posición)."""
        base = cam.get("id") or "%s_%.5f_%.5f" % (
            cam.get("fuente", "cam"), cam["lat"], cam["lon"])
        return str(base)

    def _encolar_descarga(self, cid, cam, ts, tipo):
        fut = self.pool.submit(self._descargar_y_guardar, cid, cam, ts, tipo)
        self.pendientes.setdefault(cid, []).append(fut)

    def _encolar_captura(self, cid, cam, ts, est):
        """Captura cada ~2 s (de ts, no de reloj) a un ring buffer."""
        ult = est.get("ultima_captura_ts")
        if ult is not None and (ts - ult) < (INTERVALO_S - 0.1):
            return
        est["ultima_captura_ts"] = ts
        self._encolar_descarga(cid, cam, ts, tipo="captura")

    def _descargar_y_guardar(self, cid, cam, ts, tipo):
        """Se ejecuta en el pool de hilos. Nunca lanza excepciones."""
        try:
            datos = self._descargar_url(cam["url"])
        except Exception:
            datos = None
        if not datos:
            self.descargas_fallo += 1
            return
        self.descargas_ok += 1
        dir_buf = self._dir_buffer(cid)
        os.makedirs(dir_buf, exist_ok=True)
        ruta = os.path.join(dir_buf, "foto_%s.jpg" % ts)
        i = 1
        while os.path.exists(ruta):  # mismo ts (raro): añadir sufijo
            ruta = os.path.join(dir_buf, "foto_%s_%d.jpg" % (ts, i))
            i += 1
        try:
            with open(ruta, "wb") as f:
                f.write(datos)
        except OSError:
            return
        self._poda_buffer(cid)

    def _descargar_url(self, url):
        """Directo con Referer → si falla, proxy de EuroCams (reintento)."""
        datos = self._fetch(url, referer=self._referer(url))
        if datos is None:
            # Reintento vía proxy (cámaras con protección anti-hotlink)
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

    def _esperar_descargas_cam(self, cid, timeout=10.0):
        """Espera (con tope) a que terminen las descargas en vuelo de una
        cámara antes de montar su evento."""
        futs = self.pendientes.pop(cid, [])
        fin = time.time() + timeout
        for f in futs:
            try:
                f.result(timeout=max(0.1, fin - time.time()))
            except Exception:
                pass

    # ── eventos ──────────────────────────────────────────────────────────
    def _fotos_ventana(self, cid, cruce_ts):
        """Fotos del ring buffer dentro de la ventana [cruce-20, cruce+40]."""
        ini = cruce_ts - VENTANA_ANTES
        fin = cruce_ts + VENTANA_DESPUES
        dir_buf = self._dir_buffer(cid)
        fotos = []
        if os.path.isdir(dir_buf):
            for fn in os.listdir(dir_buf):
                if not (fn.startswith("foto_") and fn.endswith(".jpg")):
                    continue
                try:
                    fts = float(fn[5:-4])
                except ValueError:
                    continue
                if ini <= fts <= fin:
                    fotos.append((fts, os.path.join(dir_buf, fn)))
        fotos.sort()
        return fotos

    def _finalizar_evento(self, cid, est):
        """Monta el evento de una cámara que cruzó los 100 m: copia las fotos
        de la ventana, genera video.mp4 (ffmpeg, 2 fps) y lo registra en BD."""
        cruce = est.get("cruce_ts")
        if cruce is None:
            return
        cam = est["cam"]
        # Dejar que terminen las descargas en vuelo para no perder fotos
        self._esperar_descargas_cam(cid, timeout=10.0)

        fotos = self._fotos_ventana(cid, cruce)
        if len(fotos) < 3:
            # Un cruce con 1-2 fotos no da un vídeo útil (p. ej. un punto
            # aislado del track dentro de los 100 m): se descarta el evento.
            est["cruce_ts"] = None
            print(f"[captura] evento descartado (solo {len(fotos)} fotos) "
                  f"cámara {cid}")
            return

        eid = uuid.uuid4().hex[:12]
        dir_ev = os.path.join(self.eventos_dir, eid)
        os.makedirs(dir_ev, exist_ok=True)

        # Copiar las imágenes originales de la ventana
        for i, (fts, src) in enumerate(fotos):
            shutil.copy2(src, os.path.join(dir_ev, "foto_%03d.jpg" % i))

        n = len(fotos)
        video = os.path.join(dir_ev, "video.mp4")
        ok_video = self._hacer_video(dir_ev, video)
        video_rel = os.path.relpath(video, os.path.join(self.data_dir, "..")) \
            if ok_video else ""
        tam = sum(os.path.getsize(os.path.join(dir_ev, f))
                  for f in os.listdir(dir_ev) if f.endswith(".jpg"))
        if ok_video:
            tam += os.path.getsize(video)

        ts_ini = cruce - VENTANA_ANTES
        ts_fin = cruce + VENTANA_DESPUES
        meta = {
            "id": eid,
            "cam_id": cid,
            "cam_nombre": cam.get("nombre", "?"),
            "cam_fuente": cam.get("fuente", "?"),
            "lat": cam["lat"],
            "lon": cam["lon"],
            "url": cam.get("url", ""),
            "ts_cruce": cruce,
            "ts_inicio": ts_ini,
            "ts_fin": ts_fin,
            "ventana_s": {"antes": VENTANA_ANTES, "despues": VENTANA_DESPUES},
            "n_fotos": n,
            "video": video_rel,
            "tam": tam,
            "fps": FPS_VIDEO,
            "creado": time.time(),
        }
        with open(os.path.join(dir_ev, "metadata.json"), "w",
                  encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        # Registrar en SQLite (tabla eventos)
        try:
            con = self.get_db()
            con.execute(
                "INSERT INTO eventos(id,cam_id,cam_nombre,lat,lon,ts_inicio,"
                "ts_fin,video,n_fotos,tam) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (eid, cid, meta["cam_nombre"], cam["lat"], cam["lon"],
                 ts_ini, ts_fin, video_rel, n, tam))
            con.commit()
            con.close()
        except sqlite3.Error as e:
            print(f"[captura] no se pudo registrar evento {eid}: {e}")

        est["cruce_ts"] = None
        est["ctx_hecho"] = True
        self.eventos_creados += 1
        print(f"[captura] EVENTO {eid} cámara {cid}: {n} fotos, "
                 f"video={os.path.getsize(video) if ok_video else 0} bytes, "
                 f"tam={tam}")

    def _hacer_video(self, dir_ev, video):
        """Monta el MP4 H.264 a 2 fps con las fotos foto_%03d.jpg."""
        base = [FFMPEG, "-y", "-loglevel", "error", "-framerate",
                str(FPS_VIDEO), "-i", "foto_%03d.jpg"]
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

    def _cortar_por_inactividad(self):
        """Sin puntos nuevos: cortar buffers y limpiar estados. Los temporales
        de cámaras que nunca cruzaron los 100 m se borran aquí (poda)."""
        with self.lock:
            for cid, est in list(self.cams.items()):
                if est["estado"] != EST_INACTIVA:
                    if est["cruce_ts"] is not None:
                        self._finalizar_evento(cid, est)
                    self._cortar_buffer(cid)
                    self._reset_estado(est)
        # Fuera del lock: esperar a que el pool drene todas las descargas
        # encoladas (centinela al final de la cola) y borrar los directorios
        # temporales huérfanos que las descargas tardías pudieran recrear.
        try:
            fut = self.pool.submit(lambda: None)
            fut.result(timeout=30.0)
        except Exception:
            pass
        if os.path.isdir(self.temps_dir):
            for nombre in os.listdir(self.temps_dir):
                p = os.path.join(self.temps_dir, nombre)
                if os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)

    # ── consultas para la API ────────────────────────────────────────────
    def buffers_info(self):
        """Estado de los buffers temporales para GET /api/temps."""
        with self.lock:
            items = list(self.cams.items())
        out = []
        for cid, est in items:
            if est["estado"] == EST_INACTIVA:
                continue
            dir_buf = self._dir_buffer(cid)
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
        """Resumen para /api/estado."""
        with self.lock:
            n_act = sum(1 for e in self.cams.values()
                        if e["estado"] == EST_ACTIVA)
            n_cap = sum(1 for e in self.cams.values()
                        if e["estado"] == EST_CAPTURANDO)
            n_ev = sum(1 for e in self.cams.values()
                       if e["estado"] == EST_EVENTO)
        return {
            "camaras_activas": n_act,
            "camaras_capturando": n_cap,
            "eventos_pendientes": n_ev,
            "descargas_ok": self.descargas_ok,
            "descargas_fallo": self.descargas_fallo,
            "eventos_creados": self.eventos_creados,
            "ultimo_ts_procesado": self.ultimo_ts,
        }
