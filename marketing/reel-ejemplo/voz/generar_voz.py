"""Genera la voz del Reel y los tiempos de los subtítulos.

Usa Kokoro (modelo de voz abierto que corre local). Lee voz/guion.json y escribe:
  - public/voz/<escena>.wav           audio de cada escena
  - src/voz/subtitulos.json           palabras con su tiempo (ms desde el inicio de la escena)

Cada frase se genera por separado, así se sabe dónde empieza y termina; dentro
de la frase el tiempo se reparte entre palabras según su largo.

Uso:
  pip install kokoro-onnx soundfile
  python voz/generar_voz.py <carpeta con kokoro-v1.0.onnx y voices-v1.0.bin>
Modelos: https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.0
"""

import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
from kokoro_onnx import Kokoro

RAIZ = Path(__file__).resolve().parent.parent
UMBRAL_SILENCIO = 0.01


def recortar_silencio(audio: np.ndarray) -> np.ndarray:
    sonido = np.where(np.abs(audio) > UMBRAL_SILENCIO)[0]
    if len(sonido) == 0:
        return audio
    return audio[max(sonido[0] - 200, 0) : sonido[-1] + 400]


def palabras_con_tiempo(frase: str, inicio_ms: float, fin_ms: float) -> list[dict]:
    palabras = frase.split()
    pesos = [len(p) + 2 for p in palabras]
    total = sum(pesos)
    t = inicio_ms
    salida = []
    for i, (palabra, peso) in enumerate(zip(palabras, pesos)):
        dur = (fin_ms - inicio_ms) * peso / total
        salida.append(
            {
                "text": palabra if i == 0 and not salida else f" {palabra}",
                "startMs": round(t),
                "endMs": round(t + dur),
                "timestampMs": round(t + dur / 2),
                "confidence": None,
            }
        )
        t += dur
    salida[0]["text"] = salida[0]["text"].strip()
    salida[-1]["pageBreakAfter"] = True  # cada frase arranca su propio subtítulo
    return salida


def main(carpeta_modelo: str) -> None:
    guion = json.loads((RAIZ / "voz/guion.json").read_text(encoding="utf-8"))
    kokoro = Kokoro(
        f"{carpeta_modelo}/kokoro-v1.0.onnx", f"{carpeta_modelo}/voices-v1.0.bin"
    )
    (RAIZ / "public/voz").mkdir(parents=True, exist_ok=True)
    (RAIZ / "src/voz").mkdir(parents=True, exist_ok=True)

    resultado = {}
    for escena in guion["escenas"]:
        partes, subtitulos, sr = [], [], 24000
        t_ms = 0.0
        for frase in escena["frases"]:
            audio, sr = kokoro.create(
                frase, voice=guion["voz"], speed=guion["velocidad"], lang="es"
            )
            audio = recortar_silencio(audio)
            dur_ms = len(audio) / sr * 1000
            subtitulos += palabras_con_tiempo(frase, t_ms, t_ms + dur_ms)
            pausa = np.zeros(int(sr * guion["pausa_entre_frases_ms"] / 1000))
            partes += [audio, pausa]
            t_ms += dur_ms + guion["pausa_entre_frases_ms"]

        completo = np.concatenate(partes[:-1])
        completo = completo * (0.97 / np.max(np.abs(completo)))  # volumen al máximo sin saturar
        sf.write(RAIZ / f"public/voz/{escena['id']}.wav", completo, sr)
        resultado[escena["id"]] = {
            "duracionMs": round(len(completo) / sr * 1000),
            "subtitulos": subtitulos,
        }
        print(escena["id"], resultado[escena["id"]]["duracionMs"], "ms")

    (RAIZ / "src/voz/subtitulos.json").write_text(
        json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main(sys.argv[1])
