# Reel de ejemplo (Remotion)

Reel vertical 1080×1920 de 24 s para Instagram, hecho con código, con voz y subtítulos:

1. **Gancho (0–4.8 s):** "Son las 12:30 y tu WhatsApp está así 😩" + 47 chats sin leer.
2. **Demo (4.8–14.8 s):** chat de WhatsApp donde el asistente responde el menú,
   cobra el domicilio y verifica el comprobante.
3. **Comanda (14.8–18.3 s):** el ticket sale solo en la impresora de cocina.
4. **Cierre (18.3–23.9 s):** mensaje + "Escribe DEMO a nuestro WhatsApp".

El video renderizado está en `../videos/reel-ejemplo-pedido-whatsapp.mp4`.

## Usarlo

```bash
npm i
npm run dev          # abre Remotion Studio para verlo y editarlo
npx remotion render ReelPedidoWhatsApp out/reel.mp4
```

## Dónde cambiar cosas

- Textos del chat y tiempos: `src/escenas/EscenaChat.tsx` (`MENSAJES`, `ETIQUETAS`)
- Gancho: `src/escenas/EscenaGancho.tsx`
- Comanda: `src/escenas/EscenaComanda.tsx`
- Marca, restaurante y CTA: `defaultProps` en `src/Root.tsx`
- Colores: `src/theme.ts`

## Voz y subtítulos

La voz es tu voz clonada con [VoiceStudio](https://github.com/debpalash/VoiceStudio)
(app local, gratis) usando el motor **CosyVoice 3** (Apache-2.0, se puede usar
comercialmente). El perfil de voz está en `voz/guion.json` (`perfil_voz`).

1. Abre VoiceStudio.
2. Cambia el texto en `voz/guion.json`. Escribe los números en palabras
   ("doce y media") y evita frases muy cortas: CosyVoice a veces inventa
   palabras en ellas.
3. Genera audio y subtítulos:

```bash
pip install numpy soundfile requests faster-whisper
python voz/generar_voz.py
```

Cada frase se genera, se transcribe con Whisper y se compara con el guion; si
CosyVoice repitió o inventó algo, se reintenta con otra semilla (hasta 4
veces). Las frases buenas quedan en `voz/.cache/` y no se regeneran. En CPU
cada frase tarda 1–3 minutos.

Escribe `public/voz/*.wav` y `src/voz/subtitulos.json` (tiempos reales de cada
palabra). Cada escena se alarga sola si su voz dura más que la animación.

La fuente Inter va incluida en `public/fonts` (licencia SIL OFL), así que el
render no necesita internet.
