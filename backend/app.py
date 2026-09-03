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

# Modo de operación:
#   ingesta (default) → recibe GPS (/track), login app, config OTA, control.
#                       Sirve en track.satetsunin.com SIN Cloudflare Access.
#   vision            → SOLO LECTURA: web del mapa, tracks, eventos, vídeos,
#                       exportaciones. Sirve en trackcam.satetsunin.com CON
#                       Cloudflare Access. NO arranca el motor de captura ni
#                       expone endpoints de escritura.
MODO = os.environ.get("TRACKCAM_MODO", "ingesta").strip().lower()
MODO_VISION = MODO == "vision"

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


def _solo_lectura():
    """En modo visión este endpoint de escritura no existe."""
    return JSONResponse({"error": "servidor de solo lectura (trackcam)"},
                        status_code=403)


if MODO_VISION:
    @app.api_route("/track", methods=["POST", "PUT", "PATCH", "DELETE"])
    async def _vision_no_track(request: Request):
        return _solo_lectura()

    @app.api_route("/api/logout", methods=["POST"])
    async def _vision_no_logout(request: Request):
        return _solo_lectura()

    @app.api_route("/api/usuarios", methods=["POST"])
    async def _vision_no_crear_usuario(request: Request):
        return _solo_lectura()

    @app.api_route("/api/usuarios/{uid}/password", methods=["POST"])
    async def _vision_no_password(request: Request, uid: int):
        return _solo_lectura()

    @app.api_route("/api/usuarios/{uid}", methods=["DELETE"])
    async def _vision_no_borrar_usuario(request: Request, uid: int):
        return _solo_lectura()

    @app.api_route("/api/evento/{eid}", methods=["DELETE"])
    async def _vision_no_borrar_evento(request: Request, eid: str):
        return _solo_lectura()

    @app.api_route("/api/limpiar_temps", methods=["POST"])
    async def _vision_no_limpiar(request: Request):
        return _solo_lectura()

    @app.api_route("/api/ajustes", methods=["POST"])
    async def _vision_no_ajustes(request: Request):
        return _solo_lectura()

    # En visión SÍ se permite el panel de control (/control + POST
    # /api/app_config) porque trackcam está detrás de Cloudflare Access y
    # el POST exige rol admin. El resto de escritura sigue bloqueado.

elif MODO == "ingesta":
    # track.satetsunin.com es SOLO la API que usa la app Android:
    #   POST /api/login · POST /track · GET /api/app_config
    #   GET /api/apk/version · GET /api/apk/download · POST /api/logout
    # NADA de web/mapa/control/eventos se expone aquí (sin Cloudflare Access).
    _INGESTA_MSG = "track.satetsunin.com es solo API de la app — usa trackcam.satetsunin.com para el mapa/control"

    def _solo_ingesta():
        return JSONResponse({"error": _INGESTA_MSG}, status_code=404)

    # Páginas web → fuera (el mapa/control viven en trackcam)
    for _p in ("/", "/replay", "/control", "/static"):
        @app.api_route(_p, methods=["GET", "POST", "PUT", "DELETE"])
        async def _ingesta_no_web(request: Request, _p: str = _p):
            return _solo_ingesta()

    # APIs de lectura/datos → fuera (solo trackcam las sirve)
    for _p in ("/api/track", "/api/ultimo_punto", "/api/eventos", "/api/pasadas",
               "/api/catalogo", "/api/verdes", "/api/cache", "/api/cache/{cam_id}/foto/{n}",
               "/api/temps", "/api/estado", "/api/evento/{eid}/video",
               "/api/evento/{eid}/foto/{n}", "/api/evento/{eid}/metadata",
               "/api/exportar/kml", "/api/exportar/gpx", "/api/exportar/todo",
               "/api/ajustes", "/api/usuarios"):
        @app.api_route(_p, methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
        async def _ingesta_no_api(request: Request, _p: str = _p):
            return _solo_ingesta()

    # Escritura de datos → fuera (solo recibe /track)
    for _p in ("/api/evento/{eid}", "/api/limpiar_temps", "/api/usuarios/{uid}",
               "/api/usuarios/{uid}/password", "/api/app_config"):
        @app.api_route(_p, methods=["POST", "PUT", "PATCH", "DELETE"])
        async def _ingesta_no_write(request: Request, _p: str = _p):
            return _solo_ingesta()


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


@app.post("/api/usuarios")
async def api_usuario_crear(request: Request):
    """Crea un usuario nuevo (admin): {username, password, rol?}."""
    u = _auth(request)
    if not u:
        return _pedir_auth()
    if u["rol"] != "admin":
        return JSONResponse({"error": "requiere rol admin"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", ""))
    rol = "admin" if body.get("rol") == "admin" else "user"
    if len(username) < 3 or len(password) < 4:
        return JSONResponse({"error": "usuario ≥3 y contraseña ≥4 caracteres"},
                            status_code=400)
    con = get_db()
    if con.execute("SELECT 1 FROM usuarios WHERE username=?",
                   (username,)).fetchone():
        con.close()
        return JSONResponse({"error": "el usuario ya existe"}, status_code=409)
    con.execute("INSERT INTO usuarios(username,pass_hash,rol,creado) VALUES(?,?,?,?)",
                (username, _hash_pass(password), rol, time.time()))
    con.commit()
    con.close()
    return {"ok": True, "usuario": username, "rol": rol}


@app.post("/api/usuarios/{uid}/password")
async def api_usuario_password(uid: int, request: Request):
    """Cambia la contraseña de un usuario. Admin puede cambiar la de
    cualquiera ({password}); un usuario normal solo la suya
    ({password_actual, password})."""
    u = _auth(request)
    if not u:
        return _pedir_auth()
    try:
        body = await request.json()
    except Exception:
        body = {}
    con = get_db()
    fila = con.execute("SELECT pass_hash, rol FROM usuarios WHERE id=?",
                       (uid,)).fetchone()
    if not fila:
        con.close()
        return JSONResponse({"error": "usuario no existe"}, status_code=404)
    es_mismo = int(u["id"]) == int(uid)
    if u["rol"] != "admin" and not es_mismo:
        con.close()
        return JSONResponse({"error": "solo puedes cambiar tu propia contraseña"},
                            status_code=403)
    nueva = str(body.get("password", ""))
    if len(nueva) < 4:
        con.close()
        return JSONResponse({"error": "contraseña ≥4 caracteres"},
                            status_code=400)
    # Un usuario normal debe confirmar la actual; el admin no (reset)
    if u["rol"] != "admin":
        actual = str(body.get("password_actual", ""))
        if not _verif_pass(actual, fila[0]):
            con.close()
            return JSONResponse({"error": "contraseña actual incorrecta"},
                                status_code=401)
    con.execute("UPDATE usuarios SET pass_hash=? WHERE id=?",
                (_hash_pass(nueva), uid))
    # invalidar sesiones previas del usuario (salvo la actual del admin)
    con.execute("DELETE FROM sesiones WHERE user_id=? AND token NOT IN "
                "(SELECT token FROM sesiones WHERE user_id=? LIMIT 1)",
                (uid, uid))
    con.commit()
    con.close()
    return {"ok": True, "usuario_id": uid}


@app.delete("/api/usuarios/{uid}")
def api_usuario_borrar(uid: int, request: Request):
    """Borra un usuario y sus datos (solo admin; no a sí mismo ni al último admin)."""
    u = _auth(request)
    if not u:
        return _pedir_auth()
    if u["rol"] != "admin":
        return JSONResponse({"error": "requiere rol admin"}, status_code=403)
    if int(u["id"]) == int(uid):
        return JSONResponse({"error": "no puedes borrarte a ti mismo"},
                            status_code=400)
    con = get_db()
    fila = con.execute("SELECT username FROM usuarios WHERE id=?", (uid,)).fetchone()
    if not fila:
        con.close()
        return JSONResponse({"error": "no existe"}, status_code=404)
    n_admin = con.execute("SELECT COUNT(*) FROM usuarios WHERE rol='admin'").fetchone()[0]
    con.close()
    if n_admin <= 1:
        # comprobar si el borrado es admin
        con = get_db()
        rol = con.execute("SELECT rol FROM usuarios WHERE id=?", (uid,)).fetchone()[0]
        con.close()
        if rol == "admin":
            return JSONResponse({"error": "debe quedar al menos un admin"},
                                status_code=400)
    import shutil
    for carpeta in (os.path.join(DATA, "eventos", str(uid)),
                    os.path.join(DATA, "temps", str(uid))):
        if os.path.isdir(carpeta):
            shutil.rmtree(carpeta, ignore_errors=True)
    con = get_db()
    con.execute("DELETE FROM tracks WHERE user_id=?", (str(uid),))
    con.execute("DELETE FROM eventos WHERE user_id=?", (str(uid),))
    con.execute("DELETE FROM sesiones WHERE user_id=?", (uid,))
    con.execute("DELETE FROM usuarios WHERE id=?", (uid,))
    con.commit()
    con.close()
    return {"ok": True, "borrado": fila[0]}


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


def _vel_entre(pts):
    """Calcula velocidad (km/h) entre puntos con ventana temporal.

    pts: lista de (ts, lat, lon). Para cada punto i se mide la distancia al
    punto i-k con dt≈5 s (ventana deslizante) — derivar punto a punto (1 s)
    amplifica el jitter GPS (el ruido de posición duplica la señal al
    caminar). Sobre esas muestras se aplica mediana móvil de 5 (robusta).
    Descarta dt absurdos y saltos de posición.
    """
    n = len(pts)
    if n == 0:
        return []
    import statistics as _st
    vels = [None] * n
    VENTANA_S = 5.0
    for i in range(1, n):
        # buscar el punto i-k con dt lo más cercano a VENTANA_S
        t_i = pts[i][0]
        mejor = None
        for j in range(i - 1, max(-1, i - 20), -1):
            if j < 0:
                break
            dt = t_i - pts[j][0]
            if dt <= 0:
                continue
            if mejor is None or abs(dt - VENTANA_S) < abs(mejor[0] - VENTANA_S):
                mejor = (dt, j)
            if dt >= VENTANA_S:
                break
        if mejor is None:
            continue
        dt, j = mejor
        if dt > 60:
            continue
        d = haversine(pts[j][1], pts[j][2], pts[i][1], pts[i][2])
        if d > 1000:  # salto GPS
            continue
        vels[i] = (d / dt) * 3.6
    out = []
    V = 2  # mediana móvil de 5 (ventana ±2)
    for i in range(n):
        if vels[i] is None:
            out.append(None)
            continue
        vec = [vels[j] for j in range(max(0, i - V), min(n, i + V + 1))
               if vels[j] is not None]
        out.append(round(_st.median(vec), 1) if vec else None)
    return out


def _modo_vel(v):
    """Clasifica la velocidad en un modo de transporte."""
    if v is None:
        return "?"
    if v < 1:
        return "parado"
    if v < 7:
        return "andando"
    if v < 20:
        return "bici"
    if v < 50:
        return "urbano"
    if v < 120:
        return "carretera"
    return "rapido"


@app.get("/api/track")
def api_track(request: Request):
    """GeoJSON del track con velocidad CALCULADA por el servidor (km/h).

    user normal: solo el suyo. admin: todos (?usuario=N) o el suyo.
    Cada feature incluye properties.v (km/h calculada, suavizada) y
    properties.modo (parado/andando/bici/urbano/carretera/rapido).
    """
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
    pts = [(r[0], r[1], r[2]) for r in filas]
    vels = _vel_entre(pts)
    feats = []
    for i, r in enumerate(filas):
        v = vels[i] if i < len(vels) else None
        feats.append({"type": "Feature",
                      "geometry": {"type": "Point",
                                   "coordinates": [r[2], r[1]]},
                      "properties": {"ts": r[0], "acc": r[3],
                                     "vel": r[4], "v": v,
                                     "modo": _modo_vel(v),
                                     "dev": r[5], "user_id": r[6]}})
    return {"type": "FeatureCollection", "features": feats,
            "vel_media": None}


@app.get("/api/ultimo_punto")
def api_ultimo_punto(request: Request):
    """Último punto del track con velocidad calculada (para velocímetro)."""
    u = _auth(request)
    if not u:
        return _pedir_auth()
    con = get_db()
    q = ("SELECT ts,lat,lon,acc,vel,dev FROM tracks")
    params = []
    conds = []
    if u["rol"] == "admin":
        vid = request.query_params.get("usuario")
        if vid:
            conds.append("user_id=?"); params.append(vid)
    else:
        conds.append("user_id=?"); params.append(str(u["id"]))
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY ts DESC LIMIT 2"
    filas = con.execute(q, params).fetchall()
    con.close()
    if not filas:
        return {"ultimo": None}
    filas = filas[::-1]  # cronológico
    pts = [(r[0], r[1], r[2]) for r in filas]
    vels = _vel_entre(pts)
    r = filas[-1]
    v = vels[-1] if vels else None
    return {"ultimo": {"ts": r[0], "lat": r[1], "lon": r[2],
                       "vel_gps": r[4], "v": v, "modo": _modo_vel(v)}}


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


@app.get("/api/cache")
def api_cache(request: Request):
    """Lista cámaras con imágenes en caché persistente (F5.3).

    Devuelve por cámara: cam_id, nombre, lat/lon resueltos del catálogo,
    nº de fotos, tamaño y primera/última fecha. Solo del usuario (admin
    puede pedir ?usuario=N).
    """
    u = _auth(request)
    if not u:
        return _pedir_auth()
    if u["rol"] == "admin":
        vid = request.query_params.get("usuario")
        uid = vid if vid else str(u["id"])
    else:
        uid = str(u["id"])
    # índice de catálogo por cam_id (original y sanitizado — los directorios
    # de caché convierten los puntos en guiones bajos)
    by_id = {}
    for c in camaras:
        cid = str(c.get("id") or "%s_%.5f_%.5f" % (c.get("fuente", "cam"),
                                                    c["lat"], c["lon"]))
        by_id[cid] = c
        by_id.setdefault(sanitizar_dir(cid), c)
    base = os.path.join(DATA, "cache", sanitizar_dir(uid))
    out = []
    # Filtro temporal opcional (punto F5.x): solo cámaras con fotos en
    # [desde, hasta]. Igual que /api/verdes y /api/eventos.
    try:
        ts_ini = float(request.query_params["desde"]) if request.query_params.get("desde") else None
    except ValueError:
        ts_ini = None
    try:
        ts_fin = float(request.query_params["hasta"]) if request.query_params.get("hasta") else None
    except ValueError:
        ts_fin = None
    if os.path.isdir(base):
        for cdir in sorted(os.listdir(base)):
            p = os.path.join(base, cdir)
            if not os.path.isdir(p):
                continue
            fotos = [f for f in os.listdir(p)
                     if f.startswith("foto_") and f.endswith(".jpg")]
            if not fotos:
                continue
            # Filtrar por rango [desde, hasta]: la cámara entra si alguna de
            # sus fotos cae dentro. Si no hay fotos en el rango, se salta.
            if ts_ini is not None or ts_fin is not None:
                _dentro = False
                for f in fotos:
                    try:
                        fts = float(f[5:-4].split("_")[0])
                    except ValueError:
                        continue
                    if (ts_ini is None or fts >= ts_ini) and \
                       (ts_fin is None or fts <= ts_fin):
                        _dentro = True
                        break
                if not _dentro:
                    continue
            tam = sum(os.path.getsize(os.path.join(p, f)) for f in fotos)
            # Detección de "sin señal": si todas las fotos del caché tienen
            # exactamente el mismo tamaño, la fuente sirve un placeholder fijo
            # (imagen de error) → la cámara no tiene señal real.
            try:
                _tams = {os.path.getsize(os.path.join(p, f)) for f in fotos}
                sin_senal = len(_tams) == 1 and len(fotos) >= 3
            except OSError:
                sin_senal = False
            ts_list = []
            for f in fotos:
                try:
                    ts_list.append(float(f[5:-4].split("_")[0]))
                except ValueError:
                    pass
            c = by_id.get(cdir, {})
            out.append({
                "cam_id": cdir,
                "nombre": c.get("nombre", cdir),
                "lat": c.get("lat"),
                "lon": c.get("lon"),
                "fuente": c.get("fuente", "?"),
                "n_fotos": len(fotos),
                "tam": tam,
                "primera_ts": min(ts_list) if ts_list else None,
                "ultima_ts": max(ts_list) if ts_list else None,
                "sin_senal": sin_senal,
            })
    out.sort(key=lambda x: -(x["ultima_ts"] or 0))
    return {"total_camaras": len(out), "camaras": out}


@app.get("/api/img_proxy")
def api_img_proxy(request: Request):
    """Proxy de imágenes de cámaras (foto ACTUAL en vivo).

    Autenticado (Bearer/?token=). Uso: /api/img_proxy?u=<url-encoded>.
    Añade Referer del dominio origen (anti-hotlink) y valida que la
    respuesta sea una imagen. Evita SSRF: solo admite http/https y
    resuelve el dominio (sin IPs privadas).
    """
    u = _auth(request)
    if not u:
        return _pedir_auth()
    import urllib.parse as _up
    url = request.query_params.get("u", "")
    if not url:
        return JSONResponse({"error": "falta u"}, status_code=400)
    try:
        p = _up.urlparse(url)
        if p.scheme not in ("http", "https") or not p.netloc:
            return JSONResponse({"error": "url inválida"}, status_code=400)
        import socket
        host = p.hostname or ""
        try:
            ip = socket.gethostbyname(host)
        except Exception:
            return JSONResponse({"error": "dns"}, status_code=502)
        if ip.startswith(("10.", "192.168.", "172.")) or ip == "127.0.0.1":
            return JSONResponse({"error": "dominio no permitido"},
                                status_code=403)
        import urllib.request as _ur
        req = _ur.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) TrackCam/1.0",
            "Referer": f"https://{p.netloc}/",
            "Accept": "image/*,*/*;q=0.8",
        })
        with _ur.urlopen(req, timeout=8) as r:
            datos = r.read()
        if datos[:3] == b"\xff\xd8\xff" or datos[:4] == b"\x89PNG" \
                or datos[:3] == b"GIF":
            from fastapi.responses import Response
            return Response(content=datos,
                            media_type="image/jpeg" if datos[:3] == b"\xff\xd8\xff" else "image/png")
        return JSONResponse({"error": "no es imagen"}, status_code=502)
    except Exception as e:
        return JSONResponse({"error": f"proxy: {e}"}, status_code=502)


@app.get("/api/cache/{cam_id}/foto/{n}")
def api_cache_foto(cam_id: str, n: str, request: Request):
    """Sirve una foto del caché (n = índice 000, 001... ordenado por ts)."""
    u = _auth(request)
    if not u:
        return _pedir_auth()
    if u["rol"] == "admin":
        vid = request.query_params.get("usuario")
        uid = vid if vid else str(u["id"])
    else:
        uid = str(u["id"])
    if any(c not in "0123456789" for c in n) or len(n) > 4:
        return JSONResponse({"error": "índice inválido"}, status_code=400)
    dir_c = os.path.join(DATA, "cache", sanitizar_dir(uid),
                         sanitizar_dir(cam_id))
    if not os.path.isdir(dir_c):
        return JSONResponse({"error": "no existe"}, status_code=404)
    fotos = sorted(f for f in os.listdir(dir_c)
                   if f.startswith("foto_") and f.endswith(".jpg"))
    idx = int(n)
    if idx >= len(fotos):
        return JSONResponse({"error": "no existe"}, status_code=404)
    return FileResponse(os.path.join(dir_c, fotos[idx]))


def sanitizar_dir(s):
    import re
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(s))[:80] or "x"


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
        "tam_cache": tam(os.path.join(DATA, "cache")) if os.path.exists(os.path.join(DATA, "cache")) else 0,
        "cuota_eventos_gb": motor.cfg["cuota_eventos_gb"],
        "cuota_eventos_gb_max": motor.cfg["cuota_eventos_gb_max"],
        "cuota_cache_gb": motor.cfg["cuota_cache_gb"],
        "cuota_cache_gb_max": motor.cfg["cuota_cache_gb_max"],
        "retencion_cache_dias": motor.cfg["retencion_cache_dias"],
        "umbral_dedup": motor.cfg["umbral_dedup"],
        "cuota_temps_mb": motor.cfg["cuota_temps_mb"],
        "usuarios_trackeando": est["usuarios_trackeando"],
    }


# ── Web estática ────────────────────────────────────────────────────────────
WEB = os.path.join(BASE, "web")
# Los estáticos solo se montan en visión (trackcam, tras Cloudflare Access).
# En ingesta (track) las páginas ya están bloqueadas arriba → no hace falta.
if MODO_VISION:
    app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(WEB, "index.html"))


@app.get("/replay")
def replay():
    return FileResponse(os.path.join(WEB, "replay.html"))


@app.get("/control")
def control():
    """Panel de control remoto: config de la app (OTA sin recompilar)."""
    return FileResponse(os.path.join(WEB, "control.html"))


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


# ── OTA (F4/F5): versión y descarga de la APK ───────────────────────────
APK_FILE = os.path.join(BASE, "apk", "trackcam-release.apk")
APK_VERSION_CODE = 7
APK_VERSION_NAME = "1.9"

@app.get("/api/apk/version")
def apk_version():
    """Versión de la APK para actualización OTA desde la app (pública)."""
    tam = os.path.getsize(APK_FILE) if os.path.exists(APK_FILE) else 0
    return {
        "versionCode": APK_VERSION_CODE,
        "versionName": APK_VERSION_NAME,
        "url": "/api/apk/download",
        "tam": tam,
    }

@app.get("/api/apk/download")
def apk_download():
    """Sirve el APK firmado (trackcam-release.apk)."""
    if not os.path.exists(APK_FILE):
        return JSONResponse({"error": "APK no disponible aún"}, status_code=404)
    return FileResponse(
        APK_FILE,
        media_type="application/vnd.android.package-archive",
        filename=f"trackcam-{APK_VERSION_NAME}.apk",
    )


# ── Config remota de la APP (OTA sin recompilar) ────────────────────────
APP_CONFIG_DEFAULTS = {
    "vel_vehiculo_kmh": 20,       # por encima = vehículo
    "vel_andando_kmh": 6,         # por debajo de vehículo y encima = andando
    "intervalo_vehiculo_s": 2,    # en vehículo: enviar cada 2 s
    "intervalo_andando_s": 10,    # andando: cada 10 s
    "intervalo_parado_s": 600,    # parado: cada 10 min (ahorra batería)
    "cola_offline": True,         # guardar puntos sin cobertura
    "cola_max": 5000,             # máx puntos pendientes en el móvil
    "radio_cache_m": 2000,        # radio del servidor para contexto (2 km)
    "version_config": 3,          # sube al cambiar para que la app refresque
}
APP_CONFIG_FILE = os.path.join(DATA, "app_config.json")


def _leer_app_config() -> dict:
    cfg = dict(APP_CONFIG_DEFAULTS)
    try:
        with open(APP_CONFIG_FILE, encoding="utf-8") as f:
            cfg.update({k: v for k, v in json.load(f).items()
                        if k in APP_CONFIG_DEFAULTS})
    except Exception:
        pass
    return cfg


def _guardar_app_config(cfg: dict):
    try:
        with open(APP_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


@app.get("/api/app_config")
def api_app_config():
    """Config remota para la app (pública de lectura): la app la descarga
    al arrancar y aplica frecuencias/radios sin necesidad de recompilar."""
    return _leer_app_config()


@app.post("/api/app_config")
async def api_app_config_set(request: Request):
    """Actualiza la config remota (solo admin). La app la recoge al arrancar."""
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
    cfg = _leer_app_config()
    cfg.update({k: v for k, v in cambios.items() if k in APP_CONFIG_DEFAULTS})
    cfg["version_config"] = int(cfg.get("version_config", 0)) + 1
    _guardar_app_config(cfg)
    return cfg


load_camaras()

# ── Motor de captura (F2, multi-usuario F5) ────────────────────────────────
# Solo el modo ingesta captura fotogramas/escribe eventos; el modo visión
# (trackcam.satetsunin.com) es de solo lectura y comparte la misma BD.
if not MODO_VISION:
    from backend.captura import MotorCaptura

    motor = MotorCaptura(
        db_path=DB,
        data_dir=DATA,
        get_db_fn=get_db,
        cams_cerca_fn=cams_cerca,
    )
    motor.start()
else:
    # Visión: el motor existe pero NO arranca (no captura nada). Así
    # /api/estado, /api/temps y /api/ajustes funcionan (ceros/vacíos)
    # sin romper con NameError: motor. El constructor solo carga cfg y
    # crea estructuras vacías; start() es lo que lanza hilos.
    from backend.captura import MotorCaptura

    motor = MotorCaptura(
        db_path=DB,
        data_dir=DATA,
        get_db_fn=get_db,
        cams_cerca_fn=cams_cerca,
    )
    print("[trackcam] MODO VISIÓN (solo lectura) — motor creado sin arrancar (captura inactiva)")
