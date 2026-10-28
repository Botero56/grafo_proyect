#!/usr/bin/env bash
# Instalación completa en la Raspberry (ver README). Se puede volver a correr.
#   sudo GITLAB_TOKEN=glpat-... BUSINESS_ID=<id del negocio> PRINTER_VENDOR_ID=0x0483 PRINTER_PRODUCT_ID=0x070b \
#        [TUNNEL_HOSTNAME=impresora-x.dominio.com] [SSH_HOSTNAME=ssh-x.dominio.com] [HEARTBEAT_URL=https://<webhook>/devices/heartbeat] [BRANCH=main] [PORT=8000] \
#        [HOTSPOT_SSID=... HOTSPOT_PASSWORD=...] bash deploy/install.sh
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Ejecutar con sudo" >&2; exit 1; }

HERE=$(cd "$(dirname "$0")" && pwd)
USER_NAME=${SERVICE_USER:-${SUDO_USER:-pi}}
APP_DIR=/opt/backend_rasperry
ENV_FILE=/etc/raspberry-backend.env
# Puerto local del backend; otro distinto de 8000 si ya hay algo escuchando ahí (migración).
PORT=${PORT:-8000}
[[ "$PORT" =~ ^[0-9]+$ ]] || { echo "PORT inválido" >&2; exit 1; }
REMOTE=git@gitlab.com:boterosamuel3/backend_rasperry.git

apt-get install -y -qq git openssh-client curl >/dev/null
# uv en /usr/local/bin para que lo encuentren el servicio y POST /update
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh

# 1. Variables del servicio (no se sobrescriben si ya existen)
if [ ! -f "$ENV_FILE" ]; then
  # GITLAB_TOKEN es opcional: solo hace falta si la llave SSH aún no está registrada en GitLab.
  : "${BUSINESS_ID:?Falta BUSINESS_ID}" \
    "${PRINTER_VENDOR_ID:?Falta PRINTER_VENDOR_ID}" "${PRINTER_PRODUCT_ID:?Falta PRINTER_PRODUCT_ID}"
  PRINTER_TOKEN=${PRINTER_TOKEN:-$(openssl rand -hex 24)}
  install -m 600 /dev/null "$ENV_FILE"
  cat > "$ENV_FILE" <<EOF
PRINTER_TOKEN=$PRINTER_TOKEN
BUSINESS_ID=$BUSINESS_ID
PRINTER_VENDOR_ID=$PRINTER_VENDOR_ID
PRINTER_PRODUCT_ID=$PRINTER_PRODUCT_ID
GITLAB_PROJECT=boterosamuel3/backend_rasperry
EOF
  if [ -n "${GITLAB_TOKEN:-}" ]; then echo "GITLAB_TOKEN=$GITLAB_TOKEN" >> "$ENV_FILE"; fi
  echo ">> Token de la impresora (pégalo en la página central, Comandas > Token): $PRINTER_TOKEN"
fi

# 1b. Heartbeat al backend principal (se puede agregar después re-corriendo con HEARTBEAT_URL)
if [ -n "${HEARTBEAT_URL:-}" ]; then
  sed -i '/^HEARTBEAT_URL=/d' "$ENV_FILE"
  echo "HEARTBEAT_URL=$HEARTBEAT_URL" >> "$ENV_FILE"
fi

# 2. Servicio SSH hacia GitLab: registra la deploy key y valida la conexión
install -m 755 "$HERE/setup_gitlab_ssh.sh" /usr/local/bin/setup-gitlab-ssh
for unit in gitlab-ssh raspberry-backend; do
  sed -e "s/__USER__/$USER_NAME/" -e "s/__PORT__/$PORT/" "$HERE/$unit.service" > "/etc/systemd/system/$unit.service"
done
systemctl daemon-reload
systemctl enable gitlab-ssh.service
systemctl restart gitlab-ssh.service   # oneshot: espera a que termine
sed -i '/^GITLAB_TOKEN=/d' "$ENV_FILE"  # la llave ya quedó registrada; el token no se guarda

# 3. Código y dependencias
if [ ! -d "$APP_DIR/.git" ]; then
  install -d -o "$USER_NAME" "$APP_DIR"
  sudo -u "$USER_NAME" git clone -b "${BRANCH:-main}" "$REMOTE" "$APP_DIR"
fi
(cd "$APP_DIR" && sudo -u "$USER_NAME" uv sync --frozen --no-dev)

# 4. Permiso USB para la impresora
set -a; . "$ENV_FILE"; set +a
vid=${PRINTER_VENDOR_ID,,}; pid=${PRINTER_PRODUCT_ID,,}
cat > /etc/udev/rules.d/99-escpos.rules <<EOF
SUBSYSTEM=="usb", ATTRS{idVendor}=="${vid#0x}", ATTRS{idProduct}=="${pid#0x}", MODE="0660", GROUP="plugdev"
EOF
usermod -aG plugdev "$USER_NAME"
udevadm control --reload-rules && udevadm trigger

# 4b. Wi-Fi desde el panel: POST /wifi guarda la red del negocio con nmcli (solo ese comando con sudo)
NMCLI=$(command -v nmcli)
echo "$USER_NAME ALL=(root) NOPASSWD: $NMCLI" > /etc/sudoers.d/raspberry-backend-nmcli
chmod 440 /etc/sudoers.d/raspberry-backend-nmcli
visudo -cf /etc/sudoers.d/raspberry-backend-nmcli >/dev/null
# Hotspot del equipo: red de respaldo con la que la Raspberry llega al negocio sin pantalla ni teclado.
# Prioridad 0: el Wi-Fi del negocio (10) le gana cuando ambos están.
if [ -n "${HOTSPOT_SSID:-}" ]; then
  nmcli connection delete id hotspot-equipo >/dev/null 2>&1 || true
  nmcli connection add type wifi con-name hotspot-equipo ssid "$HOTSPOT_SSID"     wifi-sec.key-mgmt wpa-psk wifi-sec.psk "${HOTSPOT_PASSWORD:?Falta HOTSPOT_PASSWORD}"     connection.autoconnect yes connection.autoconnect-priority 0 >/dev/null
fi

# 5. Backend
systemctl enable raspberry-backend.service
systemctl restart raspberry-backend.service

# 6. Túnel de Cloudflare (solo agrega/actualiza rutas, no lo borra)
#    TUNNEL_HOSTNAME -> backend (impresión/actualización)
#    SSH_HOSTNAME    -> SSH de la Raspberry; protegerlo con Cloudflare Access (README)
TUNNEL_CONFIG=${TUNNEL_CONFIG:-/etc/cloudflared/config.yml}
[ -f "$TUNNEL_CONFIG" ] || TUNNEL_CONFIG="$(getent passwd "$USER_NAME" | cut -d: -f6)/.cloudflared/config.yml"
tunnel_route() {
  if [ ! -f "$TUNNEL_CONFIG" ]; then
    # Túnel corriendo con token (sin config.yml): las rutas viven en el panel de Cloudflare.
    echo ">> Agrega en Cloudflare (Zero Trust > Networks > Tunnels > Public hostname): $1 -> $2"
    return 0
  fi
  "$APP_DIR/.venv/bin/python" "$APP_DIR/scripts/update_tunnel.py" --config "$TUNNEL_CONFIG" --hostname "$1" --service "$2"
}
if [ -n "${TUNNEL_HOSTNAME:-}" ]; then tunnel_route "$TUNNEL_HOSTNAME" "http://localhost:$PORT"; fi
if [ -n "${SSH_HOSTNAME:-}" ]; then
  systemctl enable --now ssh
  tunnel_route "$SSH_HOSTNAME" ssh://localhost:22
fi

(cd "$APP_DIR" && .venv/bin/python -m app.hardware) || true
echo ">> Listo. Prueba: curl http://127.0.0.1:$PORT/version"
