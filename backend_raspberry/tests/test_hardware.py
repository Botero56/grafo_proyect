"""Pruebas de red e impresora (sin hardware: se simulan socket y USB)."""
import socket
from unittest.mock import MagicMock

import usb.core

from app import hardware


def test_red_ok(monkeypatch):
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: MagicMock())
    assert hardware.check_network() is True


def test_red_caida(monkeypatch):
    def falla(*a, **k):
        raise OSError("sin red")
    monkeypatch.setattr(socket, "create_connection", falla)
    assert hardware.check_network() is False


def test_impresora_conectada(monkeypatch, usb_ids):
    llamada = {}
    def find(**kw):
        llamada.update(kw)
        return object()
    monkeypatch.setattr(usb.core, "find", find)
    assert hardware.check_printer() is True
    assert llamada == {"idVendor": 0x04B8, "idProduct": 0x0202}


def test_impresora_desconectada(monkeypatch, usb_ids):
    monkeypatch.setattr(usb.core, "find", lambda **kw: None)
    assert hardware.check_printer() is False


def test_sin_libusb_cuenta_como_desconectada(monkeypatch, usb_ids):
    def find(**kw):
        raise usb.core.NoBackendError("no backend")
    monkeypatch.setattr(usb.core, "find", find)
    assert hardware.check_printer() is False


def test_imprime_corta_y_cierra(monkeypatch):
    fake = MagicMock()
    monkeypatch.setattr(hardware, "get_printer", lambda: fake)
    hardware.print_text("hola")
    fake.text.assert_called_once_with("hola\n")
    fake.cut.assert_called_once()
    fake.close.assert_called_once()


def test_imprime_varias_copias(monkeypatch):
    fake = MagicMock()
    monkeypatch.setattr(hardware, "get_printer", lambda: fake)
    hardware.print_text("hola", copies=3)
    assert fake.text.call_count == 3 and fake.cut.call_count == 3
    fake.close.assert_called_once()


def test_cierra_aunque_falle(monkeypatch):
    fake = MagicMock()
    fake.text.side_effect = RuntimeError("papel")
    monkeypatch.setattr(hardware, "get_printer", lambda: fake)
    try:
        hardware.print_text("hola")
    except RuntimeError:
        pass
    fake.close.assert_called_once()
