#!/usr/bin/env python3
# Genera Plan-TrackCam-v1.0.pdf con gráficas (matplotlib + weasyprint)
import matplotlib; matplotlib.use('Agg')
import matplotlib.pyplot as plt
import os, base64, subprocess

BASE = "/home/alvaro/Escritorio/proyectos/trackcam"
CHARTS = os.path.join(BASE, "docs", "charts")
os.makedirs(CHARTS, exist_ok=True)
plt.rcParams['font.family'] = 'DejaVu Sans'

def save(fig, name):
    fig.savefig(os.path.join(CHARTS, name), dpi=180, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    print("chart:", name)

# ── Gráfica 1: Arquitectura (diagrama de cajas) ─────────────────────────────
fig, ax = plt.subplots(figsize=(10, 5.2)); ax.axis('off')
def box(x, y, w, h, text, fc='#eef4fb', ec='#2b6cb0', fs=10):
    ax.add_patch(plt.Rectangle((x, y), w, h, fc=fc, ec=ec, lw=1.5, zorder=3))
    ax.text(x+w/2, y+h/2, text, ha='center', va='center', fontsize=fs, zorder=4)
def arrow(x1, y1, x2, y2, label=None):
    ax.annotate('', xy=(x2, y2), xytext=(x1, y1),
                arrowprops=dict(arrowstyle='-|>', color='#c05621', lw=2), zorder=2)
    if label: ax.text((x1+x2)/2, (y1+y2)/2+0.04, label, ha='center', fontsize=8, color='#c05621')

box(0.02, 0.62, 0.26, 0.26, 'App Android\n(GPS 1 s · 24 h)\nforeground service', fc='#fef3e2', ec='#c05621')
box(0.02, 0.16, 0.26, 0.26, 'Túnel Tailscale\nWireGuard\n(100.102.145.58:8099)', fc='#e6fffa', ec='#2c7a7b')
box(0.44, 0.62, 0.26, 0.26, 'Receptor FastAPI\n/track → SQLite\n86.400 pts/día', fc='#eef4fb', ec='#2b6cb0')
box(0.44, 0.16, 0.26, 0.26, 'Motor de proximidad\ngrid hash 45.758 cams\n1,5 km · 500 m · 100 m', fc='#eef4fb', ec='#2b6cb0')
box(0.82, 0.62, 0.16, 0.26, 'ffmpeg\nJPEG→MP4\n2 fps', fc='#f0fff4', ec='#38a169')
box(0.82, 0.16, 0.16, 0.26, 'Web UI :8099\nmapa + vídeos\n+ almacenamiento', fc='#f0fff4', ec='#38a169')
arrow(0.28, 0.75, 0.44, 0.75, '1 s')
arrow(0.28, 0.29, 0.44, 0.29)
arrow(0.70, 0.75, 0.82, 0.75, 'evento')
arrow(0.70, 0.29, 0.82, 0.29, 'datos')
ax.set_xlim(0, 1); ax.set_ylim(0, 1)
ax.set_title('Arquitectura TrackCam — todo corre en el Victus (32 GB RAM · 427 GB libres)',
             fontsize=12, fontweight='bold', pad=10)
save(fig, 'arquitectura.png')

# ── Gráfica 2: Lógica de radios y ventana de captura ─────────────────────────
fig, ax = plt.subplots(figsize=(10, 4.6))
ax.set_xlim(-1.8, 1.8); ax.set_ylim(-1.8, 1.8); ax.set_aspect('equal'); ax.axis('off')
circle = plt.Circle((0, 0), 1.5, fc='#ebf8ff', ec='#3182ce', lw=2, alpha=0.5)
ax.add_patch(circle)
circle2 = plt.Circle((0, 0), 0.5, fc='#c6f6d5', ec='#38a169', lw=2, alpha=0.6)
ax.add_patch(circle2)
circle3 = plt.Circle((0, 0), 0.1, fc='#fed7d7', ec='#e53e3e', lw=2, alpha=0.8)
ax.add_patch(circle3)
ax.plot(0, 0, 'ko', ms=6)
ax.text(0, 1.28, '1,5 km — cámara "activa"\n(1 snapshot de contexto)', ha='center', fontsize=9, color='#2b6cb0')
ax.text(0, 0.62, '500 m — captura continua\ncada 2 s (ring buffer temp/)', ha='center', fontsize=9, color='#276749')
ax.text(0.16, 0.02, '100 m — EVENTO\nse conserva la ventana', ha='center', fontsize=9, color='#c53030')
ax.text(0, -1.62, 'Ventana del evento: 20 s antes + 40 s después (≈30 frames → MP4 ~2,5 MB)',
        ha='center', fontsize=9, style='italic')
ax.set_title('Radios de captura por proximidad (configurables)', fontsize=12, fontweight='bold')
save(fig, 'radios.png')

# ── Gráfica 3: Almacenamiento estimado ───────────────────────────────────────
fig, ax = plt.subplots(figsize=(10, 4.6))
cat = ['Track\ndiario', 'Evento\ntípico', '1 h ciudad\ndensa (30 ev)', 'Disco\nlibre']
val = [6, 5.5, 165, 427000]
colores = ['#3182ce', '#38a169', '#e53e3e', '#2b6cb0']
bars = ax.bar(cat, val, color=colores, width=0.6)
ax.set_yscale('log'); ax.set_ylabel('MB (escala log)')
for b, v in zip(bars, val):
    ax.text(b.get_x()+b.get_width()/2, v*1.15, f'{v:,} MB', ha='center', fontsize=10, fontweight='bold')
ax.set_title('Estimación de almacenamiento', fontsize=12, fontweight='bold')
ax.grid(axis='y', alpha=0.3)
save(fig, 'almacenamiento.png')

# ── HTML + PDF ───────────────────────────────────────────────────────────────
html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
@page {{ size: A4; margin: 2cm 2cm 2.5cm 2cm;
  @bottom-center {{ content: "TrackCam · Plan v1.0 · Página " counter(page); font-size: 8pt; color: #999; }} }}
@page :first {{ margin: 0; @bottom-center {{ content: none; }} }}
body {{ font-family: 'DejaVu Sans'; font-size: 10pt; color: #222; line-height: 1.45; }}
h1 {{ color: #1a365d; font-size: 20pt; page-break-before: always; margin-top: 0; }}
h2 {{ color: #2b6cb0; font-size: 13pt; border-bottom: 2px solid #2b6cb0; padding-bottom: 3px; }}
table {{ border-collapse: collapse; width: 100%; margin: 8px 0; font-size: 8.5pt; }}
th, td {{ border: 1px solid #cbd5e0; padding: 4px 6px; text-align: left; }}
th {{ background: #eef4fb; }}
img {{ max-width: 100%; margin: 8px 0; }}
.cover {{ width: 21cm; height: 29.7cm; background: linear-gradient(150deg, #1a365d, #2b6cb0); color: white;
  display: block; position: relative; }}
.cover-inner {{ padding: 4cm 2.5cm; }}
.cover h1 {{ color: white; font-size: 34pt; margin-top: 60pt; page-break-before: auto; }}
.cover .sub {{ font-size: 14pt; color: #bee3f8; margin-top: 14pt; }}
.cover .tag {{ font-size: 10pt; color: #90cdf4; margin-top: 60pt; }}
ul {{ margin: 4px 0 8px 0; }}
li {{ margin: 2px 0; }}
.page-break {{ page-break-before: always; }}
</style></head><body>

<div class="cover"><div class="cover-inner">
<div style="font-size:12pt; color:#bee3f8;">EuroCams · Proyecto complementario</div>
<h1>TrackCam</h1>
<div class="sub">Sistema de trackeo con captura automática de cámaras<br/>por proximidad (vídeo + imágenes + track en mapa)</div>
<div class="tag">Plan de viabilidad v1.0 · Septiembre 2026 · Documento técnico interno<br/>Generado por Hermes Agent · Datos de estimación propios del proyecto</div>
</div></div>

<h1>1 · Resumen ejecutivo</h1>
<p>TrackCam registra la ubicación del usuario cada segundo desde el móvil (enviada por túnel seguro),
cruza esa posición con el catálogo de <b>45.758 cámaras georreferenciadas</b> de EuroCams y captura
imágenes según la proximidad. Cuando el usuario pasa a menos de 100 m de una cámara se genera un
<b>evento</b>: un vídeo (montado con ffmpeg) más las imágenes originales, con un marcador clicable
en el track. Todo se almacena en el ordenador, se consulta y descarga desde el navegador, con control
del almacenamiento (temporales vs. guardado).</p>
<p><b>Viabilidad: ALTA.</b> Todos los componentes ya existen o están instalados (ffmpeg ✓, Tailscale ✓,
FastAPI ✓, Leaflet ✓, catálogo de cámaras ✓, 427 GB libres ✓). El único riesgo real es el comportamiento
de las ROMs Android agresivas con la app en segundo plano, mitigable con foreground service + exención
de batería.</p>

<h1>2 · Requisitos y solución</h1>
<table>
<tr><th>#</th><th>Petición</th><th>Solución</th></tr>
<tr><td>1</td><td>Ubicación cada segundo</td><td>App Android foreground → POST Tailscale → FastAPI :8099</td></tr>
<tr><td>2</td><td>Cámaras en radio 1,5 km "activas"</td><td>Grid hash sobre 45.758 cams → 1 snapshot de contexto</td></tr>
<tr><td>3</td><td>A &lt;500 m captura cada 2 s</td><td>Hilos de captura con ring buffer temporal (temp/)</td></tr>
<tr><td>4</td><td>Guardar solo si paso a &lt;100 m</td><td>Regla de compromiso: se conserva al entrar en 100 m</td></tr>
<tr><td>5</td><td>Conservar 20 s antes + 40 s después</td><td>Ring buffer 60 s por cámara; recorte de ventana al salir</td></tr>
<tr><td>6</td><td>Cada cámara = un evento</td><td>ffmpeg JPEG→MP4 + carpeta de imágenes + marcador en el track</td></tr>
<tr><td>7</td><td>Cámaras fijas (1 foto/5 min)</td><td>2 capturas bastan (regla especial)</td></tr>
<tr><td>8</td><td>Almacenar en el ordenador + track</td><td>SQLite + GeoJSON → Leaflet; eventos en disco</td></tr>
<tr><td>9</td><td>Ver/descargar desde navegador + control</td><td>Web UI: mapa, eventos, panel de almacenamiento (cuotas, borrado)</td></tr>
<tr><td>10</td><td>App 24 h, auto-relanzamiento, intervalo configurable</td><td>APK Kotlin: foreground + START_STICKY + exención batería (fallback: GPSLogger FOSS)</td></tr>
<tr><td>11</td><td>Envío por túnel</td><td>Tailscale (instalado). Fallback: túnel Cloudflare con token</td></tr>
<tr><td>12</td><td>"Video track": reproducir la ruta con vídeos</td><td>Modo reproducción: animación del punto; al llegar a un evento se reproduce su vídeo</td></tr>
</table>

<h1>3 · Arquitectura</h1>
<img src="charts/arquitectura.png">
<p>El flujo completo es: <b>móvil → túnel → receptor → proximidad → captura → evento → web</b>.
Las cámaras con protección hotlink se sirven a través del proxy de EuroCams (Referer por fuente).</p>

<h1>4 · Lógica de radios</h1>
<img src="charts/radios.png">
<ul>
<li><b>1,5 km</b>: la cámara queda "activa" — 1 snapshot de contexto (archivo temporal).</li>
<li><b>500 m</b>: captura continua cada 2 s a un ring buffer (máx. 12 hilos concurrentes, prioridad por distancia).</li>
<li><b>100 m</b>: evento — se conserva la ventana (20 s antes + 40 s después) y se monta el vídeo.</li>
<li>Cámaras live-capable (geobilbao, 1 s de refresco) capturan a 2 s; las fijas (5 min) con 2 capturas.</li>
<li>Si el usuario no llega a los 100 m, los temporales se borran (poda automática, tope 500 MB).</li>
</ul>

<h1>5 · Almacenamiento estimado</h1>
<img src="charts/almacenamiento.png">
<table>
<tr><th>Componente</th><th>Tamaño</th><th>Política</th></tr>
<tr><td>Track (1 pts/s)</td><td>~6 MB/día</td><td>Poda a 200 MB (≈1 mes)</td></tr>
<tr><td>Evento típico</td><td>~5,5 MB (30 fotos + MP4)</td><td>Cuota 20 GB por defecto</td></tr>
<tr><td>Temporales (ring buffers)</td><td>acotado</td><td>Tope 500 MB, poda automática</td></tr>
<tr><td>1 h ciudad densa (peor caso)</td><td>~165 MB</td><td>—</td></tr>
</table>
<p>Disco libre: 427 GB → margen de miles de horas de conducción urbana. Cuotas configurables.</p>

<h1>6 · Fases de implementación</h1>
<table>
<tr><th>Fase</th><th>Contenido</th><th>Esfuerzo</th></tr>
<tr><td><b>F0</b></td><td>Consenso del plan, repo, Nextcloud, tareas CalDAV</td><td>hoy</td></tr>
<tr><td><b>F1 · MVP</b></td><td>Receptor /track + SQLite + grid hash + motor de captura con ring buffers + eventos ffmpeg + mapa web. Cliente: GPSLogger sobre Tailscale</td><td>~1 día</td></tr>
<tr><td><b>F2</b></td><td>Panel de almacenamiento (temp/guardado, cuotas, descarga, borrado), ajustes (intervalos/radios/retención), reproductor de vídeo</td><td>~1 día</td></tr>
<tr><td><b>F3</b></td><td>APK Kotlin (24 h, auto-restart, intervalo configurable) + modo video track (reproducir ruta con vídeos)</td><td>~2-3 días</td></tr>
<tr><td><b>F4</b></td><td>Pulido: exportaciones, multi-día, notificaciones</td><td>según demanda</td></tr>
</table>

<h1>7 · Riesgos y mitigaciones</h1>
<table>
<tr><th>Riesgo</th><th>Impacto</th><th>Mitigación</th></tr>
<tr><td>ROMs Android matan la app (Xiaomi/Huawei…)</td><td>Alto</td><td>Foreground service + guía exención batería + START_STICKY. Riesgo nº 1, atacado en F3</td></tr>
<tr><td>Carga a servidores de cámaras</td><td>Medio</td><td>Tope de hilos, 2 s solo live, fijas a 2 capturas, ring buffers acotados</td></tr>
<tr><td>URLs de cámaras muertas</td><td>Medio</td><td>Chequeo de salud existente; eventos usan las que responden</td></tr>
<tr><td>GPS en túneles/interiores</td><td>Bajo</td><td>Filtro de puntos (velocidad/accuracy), fusión con red</td></tr>
<tr><td>Móvil fuera del tailnet</td><td>Bajo</td><td>Sin datos (aceptado); alternativa túnel CF</td></tr>
<tr><td>Crecimiento de disco</td><td>Bajo</td><td>Cuotas + poda + panel de control</td></tr>
</table>

<h1>8 · Preguntas abiertas</h1>
<ul>
<li><b>Nombre</b>: TrackCam (propuesto) / RutaCam / CamRoute / otro.</li>
<li><b>Cliente</b>: ¿GPSLogger como arranque rápido en F1 y APK propio en F3 (recomendado), o APK desde el principio?</li>
<li><b>Túnel</b>: ¿Tailscale (recomendado, ya instalado) o Cloudflare tunnel?</li>
<li><b>Cuotas</b>: ¿eventos 20 GB / temps 500 MB / track 200 MB por defecto?</li>
</ul>

</body></html>"""

with open(os.path.join(BASE, "docs", "plan.html"), "w") as f:
    f.write(html)

from weasyprint import HTML
pdf = os.path.join(BASE, "docs", "Plan-TrackCam-v1.0.pdf")
HTML(os.path.join(BASE, "docs", "plan.html")).write_pdf(pdf)
print("PDF:", pdf, os.path.getsize(pdf), "bytes")
subprocess.run(["pdfinfo", pdf], capture_output=True, text=True)
print("paginas:", subprocess.run(["pdfinfo", pdf], capture_output=True, text=True).stdout.split("Pages:")[1].split("\n")[0].strip())
