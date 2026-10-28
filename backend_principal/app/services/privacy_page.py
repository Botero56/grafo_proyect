"""Página pública de la política de datos de cada negocio:
GET /privacidad/{business_id} en el servicio de webhook.

Liviano a propósito (ver tests/test_service_boundaries.py): Firestore en
crudo y con import perezoso, como tenants.py; nada de fireo ni del agente.
La autorización de los clientes vive en privacy.py (worker).
"""

import html
import os
import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool

from app.config import whatsapp_settings
from app.services import tenants

# Cambiar el texto de la política = subir la versión: a cada cliente se le
# vuelve a pedir la autorización en su siguiente pedido.
POLICY_VERSION = "2026-10-25"
PROCESSOR = "3B Technology"
CONTACT_EMAIL = os.environ.get("PRIVACY_CONTACT_EMAIL", "boterosamuel3@gmail.com")

_ID = re.compile(r"[A-Za-z0-9]{1,64}")

router = APIRouter()


def policy_url(business_id: str) -> str:
    return f"{whatsapp_settings.WEBHOOK_PUBLIC_BASE_URL.rstrip('/')}/privacidad/{business_id}"


def _load(business_id: str):
    from google.cloud.firestore_v1.base_query import FieldFilter

    db = tenants._firestore()
    business = db.collection("businesses").document(business_id).get(timeout=5)
    if not business.exists:
        return None, None
    configs = db.collection("configurations").where(filter=FieldFilter("business_id", "==", business_id)).limit(1).get(timeout=5)
    return business.to_dict() or {}, (configs[0].to_dict() or {}) if configs else {}


@router.get("/privacidad/{business_id}", response_class=HTMLResponse)
async def privacy_page(business_id: str) -> HTMLResponse:
    if not _ID.fullmatch(business_id):
        raise HTTPException(status_code=404, detail="No encontrado")
    business, configuration = await run_in_threadpool(_load, business_id)
    if business is None:
        raise HTTPException(status_code=404, detail="No encontrado")
    return HTMLResponse(render_policy(business, configuration))


def render_policy(business: dict, configuration: dict) -> str:
    e = lambda v: html.escape(str(v or "").strip())  # noqa: E731
    name = e(configuration.get("restaurant_name")) or e(business.get("name")) or "el negocio"
    legal = e(business.get("legal_name")) or name
    phone, address = e(business.get("phone")), e(business.get("address"))
    contact = " · ".join(x for x in (phone and f"WhatsApp o teléfono {phone}", address) if x)
    return _PAGE.format(
        name=name, legal=legal, contact=contact or "su número de WhatsApp",
        processor=PROCESSOR, email=CONTACT_EMAIL, version=POLICY_VERSION,
    )


_PAGE = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Política de datos · {name}</title>
<style>
  :root {{ --bg:#f6f7f8; --text:#1c232b; --muted:#5b6672; --line:#dde2e7; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --bg:#11161b; --text:#e6ebef; --muted:#9aa6b1; --line:#2a333c; }} }}
  body {{ margin:0; background:var(--bg); color:var(--text); font:16px/1.6 system-ui, sans-serif; }}
  main {{ max-width:680px; margin:0 auto; padding:32px 16px 64px; }}
  h1 {{ font-size:1.5rem; line-height:1.3; margin:0 0 4px; }}
  h2 {{ font-size:1.05rem; margin:28px 0 6px; }}
  p, li {{ color:var(--text); }}
  .meta {{ color:var(--muted); font-size:.9rem; margin:0 0 24px; padding-bottom:16px; border-bottom:1px solid var(--line); }}
  a {{ color:inherit; }}
</style>
</head>
<body>
<main>
<h1>Política de tratamiento de datos personales de {name}</h1>
<p class="meta">Versión {version}. Ley 1581 de 2012 y Decreto 1377 de 2013 (Colombia).</p>

<h2>Quién es responsable de tus datos</h2>
<p><strong>{legal}</strong> ({name}) es el responsable de tus datos. Contacto: {contact}.
{processor} opera el asistente de WhatsApp por encargo de {name} y trata los datos solo según sus instrucciones.</p>

<h2>Qué datos guardamos</h2>
<ul>
<li>Tu nombre, tu número o usuario de WhatsApp y el teléfono que nos des.</li>
<li>La dirección de entrega y las indicaciones para llegar.</li>
<li>Los mensajes de la conversación, tus pedidos y su historial.</li>
<li>Si pagas por transferencia, la foto del comprobante.</li>
</ul>
<p>No solicitamos ni tratamos datos sensibles (salud, biometría, etc.) ni datos de menores de edad.</p>

<h2>Para qué los usamos</h2>
<ul>
<li>Tomar, preparar, cobrar y entregar tus pedidos.</li>
<li>Avisarte sobre tu pedido (tiempos de entrega, novedades, pagos).</li>
<li>Recordar tu dirección y tus pedidos para atenderte más rápido.</li>
</ul>
<p>No vendemos tus datos ni los usamos para publicidad. Si algún día quisiéramos enviarte promociones, te pediremos una autorización aparte.</p>

<h2>Con quién se comparten</h2>
<p>Solo con los proveedores que hacen funcionar el servicio: WhatsApp (Meta) y Kapso para los mensajes, Google Cloud para guardar la información y Google Gemini, que procesa los mensajes para responderte.
Algunos de estos servicios guardan datos fuera de Colombia, con medidas de seguridad. El repartidor solo recibe lo necesario para entregar tu pedido.</p>

<h2>Cómo protegemos tu información</h2>
<p>Implementamos medidas técnicas y administrativas razonables para evitar la adulteración, pérdida o acceso no autorizado a tu información.</p>

<h2>Cuánto tiempo los guardamos</h2>
<p>Mientras sigas siendo cliente de {name}, y luego el tiempo que exijan las normas contables y tributarias.</p>

<h2>Tus derechos</h2>
<p>Puedes conocer, actualizar, corregir y pedir que borremos tus datos, y retirar tu autorización en cualquier momento, salvo los datos que la ley nos obligue a conservar.
También puedes presentar una queja ante la Superintendencia de Industria y Comercio.</p>

<h2>Cómo ejercerlos</h2>
<p>Escríbele a {name} por este mismo WhatsApp, o al correo <a href="mailto:{email}">{email}</a>.
Respondemos las consultas en máximo 10 días hábiles y los reclamos en máximo 15 días hábiles.</p>

<h2>Tu autorización</h2>
<p>Te la pedimos por WhatsApp antes de atenderte, con el botón "Acepto", y queda guardada la fecha en que aceptaste. WhatsApp nos entrega tu primer mensaje antes de que puedas aceptar: lo guardamos solo para enviarte la pregunta, y si tocas "No acepto" lo borramos junto con todo lo demás de la conversación.</p>
</main>
</body>
</html>
"""
