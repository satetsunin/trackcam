#!/usr/bin/env python3
"""TrackCam — backend (Fase 5): auth multiusuario + receptor + API web.

Usuarios (BD data/tracks.db):
  · alvaro / 1234  → rol admin (superusuario: ve y controla todo)
  · test  / test   → rol user  (solo sus propios tracks y eventos)
Login: POST /api/login {username,password} → {token, usuario}.
Todas las rutas /api/* (salvo /api/login) exigen
  Authorization: Bearer <token>.
"""
import os
import sqlite3
import math
import json
import time
import secrets
import hashlib
import hmac
import threading
from datetime import datetime, timezone
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data")
EUROCAMS_JSON = os.environ.get(
    "EUROCAMS_JSON",
    os.path.expanduser("~/Escritorio/proyectos/eurocams/data/europa_camaras_consolidado.json"),
)
DB = os.path.join(DATA, "tracks.db")
SESSION_HORAS = 24 * 30  # sesión válida 30 días

os.makedirs(DATA, exist_ok=True)
app = FastAPI(title="TrackCam")

# ── BD ──────────────────────────────────────────────────────────────────────
def get_db():
    con = sqlite3.connect(DB, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def _hash_pass(password: str, salt_hex: str = None) -> str:
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 120_000)
    return salt.hex() + "$" + dk.hex()


def _verif_pass(password: str, stored: str) -> bool:
    try:
        salt_hex, dk_hex = stored.split("$")
    except Exception:
        return False
    return hmac.compare_digest(
        _hash_pass(password, salt_hex).split("$")[1], dk_hex)


def init_db():
    con = get_db()
    con.execute("""CREATE TABLE IF NOT EXISTS usuarios(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        pass_hash TEXT NOT NULL,
        rol TEXT NOT NULL DEFAULT 'user',
        creado REAL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS sesiones(
        token TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        creado REAL, expira REAL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS tracks(
        user_id TEXT, ts REAL, lat REAL, lon REAL, acc REAL, vel REAL, dev TEXT)""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_tracks_user_ts ON tracks(user_id, ts)")
    con.execute("""CREATE TABLE IF NOT EXISTS eventos(
        user_id TEXT, id TEXT PRIMARY KEY, cam_id TEXT, cam_nombre TEXT,
        lat REAL, lon REAL, ts_inicio REAL, ts_fin REAL, video TEXT,
        n_fotos INTEGER, tam INTEGER)""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_eventos_user ON eventos(user_id, ts_inicio)")
    # Usuarios semilla (solo si no existen)
    for u, p, rol in (("alvaro", "1234", "admin"), ("test", "test", "user")):
        if not con.execute("SELECT 1 FROM usuarios WHERE username=?",
                           (u,)).fetchone():
            con.execute("INSERT INTO usuarios(username,pass_hash,rol,creado) "
                        "VALUES(?,?,?,?)", (u, _hash_pass(p), rol, time.time()))
    con.commit()
    con.close()


init_db()

# ── Auth helpers ────────────────────────────────────────────────────────────
def _usuario_por_token(token: str):
    if not token:
        return None
    con = get_db()
    fila = con.execute(
        "SELECT s.user_id, u.username, u.rol FROM sesiones s "
        "JOIN usuarios u ON u.id=s.user_id WHERE s.token=? AND s.expira>?",
        (token, time.time())).fetchone()
    con.close()
    if not fila:
        return None
    return {"id": fila[0], "username": fila[1], "rol": fila[2]}


def _auth(request: Request):
    """Devuelve el usuario autenticado o None."""
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return _usuario_por_token(auth[7:].strip())
    # fallback: token en query (?token=) para el emisor/APK simple
    tok = request.query_params.get("token")
    if tok:
        return _usuario_por_token(tok)
    return None


def _pedir_auth():
    return JSONResponse({"error": "no autorizado"}, status_code=401)


@app.post("/api/login")
async def api_login(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", ""))
    if not username or not password:
        return JSONResponse({"error": "faltan credenciales"}, status_code=400)
    con = get_db()
    fila = con.execute("SELECT id, pass_hash, rol FROM usuarios WHERE username=?",
                       (username,)).fetchone()
    con.close()
    if not fila or not _verif_pass(password, fila[1]):
        return JSONResponse({"error": "usuario o contraseña incorrectos"},
                            status_code=401)
    token = secrets.token_hex(24)
    ahora = time.time()
    con = get_db()
    con.execute("DELETE FROM sesiones WHERE expira<?", (ahora,))
    con.execute("INSERT INTO sesiones(token,user_id,creado,expira) VALUES(?,?,?,?)",
                (token, fila[0], ahora, ahora + SESSION_HORAS * 3600))
    con.commit()
    con.close()
    return {"token": token,
            "usuario": {"id": fila[0], "username": username, "rol": fila[2]}}


@app.post("/api/logout")
async def api_logout(request: Request):
    u = _auth(request)
    if u:
        auth = request.headers.get("Authorization", "")
        tok = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if tok:
            con = get_db()
            con.execute("DELETE FROM sesiones WHERE token=?", (tok,))
            con.commit()
            con.close()
    return {"ok": True}


@app.get("/api/usuarios")
def api_usuarios(request: Request):
    """Lista usuarios (solo admin)."""
    u = _auth(request)
    if not u:
        return _pedir_auth()
    if u["rol"] != "admin":
        return JSONResponse({"error": "requiere rol admin"}, status_code=403)
    con = get_db()
    filas = con.execute(
        "SELECT id, username, rol, creado, "
        "(SELECT COUNT(*) FROM tracks t WHERE t.user_id=usuarios.id) n_tracks, "
        "(SELECT COUNT(*) FROM eventos e WHERE e.user_id=usuarios.id) n_eventos "
        "FROM usuarios ORDER BY id").fetchall()
    con.close()
    return [{"id": r[0], "username": r[1], "rol": r[2], "creado": r[3],
             "tracks": r[4], "eventos": r[5]} for r in filas]


# ── Índice geo (grid hash sobre la BD de EuroCams) ─────────────────────────
CELL = 0.02
camaras = []
grid = {}
lock_grid = threading.Lock()


def haversine(lat1, lon1, lat2, lon2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1); dl = math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*R*math.asin(math.sqrt(a))


def load_camaras():
    global camaras, grid
    if not os.path.exists(EUROCAMS_JSON):
        print(f"[trackcam] AVISO: no existe {EUROCAMS_JSON}")
        return 0
    with open(EUROCAMS_JSON) as f:
        data = json.load(f)
    cams = data if isinstance(data, list) else data.get("camaras", data.get("cameras", []))
    grid = {}
    camaras = []
    for c in cams:
        lat, lon = c.get("lat"), c.get("lon")
        if lat is None or lon is None:
            continue
        url = c.get("imagen") or c.get("url_imagen")
        if not url:
            continue
        cam = {"id": c.get("id"), "nombre": c.get("nombre", "?"),
               "lat": float(lat), "lon": float(lon), "url": url,
               "fuente": c.get("fuente", "?"), "pais": c.get("pais", "?")}
        camaras.append(cam)
    for i, c in enumerate(camaras):
        gx, gy = int(c["lon"]/CELL), int(c["lat"]/CELL)
        grid.setdefault((gx, gy), []).append(i)
    print(f"[trackcam] {len(camaras)} cámaras cargadas de EuroCams")
    return len(camaras)


def cams_cerca(lat, lon, radio):
    gx, gy = int(lon/CELL), int(lat/CELL)
    span = int(radio / (CELL * 111000.0)) + 1
    out = []
    with lock_grid:
        for dx in range(-span, span+1):
            for dy in range(-span, span+1):
                for i in grid.get((gx+dx, gy+dy), []):
                    c = camaras[i]
                    d = haversine(lat, lon, c["lat"], c["lon"])
                    if d <= radio:
                        out.append((d, c))
    out.sort(key=lambda x: x[0])
    return out


def _ids_camaras_por_pasadas(user_id, ts_ini=None, ts_fin=None):
    """Cam_id distintos con evento en el periodo (para pintar verdes)."""
    con = get_db()
    q = "SELECT DISTINCT cam_id FROM eventos WHERE user_id=?"
    params = [str(user_id)]
    if ts_ini:
        q += " AND ts_fin >= ?"; params.append(ts_ini)
    if ts_fin:
        q += " AND ts_inicio <= ?"; params.append(ts_fin)
    filas = con.execute(q, params).fetchall()
    con.close()
    return {r[0] for r in filas}


def _cams_geo_verdes(user_id, ts_ini=None, ts_fin=None):
    """Lista {lat, lon, nombre, cam_id} de cámaras verdes en el periodo."""
    ids = _ids_camaras_por_pasadas(user_id, ts_ini, ts_fin)
    # Resolver coordenadas de los cam_id contra el catálogo de EuroCams
    by_id = {}
    for c in camaras:
        cid = str(c.get("id") or "%s_%.5f_%.5f" % (c.get("fuente", "cam"),
                                                    c["lat"], c["lon"]))
        by_id[cid] = c
    out = []
    for cid in ids:
        c = by_id.get(cid)
        if c:
            out.append({"cam_id": cid, "nombre": c.get("nombre", "?"),
                        "lat": c["lat"], "lon": c["lon"],
                        "fuente": c.get("fuente", "?")})
    return out


# ── API: track ─────────────────────────────────────────────────────────────
@app.post("/track")
async def track(request: Request):
    """APK manda posición. Requiere auth (Bearer o ?token=)."""
    u = _auth(request)
    if not u:
        return _pedir_auth()
    try:
        body = await request.json()
    except Exception:
        body = {}
    lat = body.get("lat") or request.query_params.get("lat")
    lon = body.get("lon") or request.query_params.get("lon")
    if lat is None or lon is None:
        return JSONResponse({"error": "faltan lat/lon"}, status_code=400)
    lat, lon = float(lat), float(lon)
    ts = float(body.get("ts", time.time()))
    acc = float(body.get("acc", 0) or 0)
    vel = float(body.get("vel", 0) or 0)
    dev = str(body.get("dev", "desconocido"))[:40]

    con = get_db()
    con.execute("INSERT INTO tracks(user_id,ts,lat,lon,acc,vel,dev) VALUES(?,?,?,?,?,?,?)",
                (str(u["id"]), ts, lat, lon, acc, vel, dev))
    con.commit()
    cerca = cams_cerca(lat, lon, 1500)
    n500 = sum(1 for d, _ in cams_cerca(lat, lon, 500))
    n100 = sum(1 for d, _ in cams_cerca(lat, lon, 100))
    con.close()
    return {"ok": True, "user": u["username"], "pts": 1,
            "camaras_1500": len(cerca), "camaras_500": n500, "camaras_100": n100}


@app.get("/api/track")
def api_track(request: Request):
    """GeoJSON del track. user normal: solo el suyo. admin: todos (?usuario=N)
    o el suyo."""
    u = _auth(request)
    if not u:
        return _pedir_auth()
    con = get_db()
    q = "SELECT ts,lat,lon,acc,vel,dev,user_id FROM tracks"
    params = []
    conds = []
    if u["rol"] == "admin":
        vid = request.query_params.get("usuario")
        if vid:
            conds.append("user_id=?"); params.append(vid)
    else:
        conds.append("user_id=?"); params.append(str(u["id"]))
    ts_ini = request.query_params.get("desde")
    ts_fin = request.query_params.get("hasta")
    if ts_ini:
        conds.append("ts>=?"); params.append(float(ts_ini))
    if ts_fin:
        conds.append("ts<=?"); params.append(float(ts_fin))
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY ts ASC"
    filas = con.execute(q, params).fetchall()
    con.close()
    feats = [{"type": "Feature",
              "geometry": {"type": "Point", "coordinates": [r[1], r[0]]},
              "properties": {"ts": r[0], "acc": r[3], "vel": r[4],
                             "dev": r[5], "user_id": r[6]}}
             for r in filas]
    return {"type": "FeatureCollection", "features": feats}


# ── API: eventos ───────────────────────────────────────────────────────────
@app.get("/api/eventos")
def api_eventos(request: Request):
    u = _auth(request)
    if not u:
        return _pedir_auth()
    con = get_db()
    q = ("SELECT user_id,id,cam_id,cam_nombre,lat,lon,ts_inicio,ts_fin,video,"
         "n_fotos,tam FROM eventos")
    conds, params = [], []
    if u["rol"] == "admin":
        vid = request.query_params.get("usuario")
        if vid:
            conds.append("user_id=?"); params.append(vid)
    else:
        conds.append("user_id=?"); params.append(str(u["id"]))
    cam_id = request.query_params.get("cam_id")
    if cam_id:
        conds.append("cam_id=?"); params.append(cam_id)
    for p, campo in (("desde", "ts_inicio"), ("hasta", "ts_fin")):
        v = request.query_params.get(p)
        if v:
            conds.append(f"{campo}>=?" if p == "desde" else f"{campo}<=?")
            params.append(float(v))
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY ts_inicio DESC"
    filas = con.execute(q, params).fetchall()
    con.close()
    cols = ["user_id", "id", "cam_id", "cam_nombre", "lat", "lon",
            "ts_inicio", "ts_fin", "video", "n_fotos", "tam"]
    return [dict(zip(cols, r)) for r in filas]


@app.get("/api/pasadas")
def api_pasadas(request: Request):
    """Historial de pasadas por cámara: {cam_id, nombre, lat, lon, veces,
    ultima_ts, eventos:[...]}."""
    u = _auth(request)
    if not u:
        return _pedir_auth()
    uid = str(u["id"])
    con = get_db()
    filas = con.execute(
        "SELECT cam_id, cam_nombre, lat, lon, COUNT(*), MAX(ts_inicio) "
        "FROM eventos WHERE user_id=? GROUP BY cam_id ORDER BY MAX(ts_inicio) DESC",
        (uid,)).fetchall()
    con.close()
    out = []
    for cam_id, nombre, lat, lon, veces, ult in filas:
        out.append({"cam_id": cam_id, "nombre": nombre, "lat": lat,
                    "lon": lon, "veces": veces, "ultima_ts": ult})
    return out


@app.get("/api/catalogo")
def api_catalogo(request: Request):
    """Catálogo completo de cámaras (para pintar todas en el mapa).

    Campos: id, nombre, lat, lon, fuente, pais, url. Autenticado.
    """
    u = _auth(request)
    if not u:
        return _pedir_auth()
    out = []
    for c in camaras:
        cid = c.get("id") or "%s_%.5f_%.5f" % (c.get("fuente", "cam"),
                                                c["lat"], c["lon"])
        out.append({"id": cid, "nombre": c.get("nombre", "?"),
                    "lat": c["lat"], "lon": c["lon"],
                    "fuente": c.get("fuente", "?"),
                    "pais": c.get("pais", "?"),
                    "url": c.get("url", "")})
    return {"total": len(out), "camaras": out}


@app.get("/api/verdes")
def api_verdes(request: Request):
    """Cámaras verdes (pasadas) en el periodo indicado: ?desde=&hasta=."""
    u = _auth(request)
    if not u:
        return _pedir_auth()
    if u["rol"] == "admin":
        vid = request.query_params.get("usuario")
        uid = vid if vid else str(u["id"])
    else:
        uid = str(u["id"])
    ts_ini = request.query_params.get("desde")
    ts_fin = request.query_params.get("hasta")
    return _cams_geo_verdes(uid,
                            float(ts_ini) if ts_ini else None,
                            float(ts_fin) if ts_fin else None)


@app.get("/api/temps")
def api_temps(request: Request):
    u = _auth(request)
    if not u:
        return _pedir_auth()
    uid = str(u["id"]) if u["rol"] != "admin" else None
    buffers = motor.buffers_info(uid)
    return {
        "buffers": buffers,
        "total_camaras": len(buffers),
        "total_fotos": sum(b["n_fotos"] for b in buffers),
        "total_tam": sum(b["tam"] for b in buffers),
    }


@app.get("/api/estado")
def api_estado(request: Request):
    u = _auth(request)
    if not u:
        return _pedir_auth()
    con = get_db()
    if u["rol"] == "admin":
        vid = request.query_params.get("usuario")
        if vid:
            n = con.execute("SELECT COUNT(*) FROM tracks WHERE user_id=?",
                            (vid,)).fetchone()[0]
            n_ev = con.execute("SELECT COUNT(*) FROM eventos WHERE user_id=?",
                               (vid,)).fetchone()[0]
        else:
            n = con.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
            n_ev = con.execute("SELECT COUNT(*) FROM eventos").fetchone()[0]
    else:
        n = con.execute("SELECT COUNT(*) FROM tracks WHERE user_id=?",
                        (str(u["id"]),)).fetchone()[0]
        n_ev = con.execute("SELECT COUNT(*) FROM eventos WHERE user_id=?",
                           (str(u["id"]),)).fetchone()[0]
    con.close()

    def tam(p):
        t = 0
        for root, _, files in os.walk(p):
            t += sum(os.path.getsize(os.path.join(root, f)) for f in files)
        return t

    est = motor.estado_actual()
    return {
        "usuario": u["username"], "rol": u["rol"],
        "camaras_cargadas": len(camaras),
        "puntos_track": n,
        "eventos": n_ev,
        "camaras_activas": est["camaras_activas"],
        "camaras_capturando": est["camaras_capturando"],
        "descargas_ok": est["descargas_ok"],
        "descargas_fallo": est["descargas_fallo"],
        "tam_tracks_db": os.path.getsize(DB) if os.path.exists(DB) else 0,
        "tam_temps": tam(os.path.join(DATA, "temps")) if os.path.exists(os.path.join(DATA, "temps")) else 0,
        "tam_eventos": tam(os.path.join(DATA, "eventos")) if os.path.exists(os.path.join(DATA, "eventos")) else 0,
        "cuota_eventos_gb": motor.cfg["cuota_eventos_gb"],
        "cuota_eventos_gb_max": motor.cfg["cuota_eventos_gb_max"],
        "cuota_temps_mb": motor.cfg["cuota_temps_mb"],
        "usuarios_trackeando": est["usuarios_trackeando"],
    }


# ── Web estática ────────────────────────────────────────────────────────────
WEB = os.path.join(BASE, "web")
app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(WEB, "index.html"))


@app.get("/replay")
def replay():
    return FileResponse(os.path.join(WEB, "replay.html"))


# ── Eventos: servir vídeo/fotos + borrado (solo propietario o admin) ──────
def _permiso_evento(request: Request, eid: str):
    """Devuelve (user_id, eid) si el usuario puede acceder al evento."""
    u = _auth(request)
    if not u:
        return None, None, None
    con = get_db()
    fila = con.execute("SELECT user_id, id FROM eventos WHERE id=?",
                       (eid,)).fetchone()
    con.close()
    if not fila:
        return u, None, None
    if u["rol"] != "admin" and str(fila[0]) != str(u["id"]):
        return u, None, None  # no autorizado (lo trata el caller)
    return u, fila[0], fila[1]


@app.get("/api/evento/{eid}/video")
def evento_video(eid: str, request: Request):
    u, uid, eid2 = _permiso_evento(request, eid)
    if not u:
        return _pedir_auth()
    if not eid2:
        return JSONResponse({"error": "no existe o no autorizado"},
                            status_code=404)
    p = os.path.join(DATA, "eventos", str(uid), eid2, "video.mp4")
    if not os.path.exists(p):
        return JSONResponse({"error": "no existe"}, status_code=404)
    return FileResponse(p, media_type="video/mp4")


@app.get("/api/evento/{eid}/foto/{n}")
def evento_foto(eid: str, n: str, request: Request):
    u, uid, eid2 = _permiso_evento(request, eid)
    if not u:
        return _pedir_auth()
    if not eid2:
        return JSONResponse({"error": "no existe o no autorizado"},
                            status_code=404)
    if any(c not in "0123456789" for c in n) or len(n) > 3:
        return JSONResponse({"error": "foto inválida"}, status_code=400)
    p = os.path.join(DATA, "eventos", str(uid), eid2, "foto_%s.jpg" % n)
    if not os.path.exists(p):
        return JSONResponse({"error": "no existe"}, status_code=404)
    return FileResponse(p)


@app.get("/api/evento/{eid}/metadata")
def evento_metadata(eid: str, request: Request):
    u, uid, eid2 = _permiso_evento(request, eid)
    if not u:
        return _pedir_auth()
    if not eid2:
        return JSONResponse({"error": "no existe o no autorizado"},
                            status_code=404)
    p = os.path.join(DATA, "eventos", str(uid), eid2, "metadata.json")
    if not os.path.exists(p):
        return JSONResponse({"error": "no existe"}, status_code=404)
    return FileResponse(p, media_type="application/json")


@app.delete("/api/evento/{eid}")
def evento_borrar(eid: str, request: Request):
    u, uid, eid2 = _permiso_evento(request, eid)
    if not u:
        return _pedir_auth()
    if not eid2:
        return JSONResponse({"error": "no existe o no autorizado"},
                            status_code=404)
    import shutil
    carpeta = os.path.join(DATA, "eventos", str(uid), eid2)
    if os.path.exists(carpeta):
        shutil.rmtree(carpeta)
    con = get_db()
    con.execute("DELETE FROM eventos WHERE id=?", (eid2,))
    con.commit()
    con.close()
    return {"ok": True, "borrado": eid2}


@app.post("/api/limpiar_temps")
def limpiar_temps(request: Request):
    u = _auth(request)
    if not u:
        return _pedir_auth()
    import shutil
    carpeta = os.path.join(DATA, "temps")
    tam0 = 0
    if os.path.exists(carpeta):
        for root, _, files in os.walk(carpeta):
            tam0 += sum(os.path.getsize(os.path.join(root, f)) for f in files)
        shutil.rmtree(carpeta)
    return {"ok": True, "borrados_mb": round(tam0 / 1e6, 2)}


# ── Exportaciones (F4) ─────────────────────────────────────────────────────
def _escape_xml(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _track_puntos(request: Request):
    u = _auth(request)
    if not u:
        return [], None
    con = get_db()
    q = "SELECT ts, lat, lon FROM tracks"
    conds, params = [], []
    if u["rol"] == "admin":
        vid = request.query_params.get("usuario")
        if vid:
            conds.append("user_id=?"); params.append(vid)
    else:
        conds.append("user_id=?"); params.append(str(u["id"]))
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY ts"
    rows = con.execute(q, params).fetchall()
    con.close()
    return rows, u


@app.get("/api/exportar/kml")
def exportar_kml(request: Request):
    pts, u = _track_puntos(request)
    if not u:
        return _pedir_auth()
    con = get_db()
    if u["rol"] == "admin" and request.query_params.get("usuario"):
        evs = con.execute(
            "SELECT id,cam_nombre,lat,lon,ts_inicio,n_fotos FROM eventos "
            "WHERE user_id=? ORDER BY ts_inicio", (request.query_params["usuario"],)).fetchall()
    else:
        evs = con.execute(
            "SELECT id,cam_nombre,lat,lon,ts_inicio,n_fotos FROM eventos "
            "WHERE user_id=? ORDER BY ts_inicio", (str(u["id"]),)).fetchall()
    con.close()
    coords = " ".join(f"{lon},{lat},0" for _, lat, lon in pts)
    partes = ['<?xml version="1.0" encoding="UTF-8"?>',
              '<kml xmlns="http://www.opengis.net/kml/2.2">', '<Document>',
              '<name>TrackCam</name>']
    if coords:
        partes.append('<Placemark><name>Track</name><styleUrl>#track</styleUrl>'
                      f'<LineString><coordinates>{coords}</coordinates></LineString></Placemark>')
    partes.append('<Style id="track"><LineStyle><color>ff0ea5e9</color><width>5</width></LineStyle></Style>')
    partes.append('<Style id="ev"><IconStyle><color>ffef4444</color><scale>1.2</scale></IconStyle></Style>')
    for eid, nombre, lat, lon, ts, nf in evs:
        partes.append(
            f'<Placemark><name>{_escape_xml(nombre)}</name><styleUrl>#ev</styleUrl>'
            f'<description>{nf} fotos · <a href="/api/evento/{eid}/video">vídeo</a></description>'
            f'<Point><coordinates>{lon},{lat},0</coordinates></Point></Placemark>')
    partes.append('</Document></kml>')
    return PlainTextResponse("\n".join(partes),
                             media_type="application/vnd.google-earth.kml+xml")


@app.get("/api/exportar/gpx")
def exportar_gpx(request: Request):
    pts, u = _track_puntos(request)
    if not u:
        return _pedir_auth()
    partes = ['<?xml version="1.0" encoding="UTF-8"?>',
              '<gpx version="1.1" creator="TrackCam" xmlns="http://www.topografix.com/GPX/1/1">',
              '<trk><name>TrackCam</name><trkseg>']
    for ts, lat, lon in pts:
        partes.append(f'<trkpt lat="{lat}" lon="{lon}"><time>{datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}</time></trkpt>')
    partes.append('</trkseg></trk></gpx>')
    return PlainTextResponse("\n".join(partes), media_type="application/gpx+xml")


@app.get("/api/exportar/todo")
def exportar_todo(request: Request):
    pts, u = _track_puntos(request)
    if not u:
        return _pedir_auth()
    con = get_db()
    if u["rol"] == "admin" and request.query_params.get("usuario"):
        vid = request.query_params["usuario"]
        evs = con.execute(
            "SELECT * FROM eventos WHERE user_id=? ORDER BY ts_inicio", (vid,)).fetchall()
    else:
        evs = con.execute(
            "SELECT * FROM eventos WHERE user_id=? ORDER BY ts_inicio",
            (str(u["id"]),)).fetchall()
    con.close()
    cols = ["user_id", "id", "cam_id", "cam_nombre", "lat", "lon",
            "ts_inicio", "ts_fin", "video", "n_fotos", "tam"]
    return {
        "track": [{"ts": t, "lat": la, "lon": lo} for t, la, lo in pts],
        "eventos": [dict(zip(cols, e)) for e in evs],
        "exportado": datetime.now(timezone.utc).isoformat(),
    }


# ── Ajustes (F2/F5) ────────────────────────────────────────────────────────
@app.get("/api/ajustes")
def api_ajustes(request: Request):
    u = _auth(request)
    if not u:
        return _pedir_auth()
    return motor.cfg


@app.post("/api/ajustes")
async def api_ajustes_set(request: Request):
    u = _auth(request)
    if not u:
        return _pedir_auth()
    if u["rol"] != "admin":
        return JSONResponse({"error": "requiere rol admin"}, status_code=403)
    try:
        cambios = await request.json()
    except Exception:
        cambios = {}
    if not isinstance(cambios, dict):
        return JSONResponse({"error": "JSON inválido"}, status_code=400)
    return motor.actualizar_cfg(cambios)


load_camaras()

# ── Motor de captura (F2, multi-usuario F5) ────────────────────────────────
from backend.captura import MotorCaptura

motor = MotorCaptura(
    db_path=DB,
    data_dir=DATA,
    get_db_fn=get_db,
    cams_cerca_fn=cams_cerca,
)
motor.start()
