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

La voz se genera con [Kokoro](https://github.com/thewh1teagle/kokoro-onnx), un
modelo de voz abierto que corre en tu computador (gratis, sin API key).

1. Cambia el texto en `voz/guion.json`. Escribe los números en palabras
   ("doce y media"). Voces en español: `em_alex` (hombre, la que se usa),
   `ef_dora` (mujer), `em_santa`.
2. Descarga `kokoro-v1.0.onnx` y `voices-v1.0.bin` de
   https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.0
3. Genera audio y subtítulos:

```bash
pip install kokoro-onnx soundfile
python voz/generar_voz.py <carpeta-con-los-modelos>
```

Eso escribe `public/voz/*.wav` y `src/voz/subtitulos.json`. Si la voz de una
escena queda más larga, sube el `durationInFrames` de esa escena en
`src/ReelPedidoWhatsApp.tsx` y el total en `src/Root.tsx`.

Los tiempos de cada palabra se calculan repartiendo la duración de cada frase
según el largo de las palabras, así que pueden ir unas décimas adelantados o
atrasados. Los subtítulos (estilo Reels, palabra activa en amarillo) están en
`src/Subtitulos.tsx`.

Para una voz más natural con acento colombiano, se puede cambiar Kokoro por
ElevenLabs (de pago) generando los mismos archivos `public/voz/<escena>.wav`.

La fuente Inter va incluida en `public/fonts` (licencia SIL OFL), así que el
render no necesita internet.
