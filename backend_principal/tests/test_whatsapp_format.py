"""WhatsApp usa *un asterisco* para negrita; el **doble** de markdown se le
mostraba literal al cliente (prueba 2026-10-05)."""

from app.ai.core.reply_guard import to_whatsapp_format


def test_markdown_bold_becomes_whatsapp_bold():
    text = "El valor exacto a transferir es **$40.000**.\n• **Menú del día ($18.500):** Pollo al curry"

    assert to_whatsapp_format(text) == (
        "El valor exacto a transferir es *$40.000*.\n• *Menú del día ($18.500):* Pollo al curry"
    )


def test_text_without_markdown_is_unchanged():
    text = "Hola, *ya* tengo tu pedido: 2x Menú del día. Total $40.000"

    assert to_whatsapp_format(text) == text
