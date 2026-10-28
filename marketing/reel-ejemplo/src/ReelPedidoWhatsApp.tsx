import { AbsoluteFill, Series, useVideoConfig } from "remotion";
import { EscenaGancho } from "./escenas/EscenaGancho";
import { EscenaChat } from "./escenas/EscenaChat";
import { EscenaComanda } from "./escenas/EscenaComanda";
import { EscenaCTA } from "./escenas/EscenaCTA";
import { colors, fontFamily } from "./theme";

type Props = {
  readonly marca: string;
  readonly restaurante: string;
  readonly cta: string;
};

// Reel de ejemplo: gancho (dolor) → el bot atiende → comanda impresa → CTA.
export const ReelPedidoWhatsApp: React.FC<Props> = ({
  marca,
  restaurante,
  cta,
}) => {
  const { fps } = useVideoConfig();

  return (
    <AbsoluteFill style={{ backgroundColor: colors.fondo, fontFamily }}>
      <Series>
        <Series.Sequence name="Gancho" durationInFrames={3.5 * fps}>
          <EscenaGancho />
        </Series.Sequence>
        <Series.Sequence name="Chat con el bot" durationInFrames={10 * fps}>
          <EscenaChat restaurante={restaurante} />
        </Series.Sequence>
        <Series.Sequence name="Comanda en cocina" durationInFrames={3.5 * fps}>
          <EscenaComanda restaurante={restaurante} />
        </Series.Sequence>
        <Series.Sequence name="Llamado a la acción" durationInFrames={4 * fps}>
          <EscenaCTA marca={marca} cta={cta} />
        </Series.Sequence>
      </Series>
    </AbsoluteFill>
  );
};
