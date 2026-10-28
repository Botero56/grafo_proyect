"""Agrega o actualiza la ruta del backend en el config de cloudflared.

No borra el túnel ni las otras reglas: solo inserta/actualiza una regla de ingress,
deja un backup, valida con cloudflared y reinicia el servicio. Si la validación
falla, restaura el backup.

    sudo .venv/bin/python scripts/update_tunnel.py --hostname api.midominio.com
"""
import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

CATCH_ALL = {"service": "http_status:404"}


def upsert_ingress(config: dict, hostname: str, service: str) -> dict:
    rules = config.setdefault("ingress", [])
    for rule in rules:
        if rule.get("hostname") == hostname:
            rule["service"] = service
            return config
    # cloudflared exige que la última regla sea el catch-all (sin hostname ni path).
    has_catch_all = bool(rules) and "hostname" not in rules[-1] and "path" not in rules[-1]
    if not has_catch_all:
        rules.append(dict(CATCH_ALL))
    rules.insert(len(rules) - 1, {"hostname": hostname, "service": service})
    return config


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hostname", required=True)
    parser.add_argument("--service", default="http://localhost:8000")
    parser.add_argument("--config", type=Path, default=Path("/etc/cloudflared/config.yml"))
    parser.add_argument("--no-restart", action="store_true")
    args = parser.parse_args()

    if not args.config.exists():
        print(f"No existe {args.config}. Si el túnel se administra desde el panel de "
              "Cloudflare, agrega el hostname allí (Networks > Tunnels > Public hostname).")
        return 1

    config = yaml.safe_load(args.config.read_text()) or {}
    backup = args.config.with_name(f"{args.config.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(args.config, backup)

    upsert_ingress(config, args.hostname, args.service)
    args.config.write_text(yaml.safe_dump(config, sort_keys=False))

    check = subprocess.run(["cloudflared", "tunnel", "--config", str(args.config), "ingress", "validate"],
                           capture_output=True, text=True)
    if check.returncode != 0:
        shutil.copy2(backup, args.config)
        print(f"Config inválida, restaurado el backup:\n{check.stderr}")
        return 1

    if config.get("tunnel"):
        # Crea el CNAME del hostname hacia el túnel; si ya existe, solo avisa.
        dns = subprocess.run(["cloudflared", "tunnel", "route", "dns", str(config["tunnel"]), args.hostname],
                             capture_output=True, text=True)
        if dns.returncode != 0:
            print(f"Aviso DNS: {dns.stderr.strip()}")

    if not args.no_restart:
        subprocess.run(["systemctl", "restart", "cloudflared"], check=True)
    print(f"{args.hostname} -> {args.service} (backup en {backup})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
