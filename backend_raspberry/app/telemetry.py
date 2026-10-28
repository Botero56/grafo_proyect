"""Estado de esta Raspberry y heartbeat hacia el backend principal.

El backend no puede saber si la Raspberry está viva si solo la llama él:
cada HEARTBEAT_INTERVAL segundos la Raspberry le manda su estado
(POST HEARTBEAT_URL, mismo token y X-Business-Id que /print).
Nunca se envían contraseñas Wi-Fi ni el token en el cuerpo.
"""
import hashlib
import json
import logging
import os
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from app import hardware

logger = logging.getLogger("telemetry")
ROOT = Path(__file__).resolve().parent.parent

# Último resultado de /print, lo actualiza main.py.
_state = {"last_print_ok_at": None, "last_error": None, "last_error_at": None}
_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_print(ok: bool, error: str | None = None) -> None:
    with _lock:
        if ok:
            _state["last_print_ok_at"] = _now()
        else:
            _state["last_error"] = (error or "error desconocido")[:300]
            _state["last_error_at"] = _now()


def device_id() -> str:
    """DEVICE_ID del env, o uno estable derivado de /etc/machine-id (sin exponerlo)."""
    if os.environ.get("DEVICE_ID"):
        return os.environ["DEVICE_ID"]
    try:
        machine = Path("/etc/machine-id").read_text().strip()
    except OSError:
        return "rpi-desconocido"
    return "rpi-" + hashlib.sha256(machine.encode()).hexdigest()[:12]


def _cmd(*args: str) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=5, cwd=ROOT).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def current_commit() -> str:
    return _cmd("git", "rev-parse", "--short", "HEAD")


def wifi_ssid() -> str | None:
    """SSID activo. iwgetid si existe; si no, NetworkManager."""
    ssid = _cmd("iwgetid", "-r")
    if not ssid:
        for line in _cmd("nmcli", "-t", "-f", "active,ssid", "dev", "wifi").splitlines():
            if line.startswith("yes:"):
                ssid = line[4:]
                break
    return ssid or None


def uptime_seconds() -> int | None:
    try:
        return int(float(Path("/proc/uptime").read_text().split()[0]))
    except (OSError, ValueError, IndexError):
        return None


def snapshot(version: str) -> dict:
    try:
        printer_usb = hardware.check_printer()
    except Exception:  # sin libusb / IDs mal configurados: se reporta, no se cae
        printer_usb = False
    with _lock:
        state = dict(_state)
    return {
        "device_id": device_id(),
        "measured_at": _now(),
        "version": version,
        "commit": current_commit() or None,
        "agent": "ok",
        "printer_usb": printer_usb,
        "wifi_ssid": wifi_ssid(),
        "uptime_seconds": uptime_seconds(),
        **state,
    }


# Webhook público del backend principal. HEARTBEAT_URL lo cambia; "off" lo apaga.
DEFAULT_HEARTBEAT_URL = "https://j2ys-lt-plain-ywi3275i5a-uc.a.run.app/devices/heartbeat"


def heartbeat_url() -> str:
    url = os.environ.get("HEARTBEAT_URL") or DEFAULT_HEARTBEAT_URL
    return "" if url == "off" else url


def send_heartbeat(version: str) -> bool:
    url = heartbeat_url()
    token = os.environ.get("PRINTER_TOKEN", "")
    business_id = os.environ.get("BUSINESS_ID", "")
    if not (url.startswith("https://") and token and business_id):
        return False
    request = urllib.request.Request(
        url,
        data=json.dumps(snapshot(version)).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "X-Business-Id": business_id,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return 200 <= response.status < 300
    except Exception as exc:
        # Sin el token ni la URL completa en el log.
        logger.warning("heartbeat no enviado: %s", type(exc).__name__)
        return False


def start_heartbeat_loop(version: str) -> threading.Thread | None:
    if not heartbeat_url():
        return None
    interval = max(15, int(os.environ.get("HEARTBEAT_INTERVAL", "60")))

    def loop():
        while True:
            send_heartbeat(version)
            time.sleep(interval)

    thread = threading.Thread(target=loop, name="heartbeat", daemon=True)
    thread.start()
    return thread
