"""Acceso a la impresora térmica USB (ESC/POS) y chequeo de red."""
import os
import socket


def check_network(host: str = "gitlab.com", port: int = 22, timeout: float = 3.0) -> bool:
    """True si se puede abrir una conexión TCP (por defecto, al SSH de GitLab)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _usb_ids() -> tuple[int, int]:
    return int(os.environ["PRINTER_VENDOR_ID"], 16), int(os.environ["PRINTER_PRODUCT_ID"], 16)


def check_printer() -> bool:
    """True si la impresora está conectada por USB."""
    import usb.core  # import tardío: los tests no necesitan libusb

    vendor, product = _usb_ids()
    try:
        return usb.core.find(idVendor=vendor, idProduct=product) is not None
    except usb.core.NoBackendError:
        return False


def get_printer():
    from escpos.printer import Usb

    return Usb(*_usb_ids())


def print_text(text: str, cut: bool = True, copies: int = 1) -> None:
    printer = get_printer()
    try:
        for _ in range(copies):
            printer.text(text if text.endswith("\n") else text + "\n")
            if cut:
                printer.cut()
    finally:
        printer.close()


if __name__ == "__main__":
    # Diagnóstico en la Raspberry: python -m app.hardware
    net, prn = check_network(), check_printer()
    print(f"red (gitlab.com:22): {'OK' if net else 'FALLA'}")
    print(f"impresora USB:       {'OK' if prn else 'FALLA'}")
    raise SystemExit(0 if net and prn else 1)
