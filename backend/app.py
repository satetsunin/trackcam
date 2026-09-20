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
import re
import sqlite3
import math
import json
import time
import secrets
import hashlib
import hmac
import threading
import asyncio
try:                                     # F5.36 — motor de diagnóstico
    from backend import diag as _diag
except ImportError:                      # ejecutado como script suelto
    import diag as _diag               # type: ignore
from datetime import datetime, timezone
from fastapi import FastAPI, Request
from fastapi.responses import (JSONResponse, FileResponse, PlainTextResponse,
                               RedirectResponse, Response)
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

# ── Compresión de las respuestas (F5.17) ─────────────────────────────────
# El track y el catálogo son GeoJSON/JSON de coordenadas: comprimen ~85-88 %
# (track 8,2 MB → 0,9 MB; catálogo 10,4 MB → 1,7 MB). Sin esto el mapa bajaba
# ~49 MB por carga y tardaba 32 s. Aplica a partir de 1 KB.
from fastapi.middleware.gzip import GZipMiddleware  # noqa: E402
app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.middleware("http")
async def _api_sin_cache(request: Request, call_next):
    """F5.20: las respuestas de DATOS no se cachean en el navegador.

    Sin cabeceras, el navegador puede cachear heurísticamente /api/track y
    servir una versión vieja (el usuario veía el track antiguo tras un cambio
    del filtro). El catálogo se excluye a propósito: usa ETag + no-cache para
    revalidar y no volver a bajar 10 MB.
    """
    resp = await call_next(request)
    p = request.url.path
    if p.startswith("/api/") and p != "/api/catalogo":
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp

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


# ── Actividad del móvil (F5.25) ─────────────────────────────────────────────
# Contrato: `act` ∈ {'still','walk','run','bike','vehicle','tilt'} (o '') y
# `act_conf` = entero 0-100. Valores fuera del contrato o confianza fuera de
# rango se DESCARTAN (se guardan como '' y NULL) — el filtro usará la
# heurística de rumbo + velocidad mediana de siempre.
from backend import geo_filtro as _gf  # noqa: E402


def _act_valida(v):
    """Normaliza `act`: vacío si falta o no está en el contrato."""
    if v is None:
        return ""
    s = str(v).strip().lower()
    return s if s in _gf.ACTS_VALIDAS else ""


def _act_conf_valida(v):
    """Normaliza `act_conf`: entero 0-100, o None si falta/es inválido."""
    if v is None or v == "":
        return None
    try:
        c = int(float(v))
    except (TypeError, ValueError):
        return None
    return c if 0 <= c <= 100 else None


def _campo_punto(body, qp, clave):
    """Valor de un campo del punto en el body JSON o, si falta, en la query."""
    v = body.get(clave)
    if v is None or v == "":
        v = qp.get(clave)
    return v


# ── Contexto del punto: dispositivo + wifi (F5.26) ──────────────────────────
# Contrato FIJO con la APK (igual que act/act_conf de F5.25):
#   dev_id    = identificador ESTABLE del dispositivo (ANDROID_ID), ≤32 car.
#   wifi_ssid = SSID del wifi AL QUE ESTÁ CONECTADO el móvil ('' si no hay).
#   wifi_hue  = huella de las redes wifi VISIBLES, formato EXACTO
#               "<hash8>:<n>" (8 hex + ':' + nº de redes) → ≤24 car.
# Los tres son OPCIONALES: si faltan, vienen con un tipo raro, traen caracteres
# de control o se pasan de largo lo que se guarda es '' — un campo de contexto
# NUNCA puede tumbar la ingesta de un punto.
LARGO_DEV_ID = 32
LARGO_WIFI_SSID = 40
LARGO_WIFI_HUE = 24
_RE_WIFI_HUE = re.compile(r"^[0-9a-f]{8}:[0-9]{1,6}$")


def _texto_punto(v, largo):
    """Sanea un campo de texto del punto: sin controles, sin sobras, truncado.

    Devuelve '' si el valor es None, no es texto/numérico o queda vacío.
    """
    if v is None or isinstance(v, bool):
        return ""
    if isinstance(v, (int, float)):
        s = str(v)
    elif isinstance(v, str):
        s = v
    else:
        return ""  # listas, dicts…: valor absurdo para un campo de texto
    # fuera caracteres de control (\n, \r, \t, \x00…): ni un SSID ni un id de
    # dispositivo los llevan y ensucian la BD y el GeoJSON
    s = "".join(c for c in s if c.isprintable()).strip()
    return s[:largo]


def _dev_id_valido(v):
    """`dev_id`: identificador del dispositivo (≤32 caracteres) o ''."""
    return _texto_punto(v, LARGO_DEV_ID)


def _wifi_ssid_valido(v):
    """`wifi_ssid`: SSID del wifi conectado (≤40 caracteres) o ''."""
    return _texto_punto(v, LARGO_WIFI_SSID)


def _wifi_hue_valida(v):
    """`wifi_hue`: huella "<hash8>:<n>" EXACTA; cualquier otra cosa se descarta.

    El formato lo fija la APK; si no cuadra (otra forma, texto suelto, valor
    gigante) se guarda '' en vez de propagar basura al mapa. El hash se
    normaliza a MINÚSCULAS: es hexadecimal, así `A1B2C3D4:5` y `a1b2c3d4:5`
    son la misma huella y no se pierde el dato por un formato descuidado.
    """
    s = _texto_punto(v, LARGO_WIFI_HUE).lower()
    return s if _RE_WIFI_HUE.match(s) else ""


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
    # Migración F5.25 (IDEMPOTENTE): columnas de ACTIVIDAD del móvil
    # (acelerómetro Android). `act` = 'still'|'walk'|'run'|'bike'|'vehicle'|
    # 'tilt'|'foot' (o '' si se desconoce) y `act_conf` = entero 0-100.
    # Las BD anteriores a F5.25 no las tienen: se añaden si faltan.
    for _col, _tipo in (("act", "TEXT"), ("act_conf", "INTEGER")):
        try:
            con.execute("ALTER TABLE tracks ADD COLUMN %s %s" % (_col, _tipo))
        except Exception:
            pass  # ya existe (migración repetible)
    # Migración F5.26 (IDEMPOTENTE): CONTEXTO del punto GPS — identificador
    # estable del dispositivo (ANDROID_ID) y datos de wifi (SSID al que está
    # conectado + huella de las redes visibles). Son OPCIONALES: los puntos que
    # no las traigan quedan con '' y el comportamiento es el de siempre. Las BD
    # creadas antes de F5.26 no tienen las columnas: se añaden si faltan.
    for _col, _tipo in (("dev_id", "TEXT"), ("wifi_ssid", "TEXT"),
                        ("wifi_hue", "TEXT")):
        try:
            con.execute("ALTER TABLE tracks ADD COLUMN %s %s" % (_col, _tipo))
        except Exception:
            pass  # ya existe (migración repetible)
    con.execute("""CREATE TABLE IF NOT EXISTS eventos(
        user_id TEXT, id TEXT PRIMARY KEY, cam_id TEXT, cam_nombre TEXT,
        lat REAL, lon REAL, ts_inicio REAL, ts_fin REAL, video TEXT,
        n_fotos INTEGER, tam INTEGER, dist_min_m REAL)""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_eventos_user ON eventos(user_id, ts_inicio)")
    # Migración: BD creadas antes de F5.9 no tienen dist_min_m
    try:
        con.execute("ALTER TABLE eventos ADD COLUMN dist_min_m REAL")
    except Exception:
        pass  # ya existe
    # Zonas de no-monitorización (F5.8): círculos donde el usuario NO quiere
    # puntos en el mapa ni eventos/capturas (casa, bar, trabajo…).
    con.execute("""CREATE TABLE IF NOT EXISTS zonas(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        nombre TEXT NOT NULL,
        lat REAL NOT NULL, lon REAL NOT NULL,
        radio_m REAL NOT NULL DEFAULT 30,
        creado REAL)""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_zonas_user ON zonas(user_id)")
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

    # F5.25: /api/punto es un alias de /track (mismo punto + act/act_conf);
    # en modo visión también es escritura → solo lectura.
    @app.api_route("/api/punto", methods=["POST", "PUT", "PATCH", "DELETE"])
    async def _vision_no_punto(request: Request):
        return _solo_lectura()

    @app.api_route("/api/logout", methods=["POST"])
    async def _vision_no_logout(request: Request):
        return _solo_lectura()

    # La gestión de USUARIOS (crear, reset de contraseña, borrar) SÍ se
    # permite en modo visión para rol admin: los handlers reales ya exigen
    # admin, y el mapa está tras Cloudflare Access. Antes estos endpoints
    # respondían "solo lectura" en :8100 y también estaban bloqueados en
    # :8099 (ingesta) → el admin NO podía gestionar usuarios desde ningún
    # sitio. Solo el DELETE de un usuario sigue bloqueado si el objetivo
    # es uno mismo/último admin (lo valida el handler real).

    @app.api_route("/api/evento/{eid}", methods=["DELETE"])
    async def _vision_no_borrar_evento(request: Request, eid: str):
        return _solo_lectura()

    @app.api_route("/api/limpiar_temps", methods=["POST"])
    async def _vision_no_limpiar(request: Request):
        return _solo_lectura()

    @app.api_route("/api/ajustes", methods=["POST"])
    async def _vision_no_ajustes(request: Request):
        # F5.9: en visión se permite cambiar SOLO radio_pasada_m (umbral de
        # "pasada real"): se lee del archivo en cada consulta, no necesita el
        # motor de captura. El resto de ajustes requieren el modo ingesta.
        try:
            u = _auth(request)
            if not u or u["rol"] != "admin":
                return _pedir_auth()
            cambios = await request.json()
        except Exception:
            return _solo_lectura()
        if isinstance(cambios, dict) and cambios.get("radio_pasada_m") is not None:
            try:
                v = float(cambios["radio_pasada_m"])
                v = min(max(v, 5), 500)
                with open(os.path.join(DATA, "ajustes.json"),
                          encoding="utf-8") as f:
                    cfg = json.load(f)
                cfg["radio_pasada_m"] = v
                with open(os.path.join(DATA, "ajustes.json"), "w",
                          encoding="utf-8") as f:
                    json.dump(cfg, f, ensure_ascii=False, indent=2)
                # En visión el panel envía todos los ajustes juntos; solo se
                # aplica el umbral (el resto requiere el motor de la ingesta).
                if set(cambios.keys()) <= {"radio_pasada_m"}:
                    return {"ok": True, "radio_pasada_m": v}
                return {"ok": True, "radio_pasada_m": v,
                        "aviso": "solo se aplicó radio_pasada_m (el resto requiere el modo ingesta)"}
            except Exception:
                return _solo_lectura()
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
    for _p in ("/", "/replay", "/video", "/control", "/static"):
        @app.api_route(_p, methods=["GET", "POST", "PUT", "DELETE"])
        async def _ingesta_no_web(request: Request, _p: str = _p):
            return _solo_ingesta()

    # APIs de lectura/datos → fuera (solo trackcam las sirve)
    for _p in ("/api/track", "/api/ultimo_punto", "/api/eventos", "/api/pasadas",
               "/api/catalogo", "/api/verdes", "/api/cache", "/api/cache/{cam_id}/foto/{n}",
               "/api/temps", "/api/estado", "/api/evento/{eid}/video",
               "/api/evento/{eid}/foto/{n}", "/api/evento/{eid}/metadata",
               "/api/exportar/kml", "/api/exportar/gpx", "/api/exportar/todo",
               "/api/ajustes", "/api/usuarios", "/api/muertas", "/api/zonas",
               "/api/zonas/{zid}"):
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


# ── Zonas de no-monitorización (F5.8) ───────────────────────────────────────
def _zonas_usuario(user_id) -> list:
    """Zonas del usuario: [{id, nombre, lat, lon, radio_m}]."""
    con = get_db()
    try:
        filas = con.execute(
            "SELECT id, nombre, lat, lon, radio_m FROM zonas "
            "WHERE user_id=? ORDER BY id", (int(user_id),)).fetchall()
    finally:
        con.close()
    return [{"id": r[0], "nombre": r[1], "lat": r[2], "lon": r[3],
             "radio_m": r[4]} for r in filas]


def _en_zona(zonas, lat, lon) -> bool:
    """True si (lat,lon) cae dentro de alguna zona (radio ampliado 2 m)."""
    for z in zonas:
        if haversine(lat, lon, z["lat"], z["lon"]) <= z["radio_m"] + 2:
            return True
    return False


def load_camaras():
    global camaras, grid, CAMARAS_CARGADO_TS
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
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
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
    CAMARAS_CARGADO_TS = time.time()
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
    """Cam_id distintos con PASADA REAL en el periodo (para pintar verdes).

    F5.9: un evento cuenta como pasada si la distancia mínima real a la que
    pasaste (dist_min_m, medida por el motor) es <= radio_pasada_m (ajustes,
    defecto 60 m). Los eventos antiguos sin dist_min_m (NULL) se cuentan
    (compatibilidad) hasta que se recalculen.

    F5.9d: cada evento se VERIFICA contra el track real antes de decidir
    (misma red de seguridad que /api/eventos, memoizada) — los verdes nunca
    pueden salir de un dist_min_m corrupto del motor.
    """
    umbral = _umbral_pasada()
    con = get_db()
    q = ("SELECT user_id,id,cam_id,lat,lon,ts_inicio,ts_fin,dist_min_m "
         "FROM eventos WHERE user_id=?")
    params = [str(user_id)]
    if ts_ini:
        q += " AND ts_fin >= ?"; params.append(ts_ini)
    if ts_fin:
        q += " AND ts_inicio <= ?"; params.append(ts_fin)
    filas = con.execute(q, params).fetchall()
    con.close()
    cols = ["user_id", "id", "cam_id", "lat", "lon",
            "ts_inicio", "ts_fin", "dist_min_m"]
    out = set()
    for r in filas:
        d = dict(zip(cols, r))
        dm = _verificar_dist_evento(d)
        if dm is None or dm <= umbral:
            out.add(d["cam_id"])
    return out


def _umbral_pasada() -> float:
    """radio_pasada_m de ajustes.json (default 60). Funciona en ambos modos."""
    try:
        with open(os.path.join(DATA, "ajustes.json"), encoding="utf-8") as f:
            return float(json.load(f).get("radio_pasada_m", 60.0))
    except Exception:
        return 60.0


# F5.9d: eventos ya verificados contra el track real en esta sesión
# (clave (user_id,id) → dist_min_m REAL verificado). Un evento verificado es
# inmutable (su track no cambia), así que se devuelve el valor verificado, NO
# el de la BD — si alguien corrompe la BD después, la API sigue sirviendo la
# verdad hasta reiniciar (y al reiniciar se re-verifica contra el track).
_dist_verif_cache = {}


def _verificar_dist_evento(d):
    """Verifica (y corrige en BD si hace falta) el dist_min_m de UN evento
    contra el track real. Devuelve la distancia REAL (o None sin track).
    Memoizado con el valor verificado: cada evento se comprueba una vez por
    sesión y se devuelve siempre ese valor, no el de la BD."""
    clave = (str(d.get("user_id")), str(d.get("id")))
    if clave in _dist_verif_cache:
        return _dist_verif_cache[clave]
    tsa, tsb = d.get("ts_inicio"), d.get("ts_fin")
    lat, lon = d.get("lat"), d.get("lon")
    if not all(isinstance(x, (int, float)) for x in (tsa, tsb, lat, lon)):
        _dist_verif_cache[clave] = d.get("dist_min_m")
        return _dist_verif_cache[clave]
    real, npts = _dist_min_real(d["user_id"], tsa, tsb, lat, lon)
    dm = d.get("dist_min_m")
    if real is not None:
        if dm is None or abs(real - dm) > 0.6:
            _corregir_dist_evento(d, real)
        dm = real
        d["dist_min_m"] = real
    _dist_verif_cache[clave] = dm
    return dm



def _dist_min_real(uid, tsa, tsb, lat, lon):
    """RED DE SEGURIDAD F5.9d: distancia mínima REAL entre el track crudo del
    usuario y la cámara en la ventana del evento.

    El motor guarda dist_min_m calculado EN VIVO; si cualquier bug de estado
    futuro lo corrompiera (heredado entre pasadas, evento cortado antes de la
    pasada…), lo que el usuario ve debe ser SIEMPRE la verdad: el mínimo de
    haversine entre el track crudo y (lat,lon) dentro de [ts_inicio, ts_fin]
    (con margen ±3 s). Devuelve (dist_real, n_pts) o (None, 0) si no hay track
    en la ventana (evento viejo/reset → se conserva el valor del motor).
    """
    try:
        con = get_db()
        filas = con.execute(
            "SELECT lat, lon FROM tracks WHERE user_id=? AND ts BETWEEN ? AND ?",
            (str(uid), tsa - 3.0, tsb + 3.0)).fetchall()
        con.close()
    except Exception:
        return None, 0
    if not filas:
        return None, 0
    best = None
    for la, lo in filas:
        d = haversine(lat, lon, la, lo)
        if best is None or d < best:
            best = d
    return best, len(filas)


def _corregir_dist_evento(d, real):
    """Persiste la distancia verificada (BD + metadata.json) cuando el valor
    del motor no coincide con el track real. Silencioso y barato (solo se
    llama con discrepancia >0,6 m)."""
    try:
        con = get_db()
        con.execute("UPDATE eventos SET dist_min_m=? WHERE id=? AND user_id=?",
                    (real, d["id"], str(d["user_id"])))
        con.commit()
        con.close()
    except Exception:
        pass
    try:
        mp = os.path.join(DATA, "eventos", str(d["user_id"]),
                          d["id"], "metadata.json")
        if os.path.exists(mp):
            m = json.load(open(mp, encoding="utf-8"))
            if m.get("dist_min_m") != real:
                m["dist_min_m"] = real
                json.dump(m, open(mp, "w", encoding="utf-8"),
                          ensure_ascii=False, indent=2)
    except Exception:
        pass




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
async def _procesar_punto(request: Request):
    """Guarda un punto GPS. Requiere auth (Bearer o ?token=).

    Acepta los campos básicos (lat, lon, ts, acc, vel, dev) y, desde F5.25,
    la ACTIVIDAD del móvil: `act` y `act_conf`; desde F5.26 también el
    CONTEXTO: `dev_id` (ANDROID_ID), `wifi_ssid` (wifi conectado) y `wifi_hue`
    (huella "<hash8>:<n>" de las redes visibles). Todos por body JSON o por
    query string. Los valores fuera del contrato se descartan ('' / NULL).
    """
    u = _auth(request)
    if not u:
        return _pedir_auth()
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    qp = request.query_params
    lat = body.get("lat") or qp.get("lat")
    lon = body.get("lon") or qp.get("lon")
    if lat is None or lon is None:
        return JSONResponse({"error": "faltan lat/lon"}, status_code=400)
    lat, lon = float(lat), float(lon)
    # F5.26: todos los campos del punto se aceptan por body O query string
    # (antes `ts` solo miraba el body: si venía por query se ignoraba y se
    # guardaba la hora del servidor, falseando el punto).
    try:
        ts = float(_campo_punto(body, qp, "ts") or time.time())
    except (TypeError, ValueError):
        ts = time.time()
    acc = float(_campo_punto(body, qp, "acc") or 0)
    vel = float(_campo_punto(body, qp, "vel") or 0)
    dev = str(_campo_punto(body, qp, "dev") or "desconocido")[:40]
    # F5.25 — actividad del móvil (acelerómetro), validada
    act = _act_valida(_campo_punto(body, qp, "act"))
    act_conf = _act_conf_valida(_campo_punto(body, qp, "act_conf"))
    # F5.26 — contexto del punto: dispositivo estable + wifi (opcionales)
    dev_id = _dev_id_valido(_campo_punto(body, qp, "dev_id"))
    wifi_ssid = _wifi_ssid_valido(_campo_punto(body, qp, "wifi_ssid"))
    wifi_hue = _wifi_hue_valida(_campo_punto(body, qp, "wifi_hue"))

    con = get_db()
    # F5.32 — DUPLICADO CONOCIDO: si este punto exacto ya está guardado se
    # responde ok sin tocar nada. Es CRÍTICO que la respuesta sea ok y no un
    # error: la app sólo borra el punto de su cola offline cuando recibe ok, así
    # que un error la deja reenviándolo para siempre (medido el 20-09: el móvil
    # reenvió el MISMO punto de las 02:22 durante horas, 2 de cada 3 peticiones
    # rechazadas, y el usuario lo veía como "fallido, fallido, ok" en pantalla).
    try:
        _ya = con.execute(
            "SELECT 1 FROM tracks WHERE user_id=? AND ts=? AND lat=? AND lon=? LIMIT 1",
            (str(u["id"]), ts, lat, lon)).fetchone()
    except Exception:
        _ya = None
    if _ya:
        con.close()
        return JSONResponse({"ok": True, "duplicado": True, "pts": 0})

    # F5.30/F5.32 — GUARDARRAÍL ANTI-BUCLE. Medido: el 09-10 se llegaron a enviar
    # 53.628 puntos en una hora (uno cada 0,07 s). El ritmo se mide con la hora
    # REAL del servidor y sólo cuentan los puntos con ts fresco: así el vaciado
    # legítimo de la cola offline (ts viejo) NO se bloquea — el diseño anterior
    # medía la ventana del ts del punto y contaba miles de puntos ajenos.
    try:
        _recientes = con.execute(
            "SELECT COUNT(*) FROM tracks WHERE user_id=? AND ts>?",
            (str(u["id"]), time.time() - 60.0)).fetchone()[0]
    except Exception:
        _recientes = 0
    if _recientes > MAX_PTS_MIN:
        print(f"[GUARDARRAIL] RECHAZO user={u['id']!r} puntos_ultimo_minuto={_recientes}", flush=True)
        con.close()
        return JSONResponse(
            {"error": "demasiados puntos por minuto para este usuario",
             "en_el_ultimo_minuto": _recientes, "limite": MAX_PTS_MIN},
            status_code=429)

    # F5.30 — INSERT OR IGNORE: la tabla tiene un índice ÚNICO
    # (user_id, ts, lat, lon). El móvil ha llegado a enviar el mismo punto con el
    # mismo ts hasta 108 veces (medido: 147.448 filas duplicadas = un tercio de
    # la base). Con OR IGNORE el duplicado se descarta en silencio (sin 500) y
    # las estadísticas no se inflan.
    con.execute("INSERT OR IGNORE INTO tracks(user_id,ts,lat,lon,acc,vel,dev,act,act_conf,"
                "dev_id,wifi_ssid,wifi_hue) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(u["id"]), ts, lat, lon, acc, vel, dev, act, act_conf,
                 dev_id, wifi_ssid, wifi_hue))
    con.commit()
    cerca = cams_cerca(lat, lon, 1500)
    n500 = sum(1 for d, _ in cams_cerca(lat, lon, 500))
    n100 = sum(1 for d, _ in cams_cerca(lat, lon, 100))
    con.close()
    return {"ok": True, "user": u["username"], "pts": 1,
            "act": act, "act_conf": act_conf,
            "dev_id": dev_id, "wifi_ssid": wifi_ssid, "wifi_hue": wifi_hue,
            "camaras_1500": len(cerca), "camaras_500": n500, "camaras_100": n100}


@app.post("/track")
async def track(request: Request):
    """APK manda posición (endpoint de ingesta). Requiere auth."""
    return await _procesar_punto(request)


@app.post("/api/punto")
async def api_punto(request: Request):
    """Alias de /track: mismo punto (lat/lon/ts/acc/vel/dev + act/act_conf +
    dev_id/wifi_ssid/wifi_hue)."""
    return await _procesar_punto(request)


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
        # F5.27: tope de cordura. Sin esto las estadísticas del mapa mostraban
        # 1.064 km/h (una distancia de 596 m en 2 s se colaba en la ventana de
        # 5 s). Mismo umbral que el filtro (geo_filtro.VMAX_KMH).
        _v = (d / dt) * 3.6
        if _v > 180.0:
            continue
        vels[i] = _v
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


_TCACHE = {}
# F5.25 — caché del map-matching: la petición al motor tarda ~6 s por traza,
# así que se guarda por (usuario, rango, nº de puntos). Los rangos cerrados no
# cambian, así que la caché es válida sin TTL corto.
# F5.30 — techo de puntos por minuto y usuario (5/s: muy por encima de los 2/s
# normales y del pulso de 1/2 min). Protege contra un móvil en bucle.
MAX_PTS_MIN = 600
_MMCACHE = {}
_MMCACHE_MAX = 24            # clave -> (ult_ts_bd, respuesta, ts_calculo)
_TCACHE_MAX = 16        # entradas (cada una puede ser grande: se limita el nº)
_TCACHE_TTL_VIVO = 4.0  # s de gracia con datos vivos (móvil enviando puntos)


def _tcache_get(clave, ult_ts):
    """Devuelve la respuesta cacheada si los datos NO han cambiado (mismo
    MAX(ts) en BD) — cualquier punto nuevo la invalida.

    Excepción: en los rangos LARGOS (no incrementales) con el móvil enviando
    puntos cada pocos segundos, se reutiliza hasta `_TCACHE_TTL_VIVO` s para no
    recalcular el histórico entero en cada recarga; el rango «24 h / 7 días /
    Todo» no necesita precisión al segundo. En MODO INCREMENTAL (track en vivo,
    clave con incremental=True) NO se aplica gracia: ahí el resultado debe estar
    fresco al segundo para que la línea en vivo no pierda puntos.
    """
    e = _TCACHE.get(clave)
    if e is None:
        return None
    ult_ok = (e[0] == ult_ts and ult_ts is not None)
    if ult_ok:
        return e[1]
    incremental = bool(clave[3]) if len(clave) > 3 else False
    if not incremental and ult_ts is not None and (time.time() - e[2]) < _TCACHE_TTL_VIVO:
        return e[1]
    return None


def _tcache_set(clave, ult_ts, resp):
    if ult_ts is not None:
        if len(_TCACHE) >= _TCACHE_MAX and clave not in _TCACHE:
            try:
                _TCACHE.pop(next(iter(_TCACHE)))   # el más antiguo
            except StopIteration:
                pass
        _TCACHE[clave] = (ult_ts, resp, time.time())
    return resp


def _es_incremental(request: Request) -> bool:
    """¿La consulta de track es el MODO INCREMENTAL (track en vivo)?

    F5.17: solo si `desde` es un ts REAL > 0. El rango «Todo» del mapa manda
    `desde=0`, que como cadena es truthy y colaba como incremental (sin
    colapso ni adelgazado → 150k pts / 31 MB por carga). Con desde=0 o
    ausente se aplica el pipeline COMPLETO.
    """
    d = request.query_params.get("desde")
    if d is None:
        return False
    try:
        return float(d) > 0
    except (TypeError, ValueError):
        return False


def _mapmatch_respuesta(uid_datos, ts_ini, ts_fin, limpios, perfil):
    """Devuelve la FeatureCollection con la línea pegada a la red viaria.

    F5.25 — `limpios` son los puntos ya filtrados (ts, lat, lon, acc, ...).
    Se cachea por (usuario, rango, nº de puntos, perfil) porque cada llamada al
    motor tarda varios segundos. Devuelve None si el motor falla o no hay
    puntos: en ese caso el mapa sigue con su polilínea GPS normal.
    """
    try:
        if not limpios or len(limpios) < 5:
            return None
        clave = (str(uid_datos), round(float(ts_ini or 0), 1),
                 round(float(ts_fin or 0), 1), len(limpios), perfil)
        hit = _MMCACHE.get(clave)
        if hit is not None:
            return hit
        from backend import mapmatch as _mm
        # F5.25b: mismas velocidades que usa el mapa para colorear la ruta, para
        # que la línea ajustada a las calles se pinte con el mismo criterio.
        try:
            vels = _vel_entre([(p[0], p[1], p[2]) for p in limpios])
        except Exception:
            vels = None
        r = _mm.matchear([(p[0], p[1], p[2], p[3] if len(p) > 3 else 0)
                          for p in limpios], perfil=perfil, vels=vels)
        if not r or not r.get("coords"):
            return None
        props = {"mm": True, "perfil": perfil, "n_in": r["n_in"],
                 "n_matcheados": r["n_matcheados"], "dist_m": r["dist_m"],
                 "matchings": r["matchings"], "n_vertices": len(r["coords"]),
                 "n_crudos": len(limpios),
                 "vels": r.get("vels") or []}
        resp = {"type": "FeatureCollection",
                "features": [{"type": "Feature",
                              "geometry": {"type": "LineString",
                                           "coordinates": r["coords"]},
                              "properties": props}],
                "vel_media": None, "filtro": True, "mm": True,
                "properties": props}
        if len(_MMCACHE) >= _MMCACHE_MAX:
            _MMCACHE.clear()
        _MMCACHE[clave] = resp
        return resp
    except Exception as _e:
        # el map-matching NUNCA puede tumbar el mapa, pero el fallo se registra
        # (un except mudo aquí ya nos costó una ronda de depuración)
        try:
            import traceback as _tb
            print("[mm] fallo:", type(_e).__name__, _e, flush=True)
            print("[mm] traza:", _tb.format_exc()[-500:], flush=True)
        except Exception:
            pass
        return None


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
    params = []
    conds = []
    vid = request.query_params.get("usuario") if u["rol"] == "admin" else None
    uid_datos = str(vid) if vid else str(u["id"])
    if vid:
        conds.append("user_id=?"); params.append(vid)
    elif u["rol"] != "admin":
        conds.append("user_id=?"); params.append(str(u["id"]))
    ts_ini = request.query_params.get("desde")
    ts_fin = request.query_params.get("hasta")
    if ts_ini:
        conds.append("ts>=?"); params.append(float(ts_ini))
    if ts_fin:
        conds.append("ts<=?"); params.append(float(ts_fin))

    # Zonas de no-monitorización (F5.8): la BD guarda todo, pero al servir el
    # anti-deriva no deja latidos PARADOS dentro de una zona del usuario
    # (casa/bar) — así la deriva no se pinta. El movimiento real que cruza una
    # zona (sales andando de casa) SÍ se conserva: la línea no se corta.
    # Se resuelven ANTES de la consulta porque entran en la clave de caché.
    try:
        _zonas = _zonas_usuario(u["id"])
    except Exception:
        _zonas = []

    # ── Caché del track servido (F5.17) ────────────────────────────────
    # Guarda el GeoJSON YA CALCULADO y lo reutiliza mientras no entre ningún
    # punto nuevo: la clave incluye user/rango/zonas y la validez se comprueba
    # con MAX(ts) (índice, instantáneo) — en cuanto llega UN punto, el MAX
    # cambia y se recalcula. No sirve datos viejos: solo evita repetir el
    # mismo cálculo con los mismos datos.
    _ck = (uid_datos, ts_ini, ts_fin, _es_incremental(request),
           tuple(sorted((round(float(z.get("lat", 0)), 5),
                         round(float(z.get("lon", 0)), 5),
                         int(z.get("radio_m", 30) or 30)) for z in _zonas)))
    qmax = "SELECT MAX(ts) FROM tracks" + (" WHERE " + " AND ".join(conds) if conds else "")
    ult_ts = con.execute(qmax, params).fetchone()[0]
    _hit = _tcache_get(_ck, ult_ts)
    if _hit is not None:
        con.close()
        return _hit

    q = "SELECT ts,lat,lon,acc,vel,dev,user_id,act,act_conf,dev_id,wifi_hue FROM tracks"
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY ts ASC"
    filas = con.execute(q, params).fetchall()
    con.close()

    # ── Filtros correctores de GPS (lectura) ───────────────────────────
    # La BD guarda TODO (el motor de captura depende de puntos frecuentes),
    # pero al SERVIR el track se limpia: anti-deriva (quita los cientos de
    # puntos que genera el GPS parado en casa/bar) y autocompletado de
    # huecos cortos (pérdida breve de señal mientras te mueves).
    # En modo incremental (?desde=, usado por el "track en vivo" del mapa)
    # solo se aplica anti-deriva: el autocompletado necesita contexto y
    # los puntos nuevos llegan en el siguiente ciclo.
    from backend import geo_filtro as _gf
    crudos = [(r[0], r[1], r[2],
               r[3] if len(r) > 3 else 0,
               r[4] if len(r) > 4 else 0) for r in filas]
    # F5.25 — actividad del móvil indexada por ts. Solo se construye la lista
    # paralela si ALGÚN punto trae actividad; si no, acts=None y el filtro se
    # comporta EXACTAMENTE como antes.
    actos = {}
    for r in filas:
        a = r[7] if len(r) > 7 else ""
        if a:
            actos[r[0]] = (a, r[8] if len(r) > 8 else None)
    acts_par = ([actos.get(c[0]) for c in crudos] if actos else None)
    # F5.26 — contexto del punto (dispositivo/wifi) indexado por ts. Igual que
    # con la actividad: solo si ALGÚN punto lo trae (si no, no entra nada en
    # las properties y el GeoJSON es idéntico al de antes).
    contexto = {}
    for r in filas:
        if len(r) > 10 and (r[9] or r[10]):
            contexto[r[0]] = (r[9] or "", r[10] or "")
    if _es_incremental(request):
        sel = _gf.filtro_anti_deriva(crudos, zonas=_zonas or None, acts=acts_par)
        # F5.17: el rango «Todo» del mapa manda desde=0 (antes se trataba como
        # track en vivo → sin colapso NI adelgazado → 150k pts / 31 MB por
        # carga). Si aún así la serie es enorme, adelgazar para el dibujo.
        if len(sel) > 60000:
            sel = _gf.adelgazar(sel, 3.0)
        # F5.23: unificar paradas (una estancia = 1 punto) también en el modo
        # incremental, que no pasa por limpiar_track()
        sel = _gf.unificar_estancias(sel)
        # F5.25 — map-matching pedido explícitamente (?mm=1). Va AQUÍ también
        # porque un rango con `desde` real entra por el camino incremental y
        # retorna antes de llegar al bloque del modo completo.
        if request.query_params.get("mm") in ("1", "true", "si", "sí"):
            mm_txt = _mapmatch_respuesta(uid_datos, ts_ini, ts_fin, sel,
                                         request.query_params.get("mm_perfil") or "car")
            if mm_txt is not None:
                return mm_txt
        # features directas con sus metadatos
        vels = _vel_entre([(p[0], p[1], p[2]) for p in sel])
        feats = []
        for i, p in enumerate(sel):
            v = vels[i] if i < len(vels) else None
            props = {"ts": p[0], "acc": p[3], "vel": p[4], "v": v,
                     "modo": _modo_vel(v), "dev": "", "user_id": u["id"]}
            _a = actos.get(p[0])
            if _a:
                props["act"] = _a[0]
                if _a[1] is not None:
                    props["act_conf"] = _a[1]
            # F5.26: dev_id y wifi_hue solo si el punto los trae (el ssid NO se
            # expone en el mapa; se guarda en BD para uso interno)
            _c = contexto.get(p[0])
            if _c:
                if _c[0]:
                    props["dev_id"] = _c[0]
                if _c[1]:
                    props["wifi_hue"] = _c[1]
            feats.append({"type": "Feature",
                          "geometry": {"type": "Point",
                                       "coordinates": [p[2], p[1]]},
                          "properties": props})
        # último ts REAL de la BD (aunque el filtro lo descarte): así el
        # frontend avanza su marca de agua sin re-pedir puntos ya vistos
        ult_db = filas[-1][0] if filas else None
        return _tcache_set(_ck, ult_ts, {"type": "FeatureCollection", "features": feats,
                                         "vel_media": None, "filtro": True,
                                         "ultimo_ts_db": ult_db})
    # modo completo: anti-deriva (con zonas) + colapso + autocompletar.
    # Construimos un índice ts→fila original para conservar acc/vel/dev.
    por_ts = {r[0]: r for r in filas}
    limpios = _gf.limpiar_track(crudos, zonas=_zonas or None, acts=acts_par)
    pts = [(p[0], p[1], p[2]) for p in limpios]
    vels = _vel_entre(pts)
    feats = []
    for i, (ts, lat, lon) in enumerate(pts):
        orig = por_ts.get(ts)
        v = vels[i] if i < len(vels) else None
        props = {"ts": ts,
                 "acc": orig[3] if orig else 0,
                 "vel": orig[4] if orig else 0,
                 "v": v,
                 "modo": _modo_vel(v),
                 "dev": orig[5] if orig and len(orig) > 5 else "",
                 "user_id": orig[6] if orig and len(orig) > 6 else u["id"]}
        # F5.25: la actividad viaja en properties solo si el punto la trae
        if orig and len(orig) > 7 and orig[7]:
            props["act"] = orig[7]
            if orig[8] is not None:
                props["act_conf"] = orig[8]
        # F5.26: contexto del punto (dev_id / wifi_hue) solo si existe; el
        # wifi_ssid NO se expone (no se pide siquiera en el SELECT)
        if orig and len(orig) > 10:
            if orig[9]:
                props["dev_id"] = orig[9]
            if orig[10]:
                props["wifi_hue"] = orig[10]
        feats.append({"type": "Feature",
                      "geometry": {"type": "Point",
                                   "coordinates": [lon, lat]},
                      "properties": props})
    # F5.25 — map-matching opcional (?mm=1): la línea pegada a la red viaria.
    # Los giros se reconstruyen por la CALZADA real en vez de por los puntos
    # sueltos del GPS. Si el motor no está disponible se devuelve el track
    # normal (el mapa nunca se rompe por esto).
    if request.query_params.get("mm") in ("1", "true", "si", "sí"):
        mm_txt = _mapmatch_respuesta(uid_datos, ts_ini, ts_fin, limpios,
                                     request.query_params.get("mm_perfil") or "car")
        if mm_txt is not None:
            return mm_txt
    return _tcache_set(_ck, ult_ts, {"type": "FeatureCollection", "features": feats,
                                     "vel_media": None, "filtro": True,
                                     "n_crudos": len(crudos)})


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


@app.get("/api/dias")
def api_dias(request: Request):
    """F5.12: días que tienen track grabado (para el selector de día de /video).
    Devuelve [{dia:'YYYY-MM-DD', pts:N, desde:ts, hasta:ts}] ordenado desc.
    Usa la zona horaria local del servidor (CEST/CET)."""
    u = _auth(request)
    if not u:
        return _pedir_auth()
    con = get_db()
    conds, params = [], []
    if u["rol"] == "admin":
        vid = request.query_params.get("usuario")
        if vid:
            conds.append("user_id=?"); params.append(vid)
    else:
        conds.append("user_id=?"); params.append(str(u["id"]))
    q = ("SELECT date(ts,'unixepoch','localtime') AS dia, COUNT(*) AS n,"
         " MIN(ts) AS d0, MAX(ts) AS d1 FROM tracks")
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " GROUP BY dia ORDER BY dia DESC"
    filas = con.execute(q, params).fetchall()
    con.close()
    # F5.12: además del rango del TRACK (d0/d1) devolver el DÍA CIVIL completo
    # (dia0 00:00 local → dia1 23:59:59 local) para pedir los EVENTOS por día:
    # filtrar eventos por [d0,d1] del track corta los que se cierran con el post
    # de +60 s después del último punto (ts_fin > d1) → faltaban eventos.
    import datetime as _dt
    def _dia_civil(epoch_medio):
        base = _dt.datetime.fromtimestamp(epoch_medio).replace(hour=0, minute=0, second=0, microsecond=0)
        fin = base + _dt.timedelta(days=1, seconds=-1)
        return base.timestamp(), fin.timestamp()
    out = []
    for r in filas:
        d0c, d1c = _dia_civil((r[2] + r[3]) / 2.0)
        out.append({"dia": r[0], "pts": r[1], "desde": r[2], "hasta": r[3],
                    "dia0": d0c, "dia1": d1c})
    return {"dias": out}


# ── API: eventos ───────────────────────────────────────────────────────────
@app.get("/api/eventos")
def api_eventos(request: Request):
    u = _auth(request)
    if not u:
        return _pedir_auth()
    con = get_db()
    q = ("SELECT user_id,id,cam_id,cam_nombre,lat,lon,ts_inicio,ts_fin,video,"
         "n_fotos,tam,dist_min_m FROM eventos")
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
            "ts_inicio", "ts_fin", "video", "n_fotos", "tam", "dist_min_m"]
    out = []
    _umbral = _umbral_pasada()
    for r in filas:
        d = dict(zip(cols, r))
        # F5.9d RED DE SEGURIDAD: el dist_min_m que ve el usuario sale del
        # TRACK REAL, no solo del valor que guardó el motor en vivo. Si el
        # motor se equivocara (bug de estado), aquí se corrige al servir
        # (y se persiste en BD + metadata.json vía _corregir_dist_evento).
        dm = _verificar_dist_evento(d)
        # F5.9: ¿cuenta como pasada real? (distancia mínima <= umbral)
        d["es_pasada"] = (dm is None) or (dm <= _umbral)
        # Enriquecer con ts_entrada/ts_salida/foto_ts desde metadata.json
        # (cuando existe — eventos creados por el motor moderno). Así el
        # frontend puede marcar las fotos del tramo exacto de la pasada.
        try:
            mp = os.path.join(DATA, "eventos", str(d["user_id"]),
                              d["id"], "metadata.json")
            if os.path.exists(mp):
                m = json.load(open(mp, encoding="utf-8"))
                d["ts_entrada"] = m.get("ts_entrada")
                d["ts_salida"] = m.get("ts_salida")
                d["foto_ts"] = m.get("foto_ts")
        except Exception:
            pass
        out.append(d)
    return out


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


CAMARAS_CARGADO_TS = 0.0


@app.get("/api/catalogo/recargar")
def api_catalogo_recargar(request: Request):
    """Recarga el catálogo desde el JSON de EuroCams SIN reiniciar.

    Solo admin y solo en modo visión (el mapa). Devuelve cuántas cámaras
    había y cuántas hay tras recargar (útil tras actualizar EuroCams).
    """
    u = _auth(request)
    if not u:
        return _pedir_auth()
    if u["rol"] != "admin":
        return JSONResponse({"error": "requiere rol admin"}, status_code=403)
    if not MODO_VISION:
        return JSONResponse({"error": "solo en modo visión"}, status_code=403)
    antes = len(camaras)
    n = load_camaras()
    return {"antes": antes, "ahora": n, "fuente": EUROCAMS_JSON}


_CAT_LISTA_CLAVE = None
_CAT_LISTA_DATOS = None


def _cat_lista():
    """Lista de cámaras normalizada (id/nombre/lat/lon/fuente/pais/url),
    construida UNA vez por carga del catálogo (no por petición)."""
    global _CAT_LISTA_CLAVE, _CAT_LISTA_DATOS
    clave = (len(camaras), CAMARAS_CARGADO_TS)
    if _CAT_LISTA_CLAVE != clave or _CAT_LISTA_DATOS is None:
        out = []
        for c in camaras:
            cid = c.get("id") or "%s_%.5f_%.5f" % (c.get("fuente", "cam"),
                                                   c["lat"], c["lon"])
            out.append({"id": cid, "nombre": c.get("nombre", "?"),
                        "lat": c["lat"], "lon": c["lon"],
                        "fuente": c.get("fuente", "?"),
                        "pais": c.get("pais", "?"),
                        "url": c.get("url", "")})
        _CAT_LISTA_CLAVE, _CAT_LISTA_DATOS = clave, out
    return _CAT_LISTA_DATOS


def _ambito_usuario(uid, margen_km=25.0):
    """BBox (minlat,minlon,maxlat,maxlon) que cubre los tracks del usuario +
    margen — la idea del usuario: «solo de Vizcaya si no he salido de
    Vizcaya, o de España si he salido». Devuelve None si no hay tracks."""
    try:
        con = get_db()
        r = con.execute("SELECT MIN(lat),MIN(lon),MAX(lat),MAX(lon) FROM tracks"
                        " WHERE user_id=?", (str(uid),)).fetchone()
        con.close()
    except Exception:
        return None
    if not r or r[0] is None:
        return None
    la0, lo0, la1, lo1 = float(r[0]), float(r[1]), float(r[2]), float(r[3])
    dla = margen_km / 111.0
    clo = max(0.2, math.cos(math.radians((la0 + la1) / 2.0)))
    dlo = margen_km / (111.0 * clo)
    return (la0 - dla, lo0 - dlo, la1 + dla, lo1 + dlo)


@app.get("/api/catalogo")
def api_catalogo(request: Request):
    """Cámaras para pintar en el mapa. Autenticado.

    Parámetros (opcionales):
      ?ambito=1     → solo las cámaras del ÁMBITO del usuario (bbox de sus
                      tracks + 25 km): si no ha salido de Vizcaya, solo Vizcaya
                      (10 MB → unos cientos de KB). Devuelve `bbox` usado.
      ?bbox=a,b,c,d → solo las cámaras de ese bbox (minlat,minlon,maxlat,maxlon).
      Sin parámetros → catálogo completo (42.928).

    F5.17: el catálogo solo cambia al recargar el JSON de EuroCams → la lista se
    construye UNA vez y la respuesta se sirve con ETag + `Cache-Control:
    no-cache`: el navegador la guarda y revalida con If-None-Match → 304 sin
    cuerpo (10,4 MB → 0 B) mientras no cambie nada.
    """
    u = _auth(request)
    if not u:
        return _pedir_auth()
    vid = request.query_params.get("usuario") if u["rol"] == "admin" else None
    uid_datos = str(vid) if vid else str(u["id"])
    lista = _cat_lista()
    bbox = None
    if request.query_params.get("ambito"):
        bbox = _ambito_usuario(uid_datos, 25.0)
    q_bbox = request.query_params.get("bbox")
    if q_bbox:
        try:
            b = [float(x) for x in q_bbox.split(",")]
            if len(b) == 4:
                bbox = tuple(b) if bbox is None else (
                    min(bbox[0], b[0]), min(bbox[1], b[1]),
                    max(bbox[2], b[2]), max(bbox[3], b[3]))
        except ValueError:
            pass
    if bbox:
        la0, lo0, la1, lo1 = bbox
        sel = [c for c in lista
               if la0 <= c["lat"] <= la1 and lo0 <= c["lon"] <= lo1]
    else:
        sel = lista
    cuerpo = json.dumps({"total": len(sel), "total_catalogo": len(lista),
                         "camaras": sel, "fuente": EUROCAMS_JSON,
                         "cargado_ts": CAMARAS_CARGADO_TS,
                         "bbox": list(bbox) if bbox else None,
                         "ambito": bool(bbox)},
                        separators=(",", ":"), ensure_ascii=False, default=str)
    etag = '"%s"' % hashlib.md5(cuerpo.encode()).hexdigest()[:20]
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag,
                                                  "Cache-Control": "no-cache"})
    return Response(cuerpo, media_type="application/json",
                    headers={"ETag": etag, "Cache-Control": "no-cache"})


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


@app.get("/api/muertas")
def api_muertas(request: Request):
    """Cámaras detectadas como MUERTAS al intentar capturar.

    El motor registra una cámara cuando una pasada se descarta por
    placeholder real de la fuente (imagen de error fija) o cuando la
    descarga falla repetidamente (3+). Devuelve con coordenadas resueltas
    para pintarlas ⚪ gris en el mapa. Estado global (no por usuario).
    """
    u = _auth(request)
    if not u:
        return _pedir_auth()
    try:
        with open(os.path.join(DATA, "muertas.json"), encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    # Resolver contra el catálogo (igual que _cams_geo_verdes)
    by_id = {}
    for c in camaras:
        cid = str(c.get("id") or "%s_%.5f_%.5f" % (c.get("fuente", "cam"),
                                                    c["lat"], c["lon"]))
        by_id[cid] = c
    out = []
    for cid, info in raw.items():
        c = by_id.get(cid)
        if not c:
            continue  # ya no está en el catálogo → no se pinta
        out.append({
            "cam_id": cid,
            "nombre": c.get("nombre", "?"),
            "lat": c["lat"], "lon": c["lon"],
            "fuente": c.get("fuente", "?"),
            "ts": info.get("ts") if isinstance(info, dict) else None,
            "motivo": info.get("motivo") if isinstance(info, dict) else "?",
        })
    out.sort(key=lambda x: -(x["ts"] or 0))
    return {"total": len(out), "muertas": out}


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
    san2orig = {}   # sanitizado → cam_id ORIGINAL (con puntos): para que el
    # clic en un 🟠 naranja busque las pasadas con el id real de la BD
    for c in camaras:
        cid = str(c.get("id") or "%s_%.5f_%.5f" % (c.get("fuente", "cam"),
                                                    c["lat"], c["lon"]))
        by_id[cid] = c
        san = sanitizar_dir(cid)
        by_id.setdefault(san, c)
        san2orig.setdefault(san, cid)
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
            cid_real = san2orig.get(cdir, cdir)   # id original (con puntos)
            out.append({
                "cam_id": cid_real,
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
    # F5.29 — estado del CONTEXTO del móvil en las últimas 24 h: el mapa avisa
    # si los puntos no traen actividad (permiso denegado en MIUI) o wifi, porque
    # sin esa señal el servidor tiene que adivinar con el GPS.
    _uid = str(vid) if (u["rol"] == "admin" and vid) else (
        None if u["rol"] == "admin" else str(u["id"]))
    _hace24 = time.time() - 86400
    if _uid:
        _p24 = con.execute("SELECT COUNT(*) FROM tracks WHERE user_id=? AND ts>?",
                           (_uid, _hace24)).fetchone()[0]
        _a24 = con.execute(
            "SELECT COUNT(*) FROM tracks WHERE user_id=? AND ts>? AND act<>''",
            (_uid, _hace24)).fetchone()[0]
        _w24 = con.execute(
            "SELECT COUNT(*) FROM tracks WHERE user_id=? AND ts>? AND wifi_hue<>''",
            (_uid, _hace24)).fetchone()[0]
    else:
        _p24 = con.execute("SELECT COUNT(*) FROM tracks WHERE ts>?",
                           (_hace24,)).fetchone()[0]
        _a24 = con.execute("SELECT COUNT(*) FROM tracks WHERE ts>? AND act<>''",
                           (_hace24,)).fetchone()[0]
        _w24 = con.execute("SELECT COUNT(*) FROM tracks WHERE ts>? AND wifi_hue<>''",
                           (_hace24,)).fetchone()[0]
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
        "usuarios_trackeando": est["usuarios_trackeando"],    "puntos_24h": _p24, "act_24h": _a24, "wifi_24h": _w24,
}


@app.get("/api/diag")
def api_diag(request: Request):
    """Diagnóstico remoto del track en vivo (para debug de cortes).

    Muestra por usuario: último punto recibido (hace cuántos segundos),
    dispositivos, y los CORTES recientes (huecos > 60 s entre puntos
    consecutivos) con su duración y posición — permite ver en remoto si la
    APK deja de enviar (cortes cíclicos de ~10 min = sistema Android
    congelando el servicio) o si es pérdida puntual de GPS.
    """
    u = _auth(request)
    if not u:
        return _pedir_auth()
    if u["rol"] != "admin":
        return JSONResponse({"error": "requiere rol admin"}, status_code=403)
    con = get_db()
    ahora = time.time()
    out = {}
    for uid in con.execute("SELECT DISTINCT user_id FROM tracks").fetchall():
        uid = uid[0]
        filas = con.execute(
            "SELECT ts,lat,lon,dev FROM tracks WHERE user_id=? "
            "ORDER BY ts DESC LIMIT 20000", (uid,)).fetchall()
        filas = filas[::-1]  # cronológico
        if not filas:
            continue
        devs = {}
        for r in filas:
            devs[r[3]] = devs.get(r[3], 0) + 1
        cortes = []
        for i in range(1, len(filas)):
            dt = filas[i][0] - filas[i - 1][0]
            if dt > 60:
                cortes.append({
                    "desde": round(filas[i - 1][0], 1),
                    "hasta": round(filas[i][0], 1),
                    "duracion_s": round(dt, 1),
                    "lat": round(filas[i][1], 5),
                    "lon": round(filas[i][2], 5),
                })
        ult = filas[-1]
        out[str(uid)] = {
            "puntos_ultimas_24h": len(filas),
            "dispositivos": devs,
            "ultimo_punto_hace_s": round(ahora - ult[0], 1),
            "ultimo_punto": {"ts": round(ult[0], 1), "lat": round(ult[1], 5),
                             "lon": round(ult[2], 5)},
            "cortes_60s_ultimas_24h": len(cortes),
            "cortes_muestra": cortes[-15:],
        }
    con.close()
    return {"ahora": round(ahora, 1), "usuarios": out}


# ── Web estática ────────────────────────────────────────────────────────────
WEB = os.path.join(BASE, "web")
# Los estáticos solo se montan en visión (trackcam, tras Cloudflare Access).
# En ingesta (track) las páginas ya están bloqueadas arriba → no hace falta.
if MODO_VISION:
    app.mount("/static", StaticFiles(directory=WEB), name="static")


_SIN_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}


@app.get("/")
def index():
    # F5.16: las páginas HTML siempre frescas — el navegador cacheaba el JS
    # inline y tras cada cambio se seguía ejecutando la versión antigua
    # (el usuario veía comportamiento viejo sin saber por qué).
    return FileResponse(os.path.join(WEB, "index.html"), headers=_SIN_CACHE)


@app.get("/replay")
def replay():
    """F5.11d: /replay era la página vieja; /video es la herramienta nueva.
    Redirige a /video (la URL antigua que el usuario conoce)."""
    return RedirectResponse("/video", status_code=302)


@app.get("/video")
def video():
    """F5.12: reproductor de ruta profesional en página propia (pantalla
    completa, estilo app): selector de día, slider del recorrido, cuadrícula
    de cámaras con distancia en vivo. Misma sesión que el mapa (tc_auth_v1)."""
    return FileResponse(os.path.join(WEB, "video.html"), headers=_SIN_CACHE)


@app.get("/control")
def control():
    """Panel de control remoto: config de la app (OTA sin recompilar)."""
    return FileResponse(os.path.join(WEB, "control.html"), headers=_SIN_CACHE)


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
APK_VERSION_CODE = 14
APK_VERSION_NAME = "1.16"

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


# ── Zonas de no-monitorización (F5.8) ────────────────────────────────────────
@app.get("/api/zonas")
def api_zonas_list(request: Request):
    """Zonas del usuario (o de ?usuario=N si admin)."""
    u = _auth(request)
    if not u:
        return _pedir_auth()
    if u["rol"] == "admin":
        vid = request.query_params.get("usuario")
        uid = vid if vid else u["id"]
    else:
        uid = u["id"]
    return {"zonas": _zonas_usuario(uid)}


# ─────────────────── F5.36 DIAGNÓSTICO (panel 🩺) ───────────────────
# Comprueba TODOS los parámetros del sistema (servicios, red, túneles, móvil,
# ingesta, motor, datos, ajuste a calles) y devuelve problemas accionables.
# Va en hilos aparte porque cada chequeo lanza comandos externos: si bloqueara
# el bucle de eventos, el panel dejaría de responder mientras se comprueba.

@app.get("/api/diagnostico")
async def api_diag(request: Request):
    """Informe de diagnóstico completo. Solo admin."""
    u = _auth(request)
    if not u:
        return _pedir_auth()
    if u["rol"] != "admin":
        return JSONResponse({"error": "requiere rol admin"}, status_code=403)
    informe = await asyncio.to_thread(_diag.diagnosticar)
    return JSONResponse(informe)


@app.post("/api/diagnostico/accion")
async def api_diag_accion(request: Request):
    """Ejecuta UNA acción de reparación de la lista blanca. Solo admin."""
    u = _auth(request)
    if not u:
        return _pedir_auth()
    if u["rol"] != "admin":
        return JSONResponse({"error": "requiere rol admin"}, status_code=403)
    try:
        b = await request.json()
    except Exception:
        return JSONResponse({"error": "JSON inválido"}, status_code=400)
    accion = str(b.get("accion") or "")
    parametro = str(b.get("parametro") or "")
    if accion not in _diag.ACCIONES_PERMITIDAS:
        return JSONResponse({"error": f"acción no permitida: {accion}"}, status_code=400)
    r = await asyncio.to_thread(_diag.ejecutar_accion, accion, parametro)
    return JSONResponse(r)


@app.post("/api/diagnostico/reparar")
async def api_diag_reparar(request: Request):
    """Ejecuta las reparaciones automáticas de todo lo que esté mal. Solo admin.

    Solo corre acciones de la lista blanca y sin parámetros libres: reinicia
    servicios vigilados, limpia duplicados y rehace comprobaciones. Nada que
    venga del cliente se ejecuta tal cual.
    """
    u = _auth(request)
    if not u:
        return _pedir_auth()
    if u["rol"] != "admin":
        return JSONResponse({"error": "requiere rol admin"}, status_code=403)
    informe = await asyncio.to_thread(_diag.diagnosticar)
    hechas = []
    for pr in informe.get("problemas", []):
        acc = pr.get("accion")
        if not acc:
            continue
        partes = acc.split(":", 1)
        r = await asyncio.to_thread(_diag.ejecutar_accion,
                                    partes[0], partes[1] if len(partes) > 1 else "")
        hechas.append({"problema": pr["titulo"], "accion": acc,
                       "ok": r.get("ok"), "salida": r.get("salida")})
    return JSONResponse({"reparaciones": hechas, "total": len(hechas)})


@app.post("/api/zonas")
async def api_zonas_crear(request: Request):
    """Crea una zona: {nombre, lat, lon, radio_m}. El dueño o un admin.

    Se permite también en VISIÓN (el mapa web crea zonas desde aquí y la BD
    es compartida con la ingesta): el motor de la ingesta refresca su caché
    de zonas en ≤10 s (TTL). Cloudflare Access + auth protegen el endpoint.
    """
    u = _auth(request)
    if not u:
        return _pedir_auth()
    try:
        b = await request.json()
    except Exception:
        return JSONResponse({"error": "JSON inválido"}, status_code=400)
    nombre = str(b.get("nombre") or "").strip()[:60]
    try:
        lat, lon = float(b["lat"]), float(b["lon"])
        radio = float(b.get("radio_m") or 30)
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"error": "lat/lon/radio_m inválidos"}, status_code=400)
    if not nombre:
        return JSONResponse({"error": "nombre obligatorio"}, status_code=400)
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return JSONResponse({"error": "coordenadas fuera de rango"}, status_code=400)
    radio = min(max(radio, 5), 5000)
    con = get_db()
    try:
        cur = con.execute(
            "INSERT INTO zonas(user_id, nombre, lat, lon, radio_m, creado) "
            "VALUES(?,?,?,?,?,?)", (u["id"], nombre, lat, lon, radio,
                                    time.time()))
        zid = cur.lastrowid
        con.commit()
    finally:
        con.close()
    # El motor refresca su caché en ≤10 s; invalidamos aquí para que sea inmediato
    try:
        motor._zonas_cache.pop(str(u["id"]), None)
        motor._en_zona_user.pop(str(u["id"]), None)
    except Exception:
        pass
    return {"ok": True, "id": zid}


@app.put("/api/zonas/{zid}")
async def api_zonas_editar(zid: int, request: Request):
    """Edita una zona (nombre/radio). Solo su dueño o un admin."""
    u = _auth(request)
    if not u:
        return _pedir_auth()
    try:
        b = await request.json()
    except Exception:
        return JSONResponse({"error": "JSON inválido"}, status_code=400)
    con = get_db()
    try:
        fila = con.execute("SELECT user_id FROM zonas WHERE id=?",
                           (zid,)).fetchone()
        if not fila:
            return JSONResponse({"error": "no existe"}, status_code=404)
        if u["rol"] != "admin" and int(fila[0]) != int(u["id"]):
            return JSONResponse({"error": "no autorizado"}, status_code=403)
        campos, params = [], []
        if "nombre" in b:
            nombre = str(b["nombre"]).strip()[:60]
            if not nombre:
                return JSONResponse({"error": "nombre obligatorio"},
                                    status_code=400)
            campos.append("nombre=?"); params.append(nombre)
        if "radio_m" in b:
            try:
                radio = min(max(float(b["radio_m"]), 5), 5000)
            except (TypeError, ValueError):
                return JSONResponse({"error": "radio_m inválido"},
                                    status_code=400)
            campos.append("radio_m=?"); params.append(radio)
        if "lat" in b and "lon" in b:
            try:
                lat, lon = float(b["lat"]), float(b["lon"])
            except (TypeError, ValueError):
                return JSONResponse({"error": "lat/lon inválidos"},
                                    status_code=400)
            if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
                return JSONResponse({"error": "coordenadas fuera de rango"},
                                    status_code=400)
            campos.append("lat=?"); campos.append("lon=?")
            params += [lat, lon]
        if not campos:
            return JSONResponse({"error": "sin cambios"}, status_code=400)
        params.append(zid)
        con.execute("UPDATE zonas SET %s WHERE id=?" % ", ".join(campos),
                    params)
        con.commit()
    finally:
        con.close()
    try:
        motor._zonas_cache.pop(str(fila[0]), None)
        motor._en_zona_user.pop(str(fila[0]), None)
    except Exception:
        pass
    return {"ok": True}


@app.delete("/api/zonas/{zid}")
def api_zonas_borrar(zid: int, request: Request):
    """Borra la zona zid (solo su dueño o un admin). También en visión."""
    u = _auth(request)
    if not u:
        return _pedir_auth()
    con = get_db()
    try:
        fila = con.execute("SELECT user_id FROM zonas WHERE id=?",
                           (zid,)).fetchone()
        if not fila:
            return JSONResponse({"error": "no existe"}, status_code=404)
        if u["rol"] != "admin" and int(fila[0]) != int(u["id"]):
            return JSONResponse({"error": "no autorizado"}, status_code=403)
        con.execute("DELETE FROM zonas WHERE id=?", (zid,))
        con.commit()
    finally:
        con.close()
    try:
        motor._zonas_cache.pop(str(fila[0]), None)
        motor._en_zona_user.pop(str(fila[0]), None)
    except Exception:
        pass
    return {"ok": True}
