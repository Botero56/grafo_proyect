import {
  AbsoluteFill,
  Easing,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { colors } from "../theme";

const CHATS = [
  { nombre: "Laura", texto: "Hola, ¿hay almuerzo?" },
  { nombre: "Carlos", texto: "¿Cuánto vale el domicilio?" },
  { nombre: "Doña Marta", texto: "Ya transferí, mire" },
  { nombre: "Andrés", texto: "¿Me lo mandan a la 45?" },
  { nombre: "Sofía", texto: "Holaaa ¿siguen abiertos?" },
];

// 0–3.5 s: el dolor. Chats sin leer que se acumulan y un contador que sube.
export const EscenaGancho: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const sinLeer = Math.round(
    interpolate(frame, [0, 2.2 * fps], [3, 47], {
      extrapolateLeft: "clamp",
      extrapolateRight: "clamp",
      easing: Easing.out(Easing.quad),
    }),
  );

  return (
    <AbsoluteFill style={{ padding: "150px 80px", gap: 48, justifyContent: "center" }}>
      <div
        style={{
          color: colors.texto,
          fontSize: 92,
          fontWeight: 900,
          textWrap: "balance",
          lineHeight: 1.05,
          opacity: interpolate(frame, [0, 8], [0, 1], {
            extrapolateRight: "clamp",
          }),
          translate: interpolate(frame, [0, 12], ["0px 40px", "0px 0px"], {
            extrapolateRight: "clamp",
            easing: Easing.out(Easing.cubic),
          }),
        }}
      >
        Son las 12:30 y tu WhatsApp está así 😩
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
        {CHATS.map((chat, i) => {
          const entrada = spring({
            frame: frame - 10 - i * 5,
            fps,
            config: { damping: 14 },
          });
          return (
            <div
              key={chat.nombre}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 24,
                backgroundColor: colors.burbujaCliente,
                borderRadius: 28,
                padding: "22px 28px",
                opacity: entrada,
                translate: `${(1 - entrada) * 120}px 0px`,
              }}
            >
              <div
                style={{
                  width: 76,
                  height: 76,
                  borderRadius: 38,
                  backgroundColor: colors.gris,
                  color: colors.fondo,
                  fontSize: 36,
                  fontWeight: 800,
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                }}
              >
                {chat.nombre[0]}
              </div>
              <div style={{ flex: 1 }}>
                <div style={{ color: colors.texto, fontSize: 38, fontWeight: 600 }}>
                  {chat.nombre}
                </div>
                <div style={{ color: colors.gris, fontSize: 32 }}>{chat.texto}</div>
              </div>
              <div
                style={{
                  backgroundColor: colors.verde,
                  color: colors.fondo,
                  borderRadius: 30,
                  minWidth: 56,
                  height: 56,
                  fontSize: 30,
                  fontWeight: 800,
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                }}
              >
                {i + 2}
              </div>
            </div>
          );
        })}
      </div>

      <div
        style={{
          alignSelf: "center",
          marginTop: 20,
          backgroundColor: colors.rojo,
          color: "white",
          fontSize: 64,
          fontWeight: 900,
          textWrap: "balance",
          borderRadius: 60,
          padding: "18px 48px",
          scale: interpolate(frame % 15, [0, 7, 15], [1, 1.06, 1]),
        }}
      >
        {sinLeer} chats sin leer
      </div>
    </AbsoluteFill>
  );
};
