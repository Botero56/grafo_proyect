import { Audio } from "@remotion/media";
import type { Caption } from "@remotion/captions";
import { AbsoluteFill, Sequence, Series, staticFile, useVideoConfig } from "remotion";
import { EscenaGancho } from "./escenas/EscenaGancho";
import { EscenaChat } from "./escenas/EscenaChat";
import { EscenaComanda } from "./escenas/EscenaComanda";
import { EscenaCTA } from "./escenas/EscenaCTA";
import { Subtitulos } from "./Subtitulos";
import { colors, fontFamily } from "./theme";
// Lo genera voz/generar_voz.py a partir de voz/guion.json.
import voz from "./voz/subtitulos.json";

type Props = {
  readonly marca: string;
  readonly restaurante: string;
  readonly cta: string;
};

// Audio de una escena y sus subtítulos, en la misma secuencia para que se muevan juntos.
const Voz: React.FC<{
  readonly escena: keyof typeof voz;
  readonly retraso: number;
  readonly bottom?: number;
}> = ({ escena, retraso, bottom }) => {
  const { fps } = useVideoConfig();
  return (
    <Sequence name={`Voz: ${escena}`} from={Math.round(retraso * fps)} layout="none">
      <Audio src={staticFile(`voz/${escena}.wav`)} />
      <Subtitulos captions={voz[escena].subtitulos as Caption[]} bottom={bottom} />
    </Sequence>
  );
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
        <Series.Sequence name="Gancho" durationInFrames={4.8 * fps}>
          <EscenaGancho />
          <Voz escena="gancho" retraso={0.2} bottom={200} />
        </Series.Sequence>
        <Series.Sequence name="Chat con el bot" durationInFrames={10 * fps}>
          <EscenaChat restaurante={restaurante} />
          <Voz escena="chat" retraso={0.4} bottom={180} />
        </Series.Sequence>
        <Series.Sequence name="Comanda en cocina" durationInFrames={3.5 * fps}>
          <EscenaComanda restaurante={restaurante} />
          <Voz escena="comanda" retraso={0.3} />
        </Series.Sequence>
        <Series.Sequence name="Llamado a la acción" durationInFrames={5.6 * fps}>
          <EscenaCTA marca={marca} cta={cta} />
          <Voz escena="cta" retraso={0.2} bottom={230} />
        </Series.Sequence>
      </Series>
    </AbsoluteFill>
  );
};
