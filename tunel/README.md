# Túnel Cloudflare — TrackCam

Expone el servidor TrackCam de casa a Internet de forma segura:

```
https://track.satetsunin.com  ──►  Cloudflare edge  ──►  cloudflared (este PC, Victus)
                                                                │
                                                        http://127.0.0.1:8099
                                                        (FastAPI TrackCam)
```

Sin abrir ningún puerto en el router. Mismo patrón que los túneles **webcast**
(`tv.satetsunin.com`) e **indice** (`todo.satetsunin.com`) que ya tienes funcionando.

---

## Contenido de esta carpeta

| Fichero | Qué es |
|---|---|
| `cloudflared-trackcam.service` | Plantilla del servicio systemd de usuario (se copia a `~/.config/systemd/user/`) |
| `scripts/activar_tunel.sh` | Script de activación: valida token → instala servicio → verifica con curl |
| `README.md` | Esta guía |

El **token** vive fuera del repo, en `~/.cloudflared/trackcam.env` (permisos 600),
igual que `webcast.env` e `indice-tunnel.env`.

---

## Patrón copiado de tus túneles existentes

Tus servicios actuales (`~/.config/systemd/user/cloudflared-webcast.service` y
`cloudflared-indice.service`) siguen este esquema, y el de TrackCam lo replica:

```ini
[Unit]
Description=Cloudflare Tunnel trackcam (track.satetsunin.com -> localhost:8099)
After=network-online.target
Wants=network-online.target

[Service]
EnvironmentFile=%h/.cloudflared/trackcam.env        # el token se lee de aquí
ExecStart=%h/.local/bin/cloudflared tunnel --no-autoupdate --protocol http2 run --token ${TUNNEL_TOKEN}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target                             # arranca con la sesión de usuario
```

- Fichero de token: `~/.cloudflared/trackcam.env` → `TUNNEL_TOKEN=<token>` (chmod 600).
- En `ExecStart`, systemd expande `${TUNNEL_TOKEN}` desde el `EnvironmentFile` (verificado en este equipo).
- *Alternativa equivalente* (la que usan webcast/indice, sin `--token`):
  cloudflared lee la variable `TUNNEL_TOKEN` automáticamente al hacer `tunnel run`.
  Ambas formas son válidas; esta plantilla usa `--token` explícito.

---

## Paso 1 — Crear el túnel en Cloudflare (solo la primera vez)

> ⚠️ Requiere tu sesión en el dashboard. **No se puede automatizar por ti.**
> Necesitas una cuenta Cloudflare con el dominio `satetsunin.com` (ya la tienes).

### Vía A — Dashboard (RECOMENDADA)

1. Entra en **https://one.dash.cloudflare.com** → **Zero Trust** → **Networks** → **Tunnels**.
2. Botón **Create a tunnel** → elige **Cloudflared** → **Next**.
3. **Tunnel name**: `trackcam` → **Save tunnel**.
4. En la pantalla siguiente ("Install and run a connector") te muestra un comando tipo:

   ```bash
   cloudflared service install eyJhIjoi...<TOKEN_LARGO>...
   ```

   **Copia solo el TOKEN** (la parte `eyJ...` que va después de `service install`).

5. Pega ese token en `~/.cloudflared/trackcam.env`, sustituyendo el placeholder:

   ```bash
   nano ~/.cloudflared/trackcam.env
   # TUNNEL_TOKEN=PON_AQUI_EL_TOKEN_DEL_TUNEL_CF  →  TUNNEL_TOKEN=eyJhIjoi... (token real)
   ```

6. Configura el **public hostname** (lo hace el dashboard, no el PC):
   En el túnel `trackcam` → pestaña **Public Hostname** → **Add a public hostname**:
   - **Subdomain**: `track`
   - **Domain**: `satetsunin.com`
   - **Type**: `HTTP`
   - **URL**: `127.0.0.1:8099`  (o `localhost:8099`)
   - **Save hostname**.

   Con el modo token, la configuración del hostname se gestiona desde el dashboard
   (remotely-managed), no con `config.yml`.

### Vía B — Línea de comandos (alternativa)

Equivalente si prefieres CLI (requiere haber hecho `cloudflared tunnel login` una vez):

```bash
cloudflared tunnel create trackcam                                   # crea el túnel
cloudflared tunnel route dns trackcam track.satetsunin.com           # CNAME del DNS
cloudflared tunnel token trackcam        # ← imprime el token; pégalo en ~/.cloudflared/trackcam.env
```

Y después configura el **Public Hostname** en el dashboard igual que en la vía A
(paso 6), porque el servicio corre en modo `--token` (config gestionada por el dashboard).

---

## Paso 2 — Pegar el token

El fichero `~/.cloudflared/trackcam.env` ya existe con este contenido y permisos 600:

```bash
# token túnel trackcam - NO compartir
TUNNEL_TOKEN=PON_AQUI_EL_TOKEN_DEL_TUNEL_CF
```

Edítalo y pega el token real (debe empezar por `eyJ...`):

```bash
nano ~/.cloudflared/trackcam.env        # o: code ~/.cloudflared/trackcam.env
chmod 600 ~/.cloudflared/trackcam.env   # por si acaso (ya debería estar)
```

> 🔒 Nunca subas este fichero a GitHub ni lo compartas: quien tenga el token
> puede exponer tu red (es un conector hacia tu PC).

---

## Paso 3 — Activar el túnel

```bash
cd ~/Escritorio/proyectos/trackcam
bash tunel/scripts/activar_tunel.sh
```

El script:
1. Comprueba que `cloudflared` existe y que el token **no** es el placeholder.
2. Copia `tunel/cloudflared-trackcam.service` → `~/.config/systemd/user/` (chmod 600).
3. `systemctl --user daemon-reload` y `systemctl --user enable --now cloudflared-trackcam`.
4. Espera hasta 20 s y verifica que `https://track.satetsunin.com/api/estado` responde **HTTP 200**.

Si el token sigue siendo el placeholder, el script **se niega** a arrancar el servicio
(evita un bucle de reintentos sin sentido).

---

## Verificación manual

```bash
# El servicio está activo y habilitado para arrancar con la sesión
systemctl --user status cloudflared-trackcam

# El endpoint responde 200 (GET; ojo: este endpoint da 405 a HEAD)
curl -s -o /dev/null -w "HTTP %{http_code}\n" https://track.satetsunin.com/api/estado
curl -s https://track.satetsunin.com/api/estado | head -c 200; echo

# Logs del túnel
journalctl --user -u cloudflared-trackcam -n 50 --no-pager
```

---

## Parar / reiniciar / desactivar

```bash
# Parar (hasta el próximo arranque de sesión)
systemctl --user stop cloudflared-trackcam

# Reiniciar (por ejemplo tras cambiar el token)
systemctl --user restart cloudflared-trackcam

# Desactivar por completo (no arranca más con la sesión)
systemctl --user disable --now cloudflared-trackcam
```

---

## Troubleshooting

| Síntoma | Causa probable | Solución |
|---|---|---|
| `Failed to determine default credential path` / `Error: cloudflared service install` no aplica | Token mal pegado o placeholder | Revisa `~/.cloudflared/trackcam.env`; el token empieza por `eyJ` |
| `error: failed to connect to new edge` | Token inválido/revocado, o túnel borrado | Regenera el token en el dashboard (túnel → Settings → Token) |
| Servicio reiniciándose en bucle (`Restart=on-failure`) | Token placeholder o red sin conexión | `journalctl --user -u cloudflared-trackcam -n 50` para ver el error exacto |
| **502 Bad Gateway** | Cloudflare llega al túnel pero **no hay nada en :8099** | ¿Está el backend arrancado? `curl http://127.0.0.1:8099/api/estado` localmente; relanza uvicorn |
| **404 / "no hostname configured"** | Falta el Public Hostname en el dashboard | Túnel → `trackcam` → Public Hostname → `track.satetsunin.com` → `127.0.0.1:8099` |
| **530 / DNS no resuelve** | CNAME no creado o propagación pendiente | Con token (dashboard) se crea solo; con CLI: `cloudflared tunnel route dns trackcam track.satetsunin.com`; espera unos minutos |
| `--token` vacío / `TUNNEL_TOKEN` no se expande | `EnvironmentFile` apunta a otra ruta | El fichero debe ser `%h/.cloudflared/trackcam.env` y contener `TUNNEL_TOKEN=...` |
| El script dice "servicio instalado pero no responde" | Token inválido, backend caído o DNS aún propagando | Sigue el orden: logs → token → hostname → backend |

### Backend caído (el 502 típico)

El túnel solo encamina; no levanta el servidor. Arranca TrackCam como siempre:

```bash
cd ~/Escritorio/proyectos/trackcam
source venv/bin/activate
uvicorn backend.app:app --host 0.0.0.0 --port 8099
```

(El 502 de Cloudflare = túnel vivo pero origen muerto: comprueba primero el
backend local con `curl http://127.0.0.1:8099/api/estado`.)

---

## Recordatorio

- No abras puertos en el router: cloudflared sale por HTTPS (443) hacia Cloudflare.
- El túnel es un **servicio de usuario**: arranca al iniciar sesión en el PC.
  Si quieres que arranque sin sesión gráfica, habría que moverlo a un servicio de
  sistema (`/etc/systemd/system/`) — no es el caso ahora (webcast/indice usan el mismo patrón).
- IP LAN del Victus: `192.168.1.236` (solo informativa; cloudflared corre en este mismo PC).
