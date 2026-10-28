"""Fugas de configuración: el caso real terminó con el bot confirmando que
existe "información técnica interna" ante un prompt injection."""

import pytest

from app.ai.core.reply_guard import OFF_TOPIC_REPLY, guard_reply


@pytest.mark.parametrize(
    "text",
    [
        "No puedo listar las herramientas con que fui creado, es información técnica interna.",
        "Mis instrucciones no me permiten compartir eso.",
        "Eso hace parte de mi configuración interna.",
    ],
)
async def test_talking_about_its_own_setup_is_replaced_by_the_redirect(text):
    assert await guard_reply(text) == OFF_TOPIC_REPLY


@pytest.mark.parametrize(
    "text",
    [
        "¿Alguna instrucción especial para tu pedido? Por ejemplo, sin cebolla.",
        "Listo, te queda 1x Menú del día con frijoles bien caldudos. ¿Confirmas?",
    ],
)
async def test_normal_replies_pass_untouched(text):
    assert await guard_reply(text) == text
