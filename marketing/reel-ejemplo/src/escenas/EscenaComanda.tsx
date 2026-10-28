import {
  AbsoluteFill,
  Easing,
  interpolate,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { colors } from "../theme";

const LINEAS = [
  "COMANDA #128",
  "--------------------",
  "2x Bandeja paisa",
  "   + sopa y jugo",
  "--------------------",
  "Cliente: Laura",
  "Dir: Calle 45 #12-30",
  "Total: $40.000",
  "PAGO: TRANSFERENCIA ✓",
];

// La comanda sale sola de la impresora de cocina.
export const EscenaComanda: React.FC<{ readonly restaurante: string }> = ({
  restaurante,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  return (
    <AbsoluteFill style={{ alignItems: "center", paddingTop: 150 }}>
      <div
        style={{
          color: colors.texto,
          fontSize: 88,
          fontWeight: 900,
          textWrap: "balance",
          textAlign: "center",
          lineHeight: 1.05,
          padding: "0 80px",
          opacity: interpolate(frame, [0, 10], [0, 1], { extrapolateRight: "clamp" }),
        }}
      >
        …y la comanda <span style={{ color: colors.verde }}>sale sola</span> en cocina
      </div>

      {/* Impresora */}
      <div
        style={{
          marginTop: 90,
          width: 720,
          height: 150,
          backgroundColor: "#2a3942",
          borderRadius: "40px 40px 16px 16px",
          display: "flex",
          alignItems: "flex-end",
          justifyContent: "center",
          zIndex: 2,
        }}
      >
        <div style={{ width: 600, height: 22, backgroundColor: "#111", borderRadius: 11, marginBottom: 20 }} />
      </div>

      {/* Papel saliendo */}
      <div style={{ width: 580, height: 1100, overflow: "hidden", marginTop: -30 }}>
        <div
          style={{
            backgroundColor: "#FAFAF7",
            color: "#111",
            fontFamily: "JetBrains Mono, monospace",
            fontSize: 36,
            lineHeight: 1.5,
            padding: "60px 40px 40px",
            translate: interpolate(frame, [0.3 * fps, 2.4 * fps], ["0px -100%", "0px 0%"], {
              extrapolateLeft: "clamp",
              extrapolateRight: "clamp",
              easing: Easing.out(Easing.quad),
            }),
          }}
        >
          <div style={{ fontWeight: 800, fontSize: 42, textAlign: "center" }}>
            {restaurante.toUpperCase()}
          </div>
          {LINEAS.map((l) => (
            <div key={l} style={{ fontWeight: l.startsWith("PAGO") || l.startsWith("COMANDA") ? 800 : 400 }}>
              {l}
            </div>
          ))}
        </div>
      </div>
    </AbsoluteFill>
  );
};
