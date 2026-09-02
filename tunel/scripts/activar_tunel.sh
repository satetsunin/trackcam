#!/usr/bin/env bash
# =============================================================================
# activar_tunel.sh — Activa el túnel Cloudflare de TrackCam
#   https://track.satetsunin.com → http://127.0.0.1:8099
#
# Uso:   bash scripts/activar_tunel.sh        (desde tunel/)  o
#        bash tunel/scripts/activar_tunel.sh  (desde la raíz del proyecto)
#
# SIN sudo: es un servicio systemd de usuario.
#
# Qué hace:
#   1. Comprueba que cloudflared existe y que el token NO es el placeholder.
#   2. Copia el .service a ~/.config/systemd/user/ (patrón de webcast/indice).
#   3. systemctl --user daemon-reload + enable --now (o restart si ya activo).
#   4. Verifica con curl GET https://track.satetsunin.com/api/estado → HTTP 200.
# =============================================================================
set -euo pipefail

# --- Rutas -------------------------------------------------------------------
PROYECTO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SERVICE_TEMPLATE="$PROYECTO/tunel/cloudflared-trackcam.service"
SERVICE_DEST="$HOME/.config/systemd/user/cloudflared-trackcam.service"
ENV_FILE="$HOME/.cloudflared/trackcam.env"
CLOUDFLARED_BIN="$HOME/.local/bin/cloudflared"
URL="https://track.satetsunin.com/api/estado"
PLACEHOLDER="PON_AQUI_EL_TOKEN_DEL_TUNEL_CF"

echo "==> Túnel Cloudflare TrackCam  ($URL)"

# 0) Nada de root: los servicios de usuario se gestionan como el propio usuario
if [ "$(id -u)" -eq 0 ]; then
    echo "ERROR: ejecuta SIN sudo (es un servicio systemd de usuario)." >&2
    exit 1
fi

# 1) Pre-requisitos
if [ ! -x "$CLOUDFLARED_BIN" ]; then
    echo "ERROR: no encuentro cloudflared en $CLOUDFLARED_BIN" >&2
    exit 1
fi
if [ ! -f "$SERVICE_TEMPLATE" ]; then
    echo "ERROR: falta la plantilla $SERVICE_TEMPLATE" >&2
    exit 1
fi
if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: falta $ENV_FILE (crea el túnel en Cloudflare y pega el token ahí)." >&2
    exit 1
fi

# 2) Token presente y distinto del placeholder
TOKEN="$(sed -n 's/^TUNNEL_TOKEN=//p' "$ENV_FILE" | head -n1 | tr -d '[:space:]')"
if [ -z "$TOKEN" ]; then
    echo "ERROR: TUNNEL_TOKEN vacío en $ENV_FILE" >&2
    echo "       Crea el túnel en Cloudflare Zero Trust y pega el token en esa línea." >&2
    exit 1
fi
if [ "$TOKEN" = "$PLACEHOLDER" ]; then
    echo "ERROR: el token sigue siendo el placeholder '$PLACEHOLDER'." >&2
    echo "       Pasos: crea el túnel (ver tunel/README.md) y pega el token real en $ENV_FILE" >&2
    exit 1
fi
case "$TOKEN" in
    eyJ*) echo "==> Token presente (formato eyJ... ✓)" ;;
    *)    echo "AVISO: el token no empieza por 'eyJ'. ¿Es el token del dashboard? Se continúa igual." ;;
esac

# 3) Instalar el servicio (patrón idéntico a cloudflared-webcast/indice)
mkdir -p "$(dirname "$SERVICE_DEST")"
cp "$SERVICE_TEMPLATE" "$SERVICE_DEST"
chmod 600 "$SERVICE_DEST"
systemctl --user daemon-reload

if systemctl --user is-active --quiet cloudflared-trackcam; then
    echo "==> Servicio ya activo → reiniciando para aplicar el token/config"
    systemctl --user restart cloudflared-trackcam
else
    echo "==> Instalando y arrancando cloudflared-trackcam.service"
    systemctl --user enable --now cloudflared-trackcam
fi

# 4) Verificación: esperar a que el túnel responda (hasta 20 s)
echo "==> Esperando a que cloudflared conecte (hasta 20 s)..."
OK=0
for _ in $(seq 1 20); do
    sleep 1
    if systemctl --user is-active --quiet cloudflared-trackcam; then
        CODE="$(curl -s -o /dev/null -w '%{http_code}' -m 4 "$URL" || true)"
        if [ "$CODE" = "200" ]; then OK=1; break; fi
    fi
done

echo
if [ "$OK" = "1" ]; then
    echo "✅ TÚNEL ACTIVO: $URL responde HTTP 200"
    echo
    systemctl --user status cloudflared-trackcam --no-pager | head -8
    exit 0
fi

echo "❌ El servicio está instalado pero el túnel NO responde todavía." >&2
echo >&2
echo "   Estado del servicio:" >&2
systemctl --user status cloudflared-trackcam --no-pager | head -10 >&2 || true
echo >&2
echo "   Logs: journalctl --user -u cloudflared-trackcam -n 50 --no-pager" >&2
echo "   Revisa el troubleshooting en tunel/README.md (token inválido, 502, backend caído...)" >&2
exit 1
