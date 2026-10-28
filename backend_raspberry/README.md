# backend_rasperry

Backend que corre en la Raspberry: imprime en una impresora térmica USB (ESC/POS)
y se actualiza solo desde GitLab. Se expone hacia afuera con el túnel de Cloudflare.

## Endpoints

| Método | Ruta       | Auth   | Qué hace |
|--------|------------|--------|----------|
| GET    | `/version` | no     | Devuelve `{"version": "1.1.0"}` (campo `version` de `pyproject.toml`). |
| POST   | `/print`   | sí     | Body `{"text": "...", "copies"?: 1-5, "paper_width"?: "58mm"\|"80mm", "payment_method"?: "...", "cut"?: true}`. 503 si la impresora no está conectada. |
| GET    | `/health`  | sí     | Estado en el momento: `device_id`, versión, commit, impresora USB, SSID, último print OK y último error. |
| POST   | `/update`  | sí     | `git pull --ff-only` desde GitLab por SSH; si hubo cambios corre `uv sync` (si cambió `uv.lock`) y reinicia el servicio. |

**Auth** = el contrato del backend principal (j2ys, `docs/RUNBOOK.md` §4):
`Authorization: Bearer <PRINTER_TOKEN>` y `X-Business-Id: <BUSINESS_ID>`.
Token incorrecto → 401; otro negocio → 403. Es el mismo token que se guarda en la página central
(Comandas → Token de la impresora), así que el backend principal puede imprimir y también actualizar.

```bash
curl -X POST https://api.midominio.com/print -H "Authorization: Bearer $PRINTER_TOKEN" \
     -H "X-Business-Id: $BUSINESS_ID" -H "Content-Type: application/json" -d '{"text": "Pedido #12"}'
```

Para publicar una versión nueva: subir `version` en `pyproject.toml`, hacer push a `main` y pulsar
**Actualizar Raspberry** en la página central (Comandas).
Dependencias nuevas: `uv add <paquete>` y subir `pyproject.toml` + `uv.lock`.

## Heartbeat

La Raspberry manda cada `HEARTBEAT_INTERVAL` segundos (60 por
defecto, mínimo 15) el mismo cuerpo que `/health` al webhook público del backend principal (`HEARTBEAT_URL` en `/etc/raspberry-backend.env` cambia la URL;
`HEARTBEAT_URL=off` lo apaga), con el token y `X-Business-Id` en las cabeceras.
`DEVICE_ID` es opcional: sin él se deriva de `/etc/machine-id`. Solo sale por `https://`; si el backend no responde, se
reintenta en el siguiente ciclo y la impresión no se afecta.

## Desarrollo y pruebas (con [uv](https://docs.astral.sh/uv/))

```bash
uv sync
uv run pytest
uv run uvicorn app.main:app --port 8000
```

- `tests/test_hardware.py`: red (conexión a `gitlab.com:22`) e impresora (detección USB, imprimir, cortar, cerrar aunque falle).
- `tests/test_api.py`: endpoints, token y flujo de actualización.
- `tests/test_tunnel.py`: la regla del túnel se agrega sin borrar las demás.

No necesitan la impresora conectada. Para probar el hardware real en la Raspberry:

```bash
cd /opt/backend_rasperry && uv run python -m app.hardware
```

## Instalación en la Raspberry

1. Crear un token en GitLab (ver abajo), conocer el ID USB de la impresora (`lsusb`, ej. `0483:070b`)
   y el `business_id` del negocio (sale en la URL de la página central: `?business=...`).
2. Copiar la carpeta `deploy/` a la Raspberry y correr:

```bash
sudo GITLAB_TOKEN=glpat-xxxx BUSINESS_ID=<id> PRINTER_VENDOR_ID=0x0483 PRINTER_PRODUCT_ID=0x070b TUNNEL_HOSTNAME=api.midominio.com bash deploy/install.sh
```

3. En la página central → Comandas: URL `https://api.midominio.com/print` y el token que imprimió el instalador.

El script:

- Guarda la configuración en `/etc/raspberry-backend.env` (solo root) y genera el `PRINTER_TOKEN` si no se pasa uno.
- Instala y arranca **`gitlab-ssh.service`**: genera la llave SSH de la Raspberry, verifica la huella oficial de gitlab.com y registra la llave como *deploy key* de solo lectura. Corre en cada arranque; si la llave ya funciona, no hace nada.
- Borra el `GITLAB_TOKEN` del disco cuando ya no se necesita.
- Instala `uv` si no está, clona el repo en `/opt/backend_rasperry` por SSH, corre `uv sync` y da permiso USB a la impresora.
- Instala y arranca **`raspberry-backend.service`** (uvicorn en `127.0.0.1:8000`, se reinicia solo).
- Si se pasa `TUNNEL_HOSTNAME`, actualiza el túnel (siguiente sección).

Se puede volver a correr sin romper nada.

## Túnel de Cloudflare

`scripts/update_tunnel.py` **no elimina el túnel**: abre `/etc/cloudflared/config.yml`, hace un backup,
agrega (o actualiza) la regla `hostname -> http://localhost:8000` antes del catch-all, valida con
`cloudflared tunnel ingress validate` (si falla restaura el backup), crea el DNS del hostname y reinicia `cloudflared`.

```bash
sudo /opt/backend_rasperry/.venv/bin/python scripts/update_tunnel.py --hostname api.midominio.com
```

Los comentarios del YAML se pierden al reescribirlo; el original queda en `config.yml.bak-<fecha>`.
Si el túnel se creó desde el panel de Cloudflare (instalado con un token, sin `config.yml`), la ruta se agrega en
**Zero Trust → Networks → Tunnels → Public hostname**.

## Wi-Fi del negocio desde el panel

Para instalar sin pantalla ni teclado:

1. Antes de llevarla, instalar con `HOTSPOT_SSID=... HOTSPOT_PASSWORD=...`: guarda el hotspot de un celular del
   equipo (perfil `hotspot-equipo`, prioridad 0). En el negocio se prende ese hotspot y la Raspberry entra sola a
   Cloudflare.
2. En la página central → Comandas → **Wi-Fi del negocio**: nombre y clave. El backend llama `POST /wifi`, que guarda
   el perfil `wifi-negocio` (prioridad 10) con `nmcli` **sin cambiarse** de red.
3. Apagar el hotspot: NetworkManager entra al Wi-Fi del negocio. Si la clave estaba mal, vuelve al hotspot cuando
   se prenda otra vez.

El instalador da permiso al usuario del servicio para correr solo `nmcli` con sudo
(`/etc/sudoers.d/raspberry-backend-nmcli`). Guardar el Wi-Fi otra vez reemplaza el anterior.

## Entrar por SSH a la Raspberry (Cloudflare Access)

Para revisar un error directamente en la máquina, sin abrir puertos en el router del negocio.

1. **Raspberry:** instalar (o reinstalar) con `SSH_HOSTNAME=ssh-<negocio>.j2ysm.uk`. Activa SSH y agrega
   `ssh-<negocio>.j2ysm.uk -> ssh://localhost:22` al túnel, sin borrar las otras rutas.
2. **Cloudflare (una vez por hostname):** Zero Trust → Access → Applications → Add → *Self-hosted*,
   dominio `ssh-<negocio>.j2ysm.uk`, política *Allow* con los correos autorizados. Sin esta aplicación,
   el SSH quedaría expuesto a internet: no saltarse este paso.
3. **Tu PC (una vez):** instalar `cloudflared` y agregar a `~/.ssh/config`:

   ```
   Host ssh-*.j2ysm.uk
     ProxyCommand cloudflared access ssh --hostname %h
   ```

   Entrar: `ssh <usuario>@ssh-<negocio>.j2ysm.uk`. La primera vez abre el navegador para el login de Access.

Quitarle el acceso a alguien = sacar su correo de la política en Cloudflare.

## Token de GitLab y conexión SSH automática (investigación)

**SSH no acepta tokens.** GitLab autentica SSH solo con llaves; los tokens (personales, de proyecto o *deploy tokens*)
sirven para la API y para git por HTTPS. Por eso el flujo automático es:

1. La Raspberry genera su propia llave SSH (`~/.ssh/gitlab_deploy`, nunca sale del equipo).
2. Usa el token **una sola vez** para registrar esa llave como *deploy key* con la API:
   `POST https://gitlab.com/api/v4/projects/:id/deploy_keys` (header `PRIVATE-TOKEN`).
3. Desde ahí `git pull` funciona por SSH sin token ni pasos manuales. El token se borra del disco.

Qué token crear:

| Token | En GitLab.com gratis | Sirve para registrar la llave |
|-------|---------------------|-------------------------------|
| **Personal access token** (recomendado) | Sí | Sí, con scope `api` |
| Project access token | No (requiere Premium/Ultimate) | Sí, con scope `api` y rol Maintainer |
| Deploy token | Sí | No: solo da acceso por HTTPS, no puede crear llaves |

Crear el personal access token: GitLab → avatar → **Edit profile → Access tokens → Add new token**,
scope `api`, fecha de vencimiento corta (ej. 7 días). Después de instalar se puede revocar: la deploy key sigue funcionando.

La deploy key queda en **Settings → Repository → Deploy keys** del proyecto, con nombre `raspberry-<hostname>`.
Es de solo lectura: la Raspberry puede bajar código pero no subirlo.

Alternativa sin SSH: un *deploy token* con `read_repository` y clonar por HTTPS
(`https://<usuario>:<token>@gitlab.com/...`). Funciona, pero el token queda guardado en la Raspberry y vence.
