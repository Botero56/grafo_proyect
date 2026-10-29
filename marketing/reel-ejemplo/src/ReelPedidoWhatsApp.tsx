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

// Cada escena dura lo que pide su diseño o lo que dura su voz (lo que sea más largo).
const RETRASO = { gancho: 0.2, chat: 0.4, comanda: 0.3, cta: 0.2 } as const;
const MINIMO = { gancho: 4.8, chat: 10, comanda: 3.5, cta: 5.6 } as const;
const COLA = 0.4; // segundos de aire después de la última palabra

export const duracionEscena = (escena: keyof typeof voz, fps: number) =>
  Math.ceil(
    Math.max(MINIMO[escena], RETRASO[escena] + voz[escena].duracionMs / 1000 + COLA) * fps,
  );

export const duracionTotal = (fps: number) =>
  (Object.keys(MINIMO) as (keyof typeof voz)[]).reduce(
    (total, escena) => total + duracionEscena(escena, fps),
    0,
  );

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
        <Series.Sequence name="Gancho" durationInFrames={duracionEscena("gancho", fps)}>
          <EscenaGancho />
          <Voz escena="gancho" retraso={RETRASO.gancho} bottom={200} />
        </Series.Sequence>
        <Series.Sequence name="Chat con el bot" durationInFrames={duracionEscena("chat", fps)}>
          <EscenaChat restaurante={restaurante} />
          <Voz escena="chat" retraso={RETRASO.chat} bottom={180} />
        </Series.Sequence>
        <Series.Sequence name="Comanda en cocina" durationInFrames={duracionEscena("comanda", fps)}>
          <EscenaComanda restaurante={restaurante} />
          <Voz escena="comanda" retraso={RETRASO.comanda} />
        </Series.Sequence>
        <Series.Sequence name="Llamado a la acción" durationInFrames={duracionEscena("cta", fps)}>
          <EscenaCTA marca={marca} cta={cta} />
          <Voz escena="cta" retraso={RETRASO.cta} bottom={230} />
        </Series.Sequence>
      </Series>
    </AbsoluteFill>
  );
};
