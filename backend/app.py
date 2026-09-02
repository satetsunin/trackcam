#!/usr/bin/env python3
"""TrackCam backend — receptor de posición + índice geo + API web.
Fase 1: /track (APK manda lat/lon), /api/track (GeoJSON), /api/estado.
Fase 2 (siguiente): captura 2 s + eventos ffmpeg.
"""
import os, sqlite3, math, json, time, threading
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

os.makedirs(DATA, exist_ok=True)
app = FastAPI(title="TrackCam")

# ── BD ──────────────────────────────────────────────────────────────────────
def get_db():
    con = sqlite3.connect(DB, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    return con

def init_db():
    con = get_db()
    con.execute("""CREATE TABLE IF NOT EXISTS tracks(
        ts REAL, lat REAL, lon REAL, acc REAL, vel REAL, dev TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS eventos(
        id TEXT PRIMARY KEY, cam_id TEXT, cam_nombre TEXT, lat REAL, lon REAL,
        ts_inicio REAL, ts_fin REAL, video TEXT, n_fotos INTEGER, tam INTEGER)""")
    con.commit(); con.close()

init_db()

# ── Índice geo (grid hash sobre la BD de EuroCams) ─────────────────────────
CELL = 0.02  # ~2 km
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
    """Devuelve [(distancia, camara)] dentro del radio (radio en metros)."""
    gx, gy = int(lon/CELL), int(lat/CELL)
    span = int(radio / (CELL * 111000.0)) + 1  # CELL en grados ≈ 111 km/grado
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

# ── API ─────────────────────────────────────────────────────────────────────
@app.post("/track")
async def track(request: Request):
    """APK manda: {lat, lon, ts?, acc?, vel?, dev?} (JSON) o query params."""
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
    con.execute("INSERT INTO tracks(ts,lat,lon,acc,vel,dev) VALUES(?,?,?,?,?,?)",
                (ts, lat, lon, acc, vel, dev))
    con.commit()
    # cámaras en 1,5 km (contexto para el debug/estado)
    cerca = cams_cerca(lat, lon, 1500)
    n500 = sum(1 for d, _ in cams_cerca(lat, lon, 500))
    n100 = sum(1 for d, _ in cams_cerca(lat, lon, 100))
    con.close()
    return {"ok": True, "pts": 1, "camaras_1500": len(cerca), "camaras_500": n500, "camaras_100": n100}

@app.get("/api/track")
def api_track():
    """GeoJSON de todos los puntos del track."""
    con = get_db()
    rows = con.execute("SELECT ts,lat,lon,acc,vel,dev FROM tracks ORDER BY ts").fetchall()
    con.close()
    feats = [{"type": "Feature",
              "geometry": {"type": "Point", "coordinates": [r[2], r[1]]},
              "properties": {"ts": r[0], "acc": r[3], "vel": r[4], "dev": r[5]}}
             for r in rows]
    return {"type": "FeatureCollection", "features": feats}

@app.get("/api/eventos")
def api_eventos():
    con = get_db()
    rows = con.execute("SELECT * FROM eventos ORDER BY ts_inicio").fetchall()
    con.close()
    cols = ["id","cam_id","cam_nombre","lat","lon","ts_inicio","ts_fin","video","n_fotos","tam"]
    return [dict(zip(cols, r)) for r in rows]

@app.get("/api/temps")
def api_temps():
    """Estado de los buffers temporales de captura (Fase 2)."""
    buffers = motor.buffers_info()
    return {
        "buffers": buffers,
        "total_camaras": len(buffers),
        "total_fotos": sum(b["n_fotos"] for b in buffers),
        "total_tam": sum(b["tam"] for b in buffers),
    }

@app.get("/api/estado")
def api_estado():
    con = get_db()
    n = con.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    n_ev = con.execute("SELECT COUNT(*) FROM eventos").fetchone()[0]
    con.close()
    # tamaño de datos
    def tam(p):
        t = 0
        for root, _, files in os.walk(p):
            t += sum(os.path.getsize(os.path.join(root, f)) for f in files)
        return t
    return {
        "camaras_cargadas": len(camaras),
        "puntos_track": n,
        "eventos": n_ev,
        "camaras_activas": motor.estado_actual()["camaras_activas"],
        "camaras_capturando": motor.estado_actual()["camaras_capturando"],
        "descargas_ok": motor.estado_actual()["descargas_ok"],
        "descargas_fallo": motor.estado_actual()["descargas_fallo"],
        "tam_tracks_db": os.path.getsize(DB) if os.path.exists(DB) else 0,
        "tam_temps": tam(os.path.join(DATA, "temps")) if os.path.exists(os.path.join(DATA, "temps")) else 0,
        "tam_eventos": tam(os.path.join(DATA, "eventos")) if os.path.exists(os.path.join(DATA, "eventos")) else 0,
        "cuota_eventos_gb": motor.cfg["cuota_eventos_gb"],
        "cuota_temps_mb": motor.cfg["cuota_temps_mb"],
        "ultimo_punto": None,
    }

# ── Web estática ────────────────────────────────────────────────────────────
WEB = os.path.join(BASE, "web")
app.mount("/static", StaticFiles(directory=WEB), name="static")

@app.get("/")
def index():
    return FileResponse(os.path.join(WEB, "index.html"))

@app.get("/replay")
def replay():
    """Modo video track: reproduce la ruta y muestra los vídeos de los eventos."""
    return FileResponse(os.path.join(WEB, "replay.html"))

# ── Eventos: servir vídeo/fotos + borrado controlado (F2) ─────────────────
@app.get("/api/evento/{eid}/video")
def evento_video(eid: str):
    p = os.path.join(DATA, "eventos", eid, "video.mp4")
    if not os.path.exists(p):
        return JSONResponse({"error": "no existe"}, status_code=404)
    return FileResponse(p, media_type="video/mp4")

@app.get("/api/evento/{eid}/foto/{n}")
def evento_foto(eid: str, n: str):
    p = os.path.join(DATA, "eventos", eid, n)
    if not os.path.exists(p):
        return JSONResponse({"error": "no existe"}, status_code=404)
    return FileResponse(p)

@app.delete("/api/evento/{eid}")
def evento_borrar(eid: str):
    """Borra un evento (carpeta + registro BD). Solo IDs con formato hash corto."""
    if not eid or any(c not in "0123456789abcdef" for c in eid) or len(eid) > 16:
        return JSONResponse({"error": "id inválido"}, status_code=400)
    import shutil
    carpeta = os.path.join(DATA, "eventos", eid)
    if os.path.exists(carpeta):
        shutil.rmtree(carpeta)
    con = get_db()
    con.execute("DELETE FROM eventos WHERE id=?", (eid,))
    con.commit(); con.close()
    return {"ok": True, "borrado": eid}

@app.post("/api/limpiar_temps")
def limpiar_temps():
    """Borra todos los buffers temporales (fotos de cámaras a ≤500 m sin evento)."""
    import shutil
    carpeta = os.path.join(DATA, "temps")
    tam0 = 0
    if os.path.exists(carpeta):
        for root, _, files in os.walk(carpeta):
            tam0 += sum(os.path.getsize(os.path.join(root, f)) for f in files)
        shutil.rmtree(carpeta)
    return {"ok": True, "borrados_mb": round(tam0 / 1e6, 2)}

# ── Ajustes configurables (F2) ──────────────────────────────────────────
@app.get("/api/ajustes")
def api_ajustes():
    """Configuración actual del motor (radios, ventanas, cuotas...)."""
    return motor.cfg

@app.post("/api/ajustes")
async def api_ajustes_set(request: Request):
    """Actualiza configuración en caliente. Acepta JSON parcial."""
    try:
        cambios = await request.json()
    except Exception:
        cambios = {}
    if not isinstance(cambios, dict):
        return JSONResponse({"error": "JSON inválido"}, status_code=400)
    return motor.actualizar_cfg(cambios)

load_camaras()

# ── Motor de captura (Fase 2) ─────────────────────────────────────────────
from backend.captura import MotorCaptura

motor = MotorCaptura(
    db_path=DB,
    data_dir=DATA,
    get_db_fn=get_db,
    cams_cerca_fn=cams_cerca,
)
motor.start()
