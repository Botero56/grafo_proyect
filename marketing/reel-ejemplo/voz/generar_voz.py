"""Genera la voz del Reel y los tiempos de los subtítulos.

Usa VoiceStudio (app local) con tu voz clonada. Lee voz/guion.json y escribe:
  - public/voz/<escena>.wav           audio de cada escena
  - src/voz/subtitulos.json           palabras con su tiempo (ms desde el inicio de la escena)

Cada frase se genera por separado, así se sabe dónde empieza y termina. Dentro
de la frase, los tiempos de cada palabra salen de transcribir el audio con
Whisper small (local). Si Whisper oye otro número de palabras, se reparten
según el largo.

Uso (con VoiceStudio abierto):
  pip install numpy soundfile requests faster-whisper
  python voz/generar_voz.py
"""

import difflib
import hashlib
import io
import json
import sys
import unicodedata
from pathlib import Path

import numpy as np
import requests
import soundfile as sf
from faster_whisper import WhisperModel

RAIZ = Path(__file__).resolve().parent.parent
API = "http://localhost:3900"
UMBRAL_SILENCIO = 0.01
INTENTOS = 4  # CosyVoice a veces repite o inventa palabras: se reintenta con otra semilla
CACHE = RAIZ / "voz/.cache"  # frases ya aceptadas, para no regenerarlas (es lento en CPU)


def recortar_silencio(audio: np.ndarray, sr: int) -> np.ndarray:
    sonido = np.where(np.abs(audio) > UMBRAL_SILENCIO)[0]
    if len(sonido) == 0:
        return audio
    return audio[max(sonido[0] - sr // 100, 0) : sonido[-1] + sr // 50]


def generar(frase: str, guion: dict, semilla: int) -> tuple[np.ndarray, int]:
    r = requests.post(
        f"{API}/generate",
        data={
            "text": frase,
            "language": "Spanish",
            "profile_id": guion["perfil_voz"],
            "engine": guion["motor"],
            "speed": guion["velocidad"],
            "effect_preset": "broadcast",
            "seed": semilla,
        },
        timeout=1800,
    )
    if r.status_code != 200 or not r.headers.get("content-type", "").startswith("audio"):
        sys.exit(f"VoiceStudio falló ({r.status_code}): {r.text[:500]}")
    audio, sr = sf.read(io.BytesIO(r.content), dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    return audio, sr


_whisper = None


def transcribir(audio: np.ndarray, sr: int, frase: str) -> list[dict]:
    """Palabras oídas, con tiempo (ms), transcritas con Whisper small (local)."""
    global _whisper
    if _whisper is None:
        _whisper = WhisperModel("small", device="cpu", compute_type="int8")
    a16 = np.interp(  # Whisper espera 16 kHz
        np.arange(0, len(audio), sr / 16000), np.arange(len(audio)), audio
    ).astype("float32")
    # el guion como pista hace que escriba "doce" y no "12"
    segs, _ = _whisper.transcribe(
        a16, language="es", word_timestamps=True, initial_prompt=frase
    )
    return [
        {"text": w.word.strip(), "startMs": w.start * 1000, "endMs": w.end * 1000}
        for s in segs
        for w in s.words
    ]


def normalizar(texto: str) -> str:
    sin_tildes = unicodedata.normalize("NFKD", texto.lower()).encode("ascii", "ignore").decode()
    return " ".join("".join(c if c.isalnum() else " " for c in sin_tildes).split())


def parecido(frase: str, oidas: list[dict]) -> float:
    """1.0 = dijo exactamente el guion. Baja si repitió, cambió o inventó palabras."""
    dicho = normalizar(" ".join(p["text"] for p in oidas))
    return difflib.SequenceMatcher(None, normalizar(frase), dicho).ratio()


def recortar_a_palabras(
    audio: np.ndarray, sr: int, oidas: list[dict]
) -> tuple[np.ndarray, list[dict]]:
    """Quita ruido o murmullos antes de la primera palabra y después de la última."""
    if not oidas:
        return audio, oidas
    ini_ms = max(oidas[0]["startMs"] - 80, 0)
    fin_ms = oidas[-1]["endMs"] + 150
    recorte = audio[int(ini_ms * sr / 1000) : int(fin_ms * sr / 1000)]
    movidas = [
        {**p, "startMs": p["startMs"] - ini_ms, "endMs": p["endMs"] - ini_ms}
        for p in oidas
    ]
    return recorte, movidas


def frase_buena(frase: str, guion: dict) -> tuple[np.ndarray, int, list[dict]]:
    """Genera la frase hasta que Whisper oiga lo mismo que dice el guion."""
    clave = hashlib.md5(
        f"{frase}|{guion['perfil_voz']}|{guion['motor']}|{guion['velocidad']}".encode()
    ).hexdigest()[:12]
    cache = CACHE / f"{clave}.wav"
    if cache.exists():
        audio, sr = sf.read(cache, dtype="float32")
        audio, oidas = recortar_a_palabras(audio, sr, transcribir(audio, sr, frase))
        return audio, sr, oidas

    mejor = None
    for semilla in range(1, INTENTOS + 1):
        audio, sr = generar(frase, guion, semilla)
        audio = recortar_silencio(audio, sr)
        audio, oidas = recortar_a_palabras(audio, sr, transcribir(audio, sr, frase))
        nota = parecido(frase, oidas)
        print(f"    intento {semilla}: {nota:.2f} -> {' '.join(p['text'] for p in oidas)!r}")
        if mejor is None or nota > mejor[0]:
            mejor = (nota, audio, sr, oidas)
        if nota >= 0.9 and len(oidas) == len(frase.split()):
            CACHE.mkdir(parents=True, exist_ok=True)
            sf.write(cache, audio, sr)
            break
    else:
        print(f"    OJO: ningún intento quedó exacto, uso el mejor ({mejor[0]:.2f})")
    return mejor[1], mejor[2], mejor[3]


def palabras_con_tiempo(
    frase: str, inicio_ms: float, fin_ms: float, reales: list[dict] | None
) -> list[dict]:
    palabras = frase.split()
    if reales and len(reales) == len(palabras):
        tramos = [(inicio_ms + t["startMs"], inicio_ms + t["endMs"]) for t in reales]
    else:
        # ponytail: reparto por largo si Whisper oye otro número de palabras
        pesos = [len(p) + 2 for p in palabras]
        total, t, tramos = sum(pesos), inicio_ms, []
        for peso in pesos:
            dur = (fin_ms - inicio_ms) * peso / total
            tramos.append((t, t + dur))
            t += dur
    salida = [
        {
            "text": palabra if i == 0 else f" {palabra}",
            "startMs": round(a),
            "endMs": round(b),
            "timestampMs": round((a + b) / 2),
            "confidence": None,
        }
        for i, (palabra, (a, b)) in enumerate(zip(palabras, tramos))
    ]
    salida[-1]["pageBreakAfter"] = True  # cada frase arranca su propio subtítulo
    return salida


def main() -> None:
    guion = json.loads((RAIZ / "voz/guion.json").read_text(encoding="utf-8"))
    (RAIZ / "public/voz").mkdir(parents=True, exist_ok=True)
    (RAIZ / "src/voz").mkdir(parents=True, exist_ok=True)

    resultado = {}
    for escena in guion["escenas"]:
        partes, subtitulos, sr = [], [], 24000
        t_ms = 0.0
        for frase in escena["frases"]:
            print(f"  {frase!r}")
            audio, sr, reales = frase_buena(frase, guion)
            dur_ms = len(audio) / sr * 1000
            subtitulos += palabras_con_tiempo(frase, t_ms, t_ms + dur_ms, reales)
            print(f"    {dur_ms:.0f} ms, tiempos {'reales' if len(reales) == len(frase.split()) else 'calculados'}")
            pausa = np.zeros(int(sr * guion["pausa_entre_frases_ms"] / 1000), dtype="float32")
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
    main()
