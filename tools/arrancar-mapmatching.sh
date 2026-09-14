#!/usr/bin/env bash
# =============================================================================
# arrancar-mapmatching.sh
# -----------------------------------------------------------------------------
# Levanta un servidor LOCAL de GraphHopper para map-matching (endpoint /match)
# reutilizando el grafo YA construido de eurocams/routing.
#
#   - NO reconstruye el grafo: reutiliza graph-cache/ tal cual.
#     (Reconstruirlo desde spain-latest.osm.pbf tarda horas; este script aborta
#      antes de intentarlo salvo que se pase --reconstruir de forma explícita.)
#   - Genera su propio config en runtime/ partiendo del config.yml existente,
#     cambiando solo las rutas (a absolutas) y el puerto. El config.yml original
#     no se modifica.
#   - Busca un puerto libre a partir del solicitado (8098 por defecto).
#     Nunca usa 8080 (Open WebUI), 8099 ni 8100 (TrackCam).
#
# USO
#   ./arrancar-mapmatching.sh                 # primer plano, puerto 8098 (o el siguiente libre)
#   ./arrancar-mapmatching.sh --port 8091     # primer plano en 8091
#   ./arrancar-mapmatching.sh --daemon        # segundo plano (pid + log en runtime/)
#   ./arrancar-mapmatching.sh --status        # ¿está arrancado?
#   ./arrancar-mapmatching.sh --stop          # detener el de --daemon
#   ./arrancar-mapmatching.sh --reconstruir   # (PELIGRO) reconstruye el grafo: horas
#
# ENDPOINT DE MAP-MATCHING (POST, cuerpo GPX o JSON):
#   curl -X POST "http://127.0.0.1:<PUERTO>/match?profile=car&points_encoded=false&gps_accuracy=80" \
#        -H 'Content-Type: application/gpx+xml' --data-binary @traza.gpx
#   IMPORTANTE: en trazas reales hay que mandar gps_accuracy alto (60-120).
#   Sin él (valor por defecto) el matching se dispara (geometría en zigzag,
#   hasta 18x la distancia real). Ver tools/README-mapmatching.md.
# =============================================================================
set -euo pipefail

# --- Rutas del material ya existente -----------------------------------------
GH_HOME=${GH_HOME:-/home/alvaro/Escritorio/proyectos/eurocams/routing}
JAR="$GH_HOME/graphhopper-web.jar"
GRAPH_CACHE="$GH_HOME/graph-cache"
PBF="$GH_HOME/spain-latest.osm.pbf"
CONFIG_BASE="$GH_HOME/config.yml"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME="$HERE/runtime"
CONFIG="$RUNTIME/mapmatching-config.yml"
LOG="$RUNTIME/graphhopper.log"
PIDFILE="$RUNTIME/graphhopper.pid"

# --- Parámetros ---------------------------------------------------------------
PORT_PREF=${PORT:-8098}
ADMIN_OFFSET=1000            # puerto admin = puerto + 1000
RESERVADOS=(8080 8099 8100)  # Open WebUI / TrackCam
JAVA=${JAVA:-java}
HEAP=${HEAP:-4g}             # el grafo de España necesita RAM; 4g va sobrado
TIMEOUT_ARRANQUE=${TIMEOUT_ARRANQUE:-180}

MODO="primer-plano"
RECONSTRUIR=0
while [ $# -gt 0 ]; do
  case "$1" in
    --daemon)      MODO="daemon" ;;
    --stop)        MODO="stop" ;;
    --status)      MODO="status" ;;
    --reconstruir) RECONSTRUIR=1 ;;
    --port)        PORT_PREF="$2"; shift ;;
    --port=*)      PORT_PREF="${1#*=}" ;;
    -h|--help)     sed -n '2,32p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "Opción desconocida: $1 (usa --help)" >&2; exit 2 ;;
  esac
  shift
done

log()  { printf '\033[1;36m[mapmatching]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[mapmatching]\033[0m %s\n' "$*" >&2; }
die()  { err "$*"; exit 1; }

# --- Utilidades ---------------------------------------------------------------

# ¿Alguien escucha ya en ese puerto?
puerto_ocupado() {
  local p=$1
  ss -H -tln "sport = :$p" 2>/dev/null | grep -q . && return 0
  return 1
}

es_reservado() {
  local p=$1 r
  for r in "${RESERVADOS[@]}"; do [ "$p" = "$r" ] && return 0; done
  return 1
}

# Primer puerto libre desde PORT_PREF hacia arriba (saltando reservados)
elegir_puerto() {
  local p=$PORT_PREF intentos=0
  while [ $intentos -lt 40 ]; do
    if ! es_reservado "$p" && ! puerto_ocupado "$p"; then echo "$p"; return 0; fi
    log "puerto $p ocupado o reservado, probando $((p+1))" >&2
    p=$((p+1)); intentos=$((intentos+1))
  done
  return 1
}

pid_vivo() {
  [ -f "$PIDFILE" ] || return 1
  local pid; pid=$(cat "$PIDFILE" 2>/dev/null || true)
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

# --- Acciones que no arrancan nada -------------------------------------------

if [ "$MODO" = "stop" ]; then
  if pid_vivo; then
    pid=$(cat "$PIDFILE"); log "deteniendo GraphHopper (pid $pid)..."
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 30); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    kill -0 "$pid" 2>/dev/null && { err "no termina, enviando SIGKILL"; kill -9 "$pid" 2>/dev/null || true; }
    rm -f "$PIDFILE"; log "detenido."
  else
    log "no hay ningún GraphHopper arrancado con este script."
  fi
  exit 0
fi

if [ "$MODO" = "status" ]; then
  if pid_vivo; then
    log "en marcha, pid $(cat "$PIDFILE")"
    grep -m1 'port:' "$CONFIG" 2>/dev/null || true
  else
    log "parado."
  fi
  exit 0
fi

# --- Comprobaciones previas ---------------------------------------------------

[ -f "$JAR" ] || die "no existe $JAR"
[ -f "$CONFIG_BASE" ] || die "no existe $CONFIG_BASE"

if [ ! -f "$GRAPH_CACHE/properties" ]; then
  err "no encuentro un grafo construido en $GRAPH_CACHE (falta el fichero 'properties')."
  err "Arrancar aquí implicaría reimportar $PBF, lo que puede tardar HORAS."
  if [ "$RECONSTRUIR" -ne 1 ]; then
    die "abortado. Si de verdad quieres reconstruirlo: $0 --reconstruir"
  fi
  log "AVISO: --reconstruir activado, se reimportará el PBF completo (horas)."
else
  log "grafo existente detectado en $GRAPH_CACHE (no se reconstruye)."
  [ -f "$PBF" ] || log "AVISO: no está $PBF (con el grafo ya construido no hace falta)."
fi

mkdir -p "$RUNTIME"

# --- Puerto y config ----------------------------------------------------------

PUERTO=$(elegir_puerto) || die "no encontré puerto libre desde $PORT_PREF"
ADMIN=$((PUERTO + ADMIN_OFFSET))
while puerto_ocupado "$ADMIN"; do ADMIN=$((ADMIN+1)); done
[ "$PUERTO" != "$PORT_PREF" ] && log "el puerto $PORT_PREF estaba ocupado; uso $PUERTO"

# Config propio: mismos valores que config.yml (rutas absolutas) + puertos.
# GraphHopper 11 exige que 'server' sea un objeto Dropwizard con
# application_connectors/admin_connectors (el bloque en lista NO se parsea).
cat > "$CONFIG" <<YML
# Generado automáticamente por arrancar-mapmatching.sh -- NO editar a mano.
# Base: $CONFIG_BASE
graphhopper:
  datareader.file: $PBF
  graph.location: $GRAPH_CACHE
  routing.timeout: 60
  import.osm.ignored_highways: ''
  graph.encoded_values: max_speed,road_class,road_environment,road_access,car_access,car_average_speed
  profiles:
    - name: car
      custom_model_files: [car.json]
server:
  application_connectors:
    - type: http
      bind_host: 127.0.0.1
      port: $PUERTO
  admin_connectors:
    - type: http
      bind_host: 127.0.0.1
      port: $ADMIN
YML
log "config: $CONFIG"

esperar_listo() {
  local i
  for i in $(seq 1 "$TIMEOUT_ARRANQUE"); do
    if curl -sf -m 2 "http://127.0.0.1:$PUERTO/info" >/dev/null 2>&1; then return 0; fi
    if [ -f "$PIDFILE" ] && ! pid_vivo; then return 1; fi
    sleep 1
  done
  return 1
}

CMD=("$JAVA" "-Xmx$HEAP" -jar "$JAR" server "$CONFIG")

mostrar_ayuda_final() {
  echo
  log "listo:  HTTP http://127.0.0.1:$PUERTO  (admin $ADMIN)"
  log "  GET  http://127.0.0.1:$PUERTO/info"
  log "  POST http://127.0.0.1:$PUERTO/match   (Content-Type: application/gpx+xml | application/json)"
  echo
  log "Ejemplo real (traza de 46 km, usuario 1):"
  cat <<EJ
  curl -s -X POST "http://127.0.0.1:$PUERTO/match?profile=car&points_encoded=false&gps_accuracy=80" \\
       -H 'Content-Type: application/gpx+xml' \\
       --data-binary @$HERE/traza-2026-09-13.gpx -o $HERE/match-response.json -w 'HTTP %{http_code} %{size_download} bytes\\n'
EJ
}

# --- Arranque -----------------------------------------------------------------

if [ "$MODO" = "daemon" ]; then
  if pid_vivo; then die "ya hay uno en marcha (pid $(cat "$PIDFILE")); usa --stop"; fi
  log "arrancando en segundo plano... log: $LOG"
  nohup "${CMD[@]}" >"$LOG" 2>&1 &
  echo $! > "$PIDFILE"
  if esperar_listo; then
    mostrar_ayuda_final
    log "para pararlo: $0 --stop"
  else
    err "no arrancó; últimas líneas de $LOG:"
    tail -n 20 "$LOG" >&2 || true
    rm -f "$PIDFILE"
    exit 1
  fi
else
  log "arrancando en primer plano (Ctrl+C para parar)..."
  mostrar_ayuda_final
  exec "${CMD[@]}"
fi
