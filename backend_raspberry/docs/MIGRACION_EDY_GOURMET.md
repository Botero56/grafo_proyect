# Migración de la Raspberry de Edy Gourmet a backend_rasperry

**Objetivo:** reemplazar la app vieja de la Raspberry (FastAPI 0.1.0 con `/`, `/test` y `/print`, sin token)
por `backend_rasperry` 1.2.0 (token, `/health`, `/update` y heartbeat) **sin dejar de imprimir comandas**.

**Cómo:** el agente nuevo se instala **al lado** del viejo (otro puerto y otro hostname). Producción sigue
imprimiendo por el viejo hasta que el panel apunte al nuevo. Ese cambio tarda segundos y se revierte igual de rápido.

| Dato | Valor |
|---|---|
| Negocio | `kapso_test`, id `oqj9CV7LaCuvYQvatRYn` |
| Túnel de Cloudflare | `edy_gourmet` (`69e9d2ee-d30a-475a-a801-43078adf7010`) |
| Hostname actual (app vieja) | `https://edygourmet.j2ysm.uk/print` (el worker lo usa como `LEGACY_PRINT_URL`) |
| Hostname nuevo (agente nuevo) | `https://impresora-edygourmet.3b-technology.com/print` |
| Puerto local del agente nuevo | `8001` (el viejo probablemente ocupa el 8000) |
| Rama | `feat/flota-heartbeat` (ya está en `boterosamuel3/backend_rasperry`, de donde clona el instalador) |

Hora recomendada: **fuera del horario de pedidos**. Duración: unos 30 minutos.

---

## 0. Antes de ir (desde el PC)

1. **Token de GitLab** (cuenta `boterosamuel3`): Edit profile → Access tokens → scope `api`, que venza en 1 día.
   Sirve para bajar el código y registrar la llave de la Raspberry. Se revoca al terminar.
2. **DNS del hostname nuevo** (una sola vez, en Cloudflare → dominio `3b-technology.com` → DNS → Add record):
   `CNAME` · nombre `impresora-edygourmet` · destino `69e9d2ee-d30a-475a-a801-43078adf7010.cfargotunnel.com` · Proxied.
   Mientras la Raspberry no tenga la regla, ese hostname responde con error. No afecta nada.
3. Tener a mano un acceso al panel (`panel.3b-technology.com`) y `gcloud` para el rollback (paso 6).

## 1. Diagnóstico en la Raspberry (no cambia nada)

Con una terminal en la Raspberry (teclado y monitor, o SSH en la red local):

```bash
cat /etc/os-release | head -3
nmcli general 2>/dev/null || echo "sin NetworkManager"
lsusb
sudo ss -ltnp | grep -E ':80[0-9]{2}'
systemctl list-units --type=service --state=running | grep -iE 'print|uvicorn|python|cloudflared|fastapi'
systemctl cat cloudflared 2>/dev/null | grep -E 'ExecStart'
ls -la /etc/cloudflared ~/.cloudflared 2>/dev/null
test -f /etc/raspberry-backend.env && echo "OJO: ya existe /etc/raspberry-backend.env"
```

Anota:
- **IDs USB** de la impresora en `lsusb` (ej. `ID 0483:070b` → `0x0483` y `0x070b`).
- **Puerto y servicio de la app vieja** (de `ss` y `systemctl`). Si el 8001 está ocupado, usa otro puerto libre en los pasos siguientes.
- **Cómo corre cloudflared**: si `ExecStart` trae `--token`, el túnel se administra desde el panel de Cloudflare
  (paso 2b). Si hay `config.yml`, el instalador agrega la ruta solo.
- La salida de `os-release` y `nmcli`: es lo que falta para diseñar la configuración Wi-Fi remota (fase 5).

## 2. Instalar el agente nuevo al lado del viejo

La impresión sigue saliendo por la app vieja durante todo este paso.

```bash
read -rs GITLAB_TOKEN   # pegar el token del paso 0 (no queda en el historial)
mkdir -p ~/brp && cd ~/brp
curl -fsS -H "PRIVATE-TOKEN: $GITLAB_TOKEN" \
  "https://gitlab.com/api/v4/projects/boterosamuel3%2Fbackend_rasperry/repository/archive.tar.gz?sha=feat/flota-heartbeat" \
  | tar -xz --strip-components=1

sudo GITLAB_TOKEN="$GITLAB_TOKEN" BUSINESS_ID=oqj9CV7LaCuvYQvatRYn \
     PRINTER_VENDOR_ID=0x____ PRINTER_PRODUCT_ID=0x____ \
     PORT=8001 BRANCH=feat/flota-heartbeat \
     TUNNEL_HOSTNAME=impresora-edygourmet.3b-technology.com \
     bash deploy/install.sh
unset GITLAB_TOKEN
```

- El instalador imprime `>> Token de la impresora ...`. **Cópialo**: se pega en el panel en el paso 4. No lo mandes por chat.
- Si el instalador muestra `Aviso DNS`, ignóralo: el DNS ya se creó a mano en el paso 0, en `3b-technology.com`.
- Si `TUNNEL_HOSTNAME` falla porque el túnel corre con token (sin `config.yml`), haz el **2b**.

**2b. Túnel administrado desde Cloudflare:** Zero Trust → Networks → Tunnels → `edy_gourmet` → Public hostname → Add:
`impresora-edygourmet.3b-technology.com` → `HTTP` → `localhost:8001`. **No toques** la regla de `edygourmet.j2ysm.uk`.

## 3. Probar el agente nuevo (producción sigue en el viejo)

```bash
TOKEN=$(sudo grep ^PRINTER_TOKEN= /etc/raspberry-backend.env | cut -d= -f2)
B=oqj9CV7LaCuvYQvatRYn
curl -s http://127.0.0.1:8001/version                                    # {"version":"1.2.0"}
curl -s -H "Authorization: Bearer $TOKEN" -H "X-Business-Id: $B" http://127.0.0.1:8001/health
#   -> "printer_usb": true, "wifi_ssid": "...", "device_id": "rpi-..."
curl -s https://impresora-edygourmet.3b-technology.com/version                   # mismo resultado, ya por el túnel
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "X-Business-Id: $B" -H 'Content-Type: application/json' \
  -d '{"text":"*** PRUEBA DE INSTALACION ***\nNO PREPARAR"}' http://127.0.0.1:8001/print
journalctl -u raspberry-backend -n 20 --no-pager                         # sin "heartbeat no enviado"
```

- Tiene que salir el papel de prueba. Si responde **500 con "Resource busy"**, la app vieja tiene tomada la impresora USB.
  En ese caso, el cambio del paso 4 debe hacerse **junto con** apagar la app vieja (paso 5, primer comando), en un momento sin pedidos.
- El heartbeat se confirma desde el PC: `GET /api/v1/admin/configurations/oqj9CV7LaCuvYQvatRYn/printer/devices`
  tiene que mostrar la Raspberry como `operativo`.

## 4. El cambio (segundos, sin corte)

Panel → negocio → **Comandas**:
- Destino: **Impresora**
- URL: `https://impresora-edygourmet.3b-technology.com/print`
- Token: el del paso 2
- Ancho del papel: el que usa hoy
- **Guardar**

Desde esa comanda, el backend imprime por el agente nuevo, con token. En la misma sección aparece la Raspberry con su
estado, el Wi-Fi y la versión. Para confirmarlo, haz un pedido de prueba por WhatsApp y revisa que salga.

## 5. Retirar lo viejo (después de 1 o 2 días sin fallos)

```bash
# En la Raspberry: apagar la app vieja (usa el nombre de servicio del paso 1)
sudo systemctl disable --now <servicio-viejo>
```
- Cloudflare: borrar la regla `edygourmet.j2ysm.uk` del túnel (o en `config.yml`, cambiar su `service` por `http_status:404`
  y reiniciar `cloudflared`). **Esto cierra el `/print` sin token que hoy está expuesto a internet.**
- Backend (desde el PC):
  ```bash
  gcloud run services update j2ys-lt-app --region us-central1 --remove-env-vars LEGACY_PRINT_URL,LEGACY_PRINT_BUSINESS_ID,PRINT_URL
  ```
- Revocar el token de GitLab del paso 0. La deploy key de la Raspberry sigue funcionando.

## 6. Rollback

**Antes del paso 5** (la app vieja sigue viva): desactivar la impresora por API. El backend vuelve solo a `LEGACY_PRINT_URL`.
⚠️ En el panel **no** elijas "Chat del admin": eso manda las comandas a WhatsApp, no a la Raspberry vieja.

```bash
B=oqj9CV7LaCuvYQvatRYn
K=https://j2ys-lt-app-ywi3275i5a-uc.a.run.app
curl -s -X PUT "$K/api/v1/admin/configurations/$B/printer" \
  -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  -H "X-API-Key: $(gcloud secrets versions access latest --secret admin-api-key)" \
  -H 'Content-Type: application/json' \
  -d '{"enabled": false, "endpoint_url": "https://impresora-edygourmet.3b-technology.com/print"}'
```

**Después del paso 5:** `sudo systemctl enable --now <servicio-viejo>`, restaurar la regla `edygourmet.j2ysm.uk` y luego
el comando de arriba. Para quitar el agente nuevo: `sudo systemctl disable --now raspberry-backend`.

## Después

- El botón **Actualizar Raspberry** del panel baja lo nuevo de la rama `feat/flota-heartbeat`. Cuando esa rama se fusione,
  en la Raspberry: `cd /opt/backend_rasperry && git switch main`.
- `/update` todavía hace `git pull` sobre el código en uso y no tiene rollback (fase 4 del plan). Úsalo con cuidado.
