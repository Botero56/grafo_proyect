import { useMemo } from "react";
import { createTikTokStyleCaptions, type Caption } from "@remotion/captions";
import {
  AbsoluteFill,
  Sequence,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { colors } from "./theme";

// Cuántos ms caben en una "página" de subtítulo (2–4 palabras).
const AGRUPAR_MS = 900;

type Props = {
  readonly captions: Caption[];
  readonly bottom?: number;
};

// Subtítulos estilo Reels: pocas palabras a la vez y la palabra que suena en amarillo.
export const Subtitulos: React.FC<Props> = ({ captions, bottom = 430 }) => {
  const { fps } = useVideoConfig();
  const { pages } = useMemo(
    () =>
      createTikTokStyleCaptions({
        captions,
        combineTokensWithinMilliseconds: AGRUPAR_MS,
      }),
    [captions],
  );

  return (
    <AbsoluteFill>
      {pages.map((page, i) => {
        const siguiente = pages[i + 1]?.startMs ?? page.startMs + page.durationMs + 400;
        const desde = Math.round((page.startMs / 1000) * fps);
        const hasta = Math.round((siguiente / 1000) * fps);
        return (
          <Sequence
            key={page.startMs}
            name={`Subtítulo: ${page.text.trim()}`}
            from={desde}
            durationInFrames={Math.max(1, hasta - desde)}
          >
            <Pagina tokens={page.tokens} inicioMs={page.startMs} bottom={bottom} />
          </Sequence>
        );
      })}
    </AbsoluteFill>
  );
};

const Pagina: React.FC<{
  readonly tokens: { text: string; fromMs: number; toMs: number }[];
  readonly inicioMs: number;
  readonly bottom: number;
}> = ({ tokens, inicioMs, bottom }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const ahoraMs = inicioMs + (frame / fps) * 1000;
  const entrada = spring({ frame, fps, config: { damping: 200 }, durationInFrames: 6 });

  return (
    <AbsoluteFill
      style={{
        justifyContent: "flex-end",
        alignItems: "center",
        paddingBottom: bottom,
        paddingLeft: 80,
        paddingRight: 80,
      }}
    >
      <div
        style={{
          fontSize: 76,
          fontWeight: 900,
          lineHeight: 1.1,
          textAlign: "center",
          textWrap: "balance",
          color: "white",
          WebkitTextStroke: "14px black",
          paintOrder: "stroke",
          textShadow: "0 6px 18px rgba(0,0,0,0.6)",
          scale: 0.85 + 0.15 * entrada,
        }}
      >
        {tokens.map((t) => {
          const activa = ahoraMs >= t.fromMs && ahoraMs < t.toMs;
          return (
            <span
              key={t.fromMs}
              style={{ color: activa ? colors.amarillo : "white", whiteSpace: "pre" }}
            >
              {t.text}
            </span>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};
