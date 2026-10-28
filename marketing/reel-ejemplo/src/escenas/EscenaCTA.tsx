import {
  AbsoluteFill,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { colors } from "../theme";

export const EscenaCTA: React.FC<{ readonly marca: string; readonly cta: string }> = ({
  marca,
  cta,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const entrada = (retraso: number) =>
    spring({ frame: frame - retraso * fps, fps, config: { damping: 200 } });

  return (
    <AbsoluteFill
      style={{
        background: `linear-gradient(160deg, ${colors.verdeOscuro}, ${colors.fondo} 70%)`,
        alignItems: "center",
        justifyContent: "center",
        padding: "0 80px",
        gap: 60,
        textAlign: "center",
      }}
    >
      <div
        style={{
          color: "white",
          fontSize: 96,
          fontWeight: 900,
          textWrap: "balance",
          lineHeight: 1.05,
          opacity: entrada(0),
          translate: `0px ${(1 - entrada(0)) * 50}px`,
        }}
      >
        Tu restaurante vende por WhatsApp. Tú cocinas.
      </div>
      <div
        style={{
          color: colors.texto,
          fontSize: 48,
          textWrap: "balance",
          opacity: entrada(0.5),
        }}
      >
        Pedidos, domicilios y comprobantes con IA
      </div>
      <div
        style={{
          backgroundColor: colors.verde,
          color: colors.fondo,
          fontSize: 54,
          fontWeight: 900,
          textWrap: "balance",
          borderRadius: 80,
          padding: "30px 56px",
          opacity: entrada(1),
          scale: interpolate(entrada(1), [0, 1], [0.8, 1]) *
            interpolate(frame % 30, [0, 15, 30], [1, 1.04, 1]),
        }}
      >
        💬 {cta}
      </div>
      <div style={{ color: colors.gris, fontSize: 44, fontWeight: 800, opacity: entrada(1.4) }}>
        {marca}
      </div>
    </AbsoluteFill>
  );
};
