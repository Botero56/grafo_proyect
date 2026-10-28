import {
  AbsoluteFill,
  Easing,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { colors } from "../theme";

type Mensaje = {
  readonly de: "cliente" | "bot";
  readonly texto: string;
  readonly segundo: number; // cuándo aparece dentro de la escena
  readonly hora: string;
};

const MENSAJES: Mensaje[] = [
  { de: "cliente", texto: "Hola! ¿Qué hay de almuerzo hoy?", segundo: 0.6, hora: "12:31" },
  {
    de: "bot",
    texto: "¡Hola Laura! 👋 Hoy hay Bandeja paisa o Pechuga gratinada, con sopa y jugo. $18.000 🍲",
    segundo: 1.8,
    hora: "12:31",
  },
  { de: "cliente", texto: "2 bandejas pa la Cl. 45 #12-30", segundo: 3.4, hora: "12:32" },
  {
    de: "bot",
    texto: "Listo ✅\n2× Bandeja paisa $36.000\nDomicilio $4.000\nTotal: $40.000\n¿Efectivo o transferencia?",
    segundo: 4.6,
    hora: "12:32",
  },
  { de: "cliente", texto: "📎 comprobante_nequi.jpg", segundo: 6.4, hora: "12:33" },
  {
    de: "bot",
    texto: "Pago de $40.000 verificado ✅ Tu pedido ya está en cocina 🛵",
    segundo: 7.6,
    hora: "12:33",
  },
];

// Lo que el espectador debe notar arriba, cambia según avanza el chat.
const ETIQUETAS = [
  { desde: 0, texto: "Responde al instante" },
  { desde: 4.4, texto: "Cobra el domicilio solo" },
  { desde: 7.4, texto: "Verifica el comprobante" },
];

const ALTO_ESCRIBIENDO = 0.9; // segundos de "escribiendo..." antes de cada respuesta

export const EscenaChat: React.FC<{ readonly restaurante: string }> = ({
  restaurante,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const t = frame / fps;

  const visibles = MENSAJES.filter((m) => t >= m.segundo);
  const siguiente = MENSAJES.find((m) => t < m.segundo);
  const escribiendo =
    siguiente?.de === "bot" && t >= siguiente.segundo - ALTO_ESCRIBIENDO;
  const etiqueta = [...ETIQUETAS].reverse().find((e) => t >= e.desde)!;

  // Sube el chat cuando ya no caben los mensajes.
  const desplazamiento = interpolate(t, [7.4, 8.2], [0, 300], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: Easing.inOut(Easing.cubic),
  });

  return (
    <AbsoluteFill style={{ alignItems: "center", paddingTop: 110 }}>
      <div
        key={etiqueta.texto}
        style={{
          color: colors.amarillo,
          fontSize: 76,
          fontWeight: 900,
          textAlign: "center",
          height: 120,
        }}
      >
        {etiqueta.texto}
      </div>

      {/* Teléfono */}
      <div
        style={{
          marginTop: 30,
          width: 900,
          height: 1560,
          borderRadius: 70,
          border: "14px solid #1f2a30",
          backgroundColor: "#0B141A",
          overflow: "hidden",
          display: "flex",
          flexDirection: "column",
          scale: interpolate(frame, [0, 12], [0.92, 1], {
            extrapolateRight: "clamp",
            easing: Easing.out(Easing.cubic),
          }),
        }}
      >
        <div
          style={{
            backgroundColor: "#202C33",
            padding: "40px 36px 28px",
            display: "flex",
            alignItems: "center",
            gap: 24,
          }}
        >
          <div
            style={{
              width: 84,
              height: 84,
              borderRadius: 42,
              backgroundColor: colors.verdeOscuro,
              color: "white",
              fontSize: 44,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
            }}
          >
            🍽️
          </div>
          <div>
            <div style={{ color: colors.texto, fontSize: 40, fontWeight: 600 }}>
              {restaurante}
            </div>
            <div style={{ color: colors.verde, fontSize: 30 }}>
              {escribiendo ? "escribiendo..." : "en línea"}
            </div>
          </div>
        </div>

        <div style={{ flex: 1, overflow: "hidden", padding: "30px 28px" }}>
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              gap: 22,
              translate: `0px ${-desplazamiento}px`,
            }}
          >
            {visibles.map((m) => (
              <Burbuja key={m.segundo} mensaje={m} />
            ))}
            {escribiendo ? <Escribiendo /> : null}
          </div>
        </div>
      </div>
    </AbsoluteFill>
  );
};

const Burbuja: React.FC<{ readonly mensaje: Mensaje }> = ({ mensaje }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const esBot = mensaje.de === "bot";
  const entrada = spring({
    frame: frame - mensaje.segundo * fps,
    fps,
    config: { damping: 15, stiffness: 180 },
  });

  return (
    <div
      style={{
        alignSelf: esBot ? "flex-start" : "flex-end",
        maxWidth: "82%",
        backgroundColor: esBot ? colors.burbujaBot : colors.burbujaCliente,
        color: colors.texto,
        fontSize: 38,
        lineHeight: 1.3,
        padding: "20px 26px 14px",
        borderRadius: 28,
        whiteSpace: "pre-line",
        opacity: entrada,
        scale: interpolate(entrada, [0, 1], [0.85, 1]),
        transformOrigin: esBot ? "left bottom" : "right bottom",
      }}
    >
      {esBot ? (
        <div style={{ color: colors.verde, fontSize: 26, fontWeight: 800, marginBottom: 4 }}>
          🤖 Asistente IA
        </div>
      ) : null}
      {mensaje.texto}
      <div style={{ color: colors.gris, fontSize: 24, textAlign: "right", marginTop: 6 }}>
        {mensaje.hora} {esBot ? "" : "✓✓"}
      </div>
    </div>
  );
};

const Escribiendo: React.FC = () => {
  const frame = useCurrentFrame();
  return (
    <div
      style={{
        alignSelf: "flex-start",
        backgroundColor: colors.burbujaBot,
        borderRadius: 28,
        padding: "26px 30px",
        display: "flex",
        gap: 12,
      }}
    >
      {[0, 1, 2].map((i) => (
        <div
          key={i}
          style={{
            width: 18,
            height: 18,
            borderRadius: 9,
            backgroundColor: colors.texto,
            opacity: interpolate((frame + i * 5) % 15, [0, 7, 15], [0.3, 1, 0.3]),
          }}
        />
      ))}
    </div>
  );
};
