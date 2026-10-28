# Plan de Instagram para 3B Technology

Cómo arrancar una cuenta nueva de Instagram para vender el asistente de
WhatsApp con IA para restaurantes, usando Claude Code y skills para producir
y analizar los videos.

---

## 1. Qué vendemos (en una frase)

> "Tu restaurante recibe pedidos por WhatsApp solo: la IA toma el pedido,
> cobra el domicilio, revisa el comprobante de Nequi/Bancolombia y la
> comanda sale impresa en cocina."

Lo que más se puede mostrar en video (sale del código, no es promesa):

| Función | Plan | Por qué funciona en video |
|---|---|---|
| Bot que toma el pedido completo por WhatsApp | Básico | Se ve en pantalla de celular, se entiende en 3 segundos |
| Foto del menú del día → el bot lo lee y lo ofrece | Básico | "Mando una foto y ya" es un gancho fuerte |
| Domicilio cobrado por zona sin humano | Básico | Dolor diario del restaurante |
| Lectura automática del comprobante de transferencia | Avanzado | Ataca el fraude con comprobantes falsos |
| Comanda impresa sola en cocina (Raspberry) | Avanzado | Lo más visual: el ticket saliendo |
| Domiciliarios, liquidación y cierre diario | Premium | Contenido para dueños con varios domiciliarios |
| Reporte en Excel y preguntas tipo "¿cuánto vendí hoy?" | Premium | "Le pregunto a mi WhatsApp cuánto vendí" |

Público: dueños y administradores de restaurantes pequeños y medianos en
Colombia (corrientazo, comida rápida, almuerzos ejecutivos) que hoy
contestan WhatsApp a mano.

Prueba social disponible: **Edy Gourmet** (cliente real). Pedirle permiso
para grabar la cocina, la impresora y un testimonio corto es la pieza más
valiosa del arranque.

---

## 2. Skills instaladas y para qué sirve cada una

Se buscaron con `find-skills`. El buscador de skills.sh está bloqueado en
este entorno de la nube, así que se revisaron directo los repos más
conocidos en GitHub.

### Para hacer los videos
| Skill | Repo | Uso |
|---|---|---|
| `remotion-best-practices` | remotion-dev/skills (oficial) | Guía principal: videos hechos con código (React) |
| `remotion-create` | remotion-dev/skills | Crear un proyecto de video nuevo |
| `remotion-markup` | remotion-dev/skills | Animaciones, textos, efectos |
| `remotion-captions` | remotion-dev/skills | Transcribir y animar subtítulos (clave en Reels: la mayoría se ve sin sonido) |
| `remotion-render` | remotion-dev/skills | Exportar el MP4 vertical 1080×1920 |
| `video` | coreyhaines31/marketingskills | Estrategia de producción con IA (Remotion, HeyGen, etc.) |

### Para estrategia, guiones y análisis
| Skill | Uso |
|---|---|
| `social` | Calendario, formatos de Instagram, ganchos, análisis de cuentas y engagement |
| `content-strategy` | Pilares de contenido y qué publicar |
| `competitor-profiling` | Perfil de competidores a partir de sus URLs |
| `ad-creative` | Ganchos y variantes para Reels y anuncios de Meta |
| `copywriting` | Captions, bio, CTA |
| `marketing-psychology` | Por qué un gancho funciona (o no) |
| `product-marketing` | Documento base de posicionamiento que leen las demás skills |

### Instalarlas en tu computador
Esta sesión corre en un contenedor de la nube que se borra, así que las
skills instaladas aquí **no quedan en tu máquina**. Corre esto en tu
terminal:

```bash
npx skills add https://github.com/vercel-labs/skills --skill find-skills -g
```

```bash
npx skills add https://github.com/remotion-dev/skills --skill remotion-best-practices --skill remotion-create --skill remotion-captions --skill remotion-render --skill remotion-markup -g
```

```bash
npx skills add https://github.com/coreyhaines31/marketingskills --skill social --skill video --skill content-strategy --skill ad-creative --skill competitor-profiling --skill marketing-psychology --skill product-marketing --skill copywriting -g
```

---

## 3. Cómo comparar videos exitosos de Instagram

No hay una skill que descargue métricas de Instagram por sí sola. Tres
caminos, de más simple a más automatizado:

1. **Manual + Claude (empezar aquí).** Elige 10–15 cuentas de referencia
   (software para restaurantes, POS, delivery, creadores de "automatización
   con IA" en español). Por cada Reel destacado anota en una hoja: link,
   vistas, likes, comentarios, duración, gancho (primeros 3 s), formato,
   CTA. Pásale la hoja a Claude con la skill `social` y pide: "¿qué tienen
   en común los Reels con más vistas por seguidor?".
2. **API oficial de Instagram (Graph API, `business_discovery`).** Con una
   cuenta de Instagram Profesional conectada a una página de Facebook se
   pueden leer publicaciones y métricas públicas (likes, comentarios) de
   otras cuentas Business/Creator. Claude puede escribir un script que las
   guarde en un CSV cada semana. No da vistas de Reels ajenos.
3. **Scrapers de terceros (p. ej. Apify).** Traen vistas y transcripciones,
   pero van contra los términos de Instagram y pueden cobrar. Usar con
   cuidado y nunca con la cuenta principal.

La métrica para comparar no son las vistas sueltas sino
**vistas ÷ seguidores de la cuenta** y **comentarios + guardados ÷ vistas**:
así un Reel viral de una cuenta pequeña pesa más que uno normal de una
cuenta grande.

Para tus propios videos: Instagram Insights (cuenta profesional) da
retención, alcance a no seguidores, guardados y compartidos. Revisar cada
viernes.

---

## 4. Plan de arranque (primeras 4 semanas)

### Semana 0 — Montar la cuenta
- Cuenta **profesional** (categoría "Software" o "Servicio de tecnología").
- Nombre: `3B Technology | Pedidos por WhatsApp con IA`.
- Bio: qué hace + para quién + CTA. Ej.: "Tu restaurante vende por WhatsApp
  sin contestar a mano 🍽️🤖 Pedidos, domicilios y comprobantes con IA. 👇
  Prueba gratis".
- Link en bio: WhatsApp de ventas con mensaje prellenado (que el propio bot
  pueda atender = demo en vivo).
- 6–9 publicaciones antes de empezar a promocionar, para que el perfil no
  se vea vacío.
- Correr `product-marketing` una vez para crear el documento de
  posicionamiento que usan las otras skills.

### Semanas 1–4 — Publicar
Ritmo realista: **4 Reels + 2 carruseles por semana + historias diarias**.

Pilares de contenido:
1. **Demo (40 %)** — pantalla del celular: un cliente pide y el bot
   resuelve. Ganchos: "Este restaurante no contestó un solo WhatsApp hoy",
   "Mira lo que pasa cuando mandan un comprobante falso".
2. **Dolor del dueño (25 %)** — humor/situaciones: el administrador con 40
   chats sin leer a las 12:30; el domiciliario que no sabe cuánto cobrar.
3. **Prueba social (20 %)** — Edy Gourmet: la comanda saliendo sola,
   testimonio corto, números ("X pedidos atendidos por el bot esta semana").
4. **Educativo (15 %)** — carruseles: "5 errores al vender por WhatsApp",
   "Cómo evitar comprobantes falsos de Nequi".

Formato de cada Reel: 15–30 s, vertical, gancho en el primer segundo,
subtítulos siempre, CTA final ("Escríbenos 'DEMO' al WhatsApp").

### Promoción
- Primeras 2 semanas: orgánico, para ver qué Reel funciona solo.
- Después: pautar en Meta **solo** el Reel con mejor retención, segmentado
  a dueños de restaurante en tu ciudad, con presupuesto pequeño (usar
  `ad-creative` para variantes del gancho).
- Comentar en cuentas de restaurantes locales y de proveedores del sector.

---

## 5. Flujo de trabajo con Claude para cada video

1. **Idea y guion:** "Con la skill `social`, dame 10 ganchos para un Reel
   del pilar Demo sobre la lectura de comprobantes."
2. **Grabación:** grabar la pantalla del celular con una conversación real
   (o de prueba) con el bot, y tomas reales de la cocina/impresora.
3. **Edición con código:** "Con `remotion-create`, arma un video vertical
   1080×1920 que muestre este chat mensaje por mensaje, con subtítulos
   (`remotion-captions`) y el logo al final." Luego `remotion-render`.
   Alternativa sin código: CapCut con las plantillas de Reels.
4. **Caption y hashtags:** `copywriting` (y el humanizador en español para
   que no suene a IA).
5. **Revisión semanal:** exportar Insights + la hoja de referencia y pedir
   a Claude qué repetir y qué dejar.

Las animaciones de Remotion encajan mejor en: chats animados, números
("$0 en comprobantes falsos"), comparaciones antes/después. Las tomas
reales (cocina, impresora, la encargada) siguen siendo lo que más
confianza genera.

---

## 6. Qué medir

| Métrica | Meta del primer mes |
|---|---|
| Retención a 3 s de los Reels | > 60 % |
| Vistas de no seguidores | > 70 % del alcance |
| Guardados + compartidos | subir semana a semana |
| Mensajes al WhatsApp de ventas | la métrica que importa |
| Demos agendadas / clientes nuevos | 1–3 restaurantes de prueba |
