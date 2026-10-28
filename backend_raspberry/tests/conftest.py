import pytest


@pytest.fixture
def usb_ids(monkeypatch):
    monkeypatch.setenv("PRINTER_VENDOR_ID", "0x04b8")
    monkeypatch.setenv("PRINTER_PRODUCT_ID", "0x0202")
