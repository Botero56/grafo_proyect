# Reel de ejemplo (Remotion)

Reel vertical 1080×1920 de 21 s para Instagram, hecho con código:

1. **Gancho (0–3.5 s):** "Son las 12:30 y tu WhatsApp está así 😩" + 47 chats sin leer.
2. **Demo (3.5–13.5 s):** chat de WhatsApp donde el asistente responde el menú,
   cobra el domicilio y verifica el comprobante.
3. **Comanda (13.5–17 s):** el ticket sale solo en la impresora de cocina.
4. **Cierre (17–21 s):** mensaje + "Escribe DEMO a nuestro WhatsApp".

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

La fuente Inter va incluida en `public/fonts` (licencia SIL OFL), así que el
render no necesita internet.
