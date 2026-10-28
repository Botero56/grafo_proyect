"""Teléfonos en un solo formato: solo dígitos, con indicativo de país, que
es como WhatsApp manda el `from` y como se guarda Customers.phone_number."""

from typing import Optional

# Un celular colombiano escrito sin indicativo (10 dígitos, empieza por 3).
_CO_MOBILE_LEN = 10


def is_username_id(value: Optional[str]) -> bool:
    """BSUID de WhatsApp (ej. "CO.1780159176423859"): el cliente escribe con
    nombre de usuario y Meta no nos da su número. Se guarda en
    Customers.phone_number para responderle (ver WhatsAppClient._send)."""
    return bool(value) and "." in value


def contact_phone(customer) -> Optional[str]:
    """El número para que una persona (domiciliario, admin, comanda) llame o
    escriba al cliente: el que él mismo nos dio si escribe con nombre de
    usuario, o su número de WhatsApp. None si no lo tenemos."""
    if getattr(customer, "contact_phone", None):
        return customer.contact_phone
    phone = getattr(customer, "phone_number", None)
    return None if is_username_id(phone) else phone


def normalize_phone(raw: Optional[str]) -> str:
    """"+57 300 123-4567" / "3001234567" -> "573001234567". Otros países
    deben venir con su indicativo: solo se quitan los símbolos."""
    digits = "".join(c for c in str(raw or "") if c.isdigit())
    if len(digits) == _CO_MOBILE_LEN and digits.startswith("3"):
        digits = "57" + digits
    return digits
