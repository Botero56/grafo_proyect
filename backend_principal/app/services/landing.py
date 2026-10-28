"""Landing pública mínima ("próximamente"), montada en el servicio de
webhook (app/services/webhook_entry.py) en GET /.

Página estática, sin datos ni llamadas: sirve como sitio web de la app
mientras no exista un front propio.
"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

_PAGE = """<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>3b-technology · Próximamente</title>
  <meta name="description" content="Asistente de WhatsApp para restaurantes. Próximamente.">
  <style>
    :root {
      --bg: #f4f6f8;
      --text: #17202a;
      --muted: #5b6672;
      --brand: #1f8f5f;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #0f1418;
        --text: #e8edf1;
        --muted: #9aa6b1;
        --brand: #2bb07a;
      }
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px 16px;
      background: var(--bg);
      color: var(--text);
      font: 16px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
      text-align: center;
    }
    main { max-width: 520px; }
    .tag {
      display: inline-block;
      margin-bottom: 16px;
      padding: 4px 12px;
      border-radius: 999px;
      background: var(--brand);
      color: #fff;
      font-size: .8rem;
      font-weight: 600;
      letter-spacing: .04em;
      text-transform: uppercase;
    }
    h1 { font-size: clamp(2rem, 6vw, 3rem); margin: 0 0 12px; }
    p { margin: 0; color: var(--muted); }
  </style>
</head>
<body>
  <main>
    <span class="tag">Próximamente</span>
    <h1>Coming soon</h1>
    <p>Estamos construyendo un asistente de WhatsApp para restaurantes. Muy pronto tendremos novedades.</p>
  </main>
</body>
</html>
"""


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def landing() -> HTMLResponse:
    return HTMLResponse(
        _PAGE,
        headers={
            "Cache-Control": "public, max-age=300",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
        },
    )
