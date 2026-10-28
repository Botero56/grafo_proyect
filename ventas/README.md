# Ventas: buscar restaurantes para el bot de WhatsApp

| Archivo | Para qué |
|---|---|
| `seguimiento_clientes.xlsx` | Hoja de seguimiento con 101 restaurantes de Medellín y Bello (teléfono, enlace a WhatsApp, estado, fechas, plan) y un resumen que se calcula solo. |
| `datos/leads.tsv` | Los mismos restaurantes en texto plano, con la fuente de cada uno. |
| `buscar_restaurantes.py` | Saca de OpenStreetMap los restaurantes de Medellín y Bello (u otras ciudades) a un CSV, primero los que tienen celular (WhatsApp probable). Gratis y sin cuenta. |
| `mensajes_whatsapp.md` | Primer mensaje, seguimiento y respuestas a objeciones, para enviar uno por uno. |
| `harvey/harvey.local.yaml` | Configuración de [Harvey](https://github.com/ethanplusai/harvey) ya ajustada al producto, por si quieren prospectar por correo. |

## Paso a paso

```bash
python3 ventas/buscar_restaurantes.py --solo-contactables --salida restaurantes_medellin_bello.csv
```

Abra el CSV en Excel o Google Sheets y trabájelo de arriba hacia abajo: los que
tienen `whatsapp` reciben el mensaje 1, los que solo tienen `instagram` el
mensaje 2 por DM, los que tienen un fijo se llaman. Agregue una columna de
estado (enviado, respondió, demo, cliente, no).

Para no quemar el número de ventas: 30 a 40 mensajes al día, escritos a mano o
por lo menos enviados uno a uno, y nada de listas de difusión a gente que no lo
tiene guardado.

## Harvey

Revisado el 2026-10-28 (commit `4237f07`). No encontramos nada malicioso: solo
se conecta a las fuentes de búsqueda, al correo que uno configure y a
api.anthropic.com para leer cuánto queda de la suscripción. Pero para este
negocio sirve poco:

- Solo envía correo; WhatsApp e Instagram no los maneja.
- Su fuente gratis no busca restaurantes; hay que pagar Scavio o DataForSEO.
- Exige un dominio de correo nuevo y 2 a 4 semanas de calentamiento antes de
  enviar en volumen.
- Las bajas solo las entiende en inglés, así que nunca se debe activar el
  autopiloto: cada correo se aprueba a mano.
- Corre en bucle con la suscripción de Claude y gasta de sus límites.

Si igual lo quieren usar: clonar el repositorio, copiar
`ventas/harvey/harvey.local.yaml` en esa carpeta, llenar todo lo que dice
PENDIENTE y seguir su README (`harvey signals`, `harvey discover`, `harvey run`).
