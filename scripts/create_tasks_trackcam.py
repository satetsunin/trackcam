"""Crea la lista de tareas TrackCam en Nextcloud CalDAV con jerarquía RELATED-TO."""
import os, sys, uuid
from datetime import datetime, timezone
from icalendar import Calendar, Todo
from caldav import DAVClient, Todo as CDTodo

URL = os.getenv("NEXTCLOUD_CALDAV_URL", "https://nextcloud.satetsunin.com/remote.php/dav/calendars/admin/")
USER = os.getenv("NEXTCLOUD_USER", "admin")
PASS = os.getenv("NEXTCLOUD_PASS", "")
if not PASS:
    sys.exit("Falta NEXTCLOUD_PASS")
client = DAVClient(url=URL, username=USER, password=PASS)
principal = client.principal()

cal = None
for c in principal.calendars():
    try:
        name = c.get_display_name()
    except Exception:
        name = c.name
    if c.id == "trackcam" or "TrackCam" in str(name):
        cal = c
        break
if cal is None:
    cal = principal.make_calendar(name="\U0001F3A5 TrackCam", cal_id="trackcam")
print("CALENDAR:", cal.url)

def make_ical(summary, desc="", prio=1, parent_uid=None, completed=False):
    t = Todo()
    t.add("uid", str(uuid.uuid4()))
    t.add("summary", summary)
    if desc:
        t.add("description", desc)
    t.add("priority", prio)
    t.add("status", "COMPLETED" if completed else "NEEDS-ACTION")
    if completed:
        t.add("completed", datetime.now(timezone.utc))
        t.add("percent-complete", 100)
    if parent_uid:
        t.add("related-to", parent_uid)
    c = Calendar()
    c.add("prodid", "-//Hermes//TrackCam//ES")
    c.add("version", "2.0")
    c.add_component(t)
    return c.to_ical()

def add_task(summary, desc="", prio=1, parent_uid=None, completed=False):
    obj = CDTodo(parent=cal, data=make_ical(summary, desc, prio, parent_uid, completed))
    obj.save()
    return obj.id

# F0 — decisiones consensuadas (completada)
t_f0 = add_task("📋 TrackCam — Plan v1.0 consensuado (nombre, túnel CF, captura 2s servidor, Redmi)",
                "Radios 1,5km/500m/100m · solo BD EuroCams · APK propia · túnel Cloudflare.", completed=True)

# F1 — MVP
t_f1 = add_task("🚧 F1 — MVP: receptor /track + grid + captura 2s + eventos ffmpeg + mapa")
add_task("Receptor FastAPI /track + SQLite (puntos), grid hash 45.759 cams", parent_uid=t_f1)
add_task("Motor de captura: ring buffers 2 s por cámara en 500 m (proxy EuroCams)", parent_uid=t_f1)
add_task("Eventos: regla 100 m (20 s antes + 40 s después) → ffmpeg MP4 + imágenes", parent_uid=t_f1)
add_task("Mapa web Leaflet: track + marcadores de evento + reproductor", parent_uid=t_f1)
add_task("Prueba simulada: recorrido por Bilbao con cámaras live", parent_uid=t_f1)

# F2 — almacenamiento
t_f2 = add_task("🗄️ F2 — Control de almacenamiento y ajustes")
add_task("Panel: temps vs guardado, cuotas (eventos 20GB/temps 500MB), descarga, borrado", parent_uid=t_f2)
add_task("Ajustes: intervalos de captura, radios, retención", parent_uid=t_f2)
add_task("Reproductor de vídeo por evento (browser)", parent_uid=t_f2)

# F3 — APK
t_f3 = add_task("📱 F3 — APK Android (mitigación Redmi desde el inicio)")
add_task("Foreground service + notificación + START_STICKY + autostart tras reinicio", parent_uid=t_f3)
add_task("Exención de batería Redmi: guía + intent a Ajustes de batería", parent_uid=t_f3)
add_task("Intervalo GPS configurable (1 s ruta / reposo) + envío por túnel Cloudflare", parent_uid=t_f3)
add_task("Modo video track: reproducir ruta con vídeos de los eventos", parent_uid=t_f3)

# F4 — pulido
add_task("✨ F4 — Pulido: exportaciones, multi-día, notificaciones")

# Túnel
t_tun = add_task("🌐 Túnel Cloudflare track.satetsunin.com → 127.0.0.1:8099")
add_task("Crear túnel + token en Cloudflare (dashboard)", parent_uid=t_tun)
add_task("Servicio systemd user cloudflared-trackcam + .env token", parent_uid=t_tun)

print("Tareas creadas. Listando...")
for t in cal.todos(sort_key="summary", include_completed=True):
    print("-", t.id[:8], "|", t.icalendar_component.get("summary", ""))
