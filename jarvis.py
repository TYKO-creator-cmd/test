#!/usr/bin/env python3
"""
Jarvis : écoute le micro et, au double clap, ouvre le bureau (Claude, Gmail, Agenda,
WhatsApp) et dit une phrase de bienvenue avec la voix ElevenLabs.

Lancer (Windows) :
  .venv\\Scripts\\python jarvis.py

Réglages principaux (constantes ci-dessous) :
  MIN_RMS            — niveau minimum d'un clap (plancher absolu).
  CLAP_DECAY_MS      — au bout de ce délai, le son doit être retombé.
  CLAP_DECAY_RATIO   — ... sous cette fraction du pic (sinon : voix ou musique).
  MIN/MAX_DOUBLE_GAP_S — écart permis entre les deux claps.

Mode debug : définir JARVIS_DEBUG=1 (dans .env ou le terminal) pour voir, à chaque pic,
le niveau mesuré, le seuil et le verdict (clap / voix).
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
import threading
import time
import wave
import webbrowser
from pathlib import Path

from dotenv import load_dotenv
import numpy as np
import sounddevice as sd

# --- détection du clap ------------------------------------------------------
BLOCK_MS = 20
CHANNELS = 1

SPIKE_RATIO = 7.0  # un pic doit dépasser N x le bruit de fond...
MIN_RMS = 0.28  # ...et ce plancher absolu
CLAP_DECAY_MS = 100  # un clap s'effondre en moins de 100 ms
CLAP_DECAY_RATIO = 0.35  # niveau après 100 ms < 35 % du pic => clap
MIN_DOUBLE_GAP_S = 0.12
MAX_DOUBLE_GAP_S = 0.35
COOLDOWN_S = 0.45
RETRIGGER_RATIO = 0.55
NOISE_FLOOR_ALPHA = 0.992
QUIET_GATE_MULT = 2.2  # le bruit de fond n'évolue que sous floor * cette valeur

# Test du micro au démarrage : si le micro par défaut est muet, on cherche un autre.
INPUT_PROBE_S = 0.5
INPUT_SILENT_RMS = 0.001

# --- ce qui s'ouvre au double clap ------------------------------------------
OPEN_URLS = [
    "https://claude.ai/new",
    "https://mail.google.com",
    "https://calendar.google.com",
]
# WhatsApp : application Windows (protocole "whatsapp:"), sinon version web.
WHATSAPP_APP_URI = "whatsapp:"
WHATSAPP_WEB_URL = "https://web.whatsapp.com"
OPEN_DELAY_S = 0.6  # petite pause entre deux ouvertures

# --- voix -------------------------------------------------------------------
JARVIS_WELCOME_ENABLED = True
JARVIS_WELCOME_PHRASE = (
    "Bonjour, bienvenue. Je lance ton bureau, et également les notifications "
    "importantes, comme les mails non répondus ou les messages."
)
JARVIS_WELCOME_CACHE_ENABLED = True

load_dotenv(Path(__file__).resolve().parent / ".env")

DEBUG = (os.environ.get("JARVIS_DEBUG") or "").strip().lower() in {"1", "true", "yes", "on"}

logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("jarvis")


# --- détecteur (logique pure, testable sans micro) --------------------------
class ClapDetector:
    """Reçoit un niveau RMS par bloc ; renvoie True quand un double clap est confirmé.

    Un pic au-dessus du seuil n'est pas validé tout de suite : on attend CLAP_DECAY_MS
    et on vérifie que le niveau est retombé sous CLAP_DECAY_RATIO x pic. Une voix ou
    de la musique restent forts, un clap non.
    """

    def __init__(self) -> None:
        self.noise_floor = 1e-4
        self.armed = True
        self.pending_peak: float | None = None
        self.pending_t = 0.0
        self.first_clap_t: float | None = None
        self.last_double_t = -1e9

    def threshold(self) -> float:
        return max(self.noise_floor * SPIKE_RATIO, MIN_RMS)

    def feed(self, level: float, now: float) -> bool:
        if level < self.noise_floor * QUIET_GATE_MULT:
            self.noise_floor = max(
                NOISE_FLOOR_ALPHA * self.noise_floor + (1 - NOISE_FLOOR_ALPHA) * level,
                1e-7,
            )
        threshold = self.threshold()

        # Pic en cours de vérification : on attend la fin du délai.
        if self.pending_peak is not None:
            self.pending_peak = max(self.pending_peak, level)
            if now - self.pending_t < CLAP_DECAY_MS / 1000:
                return False
            peak, t0 = self.pending_peak, self.pending_t
            self.pending_peak = None
            is_clap = level < CLAP_DECAY_RATIO * peak
            if DEBUG:
                log.debug(
                    "pic=%.3f  seuil=%.3f  apres %d ms=%.3f (limite %.3f)  -> %s",
                    peak,
                    threshold,
                    CLAP_DECAY_MS,
                    level,
                    CLAP_DECAY_RATIO * peak,
                    "CLAP" if is_clap else "voix/musique, ignore",
                )
            if not is_clap:
                return False
            return self._register_clap(t0)

        if level < threshold * RETRIGGER_RATIO:
            self.armed = True

        if self.armed and level >= threshold and (now - self.last_double_t) >= COOLDOWN_S:
            self.armed = False
            self.pending_peak = level
            self.pending_t = now
        return False

    def _register_clap(self, t0: float) -> bool:
        if self.first_clap_t is None:
            self.first_clap_t = t0
            return False
        gap = t0 - self.first_clap_t
        if gap < MIN_DOUBLE_GAP_S:
            return False  # écho du même clap
        if gap <= MAX_DOUBLE_GAP_S:
            self.first_clap_t = None
            self.last_double_t = t0
            log.info("Double clap détecté (écart %.2f s)", gap)
            return True
        self.first_clap_t = t0  # trop lent : ce clap devient le premier
        return False


# --- micro ------------------------------------------------------------------
def rms_mono(block: np.ndarray) -> float:
    if block.ndim > 1:
        block = np.mean(block.astype(np.float64), axis=1)
    else:
        block = block.astype(np.float64)
    if block.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(block**2)))


def _input_devices() -> list[tuple[int, dict]]:
    return [
        (i, dev)
        for i, dev in enumerate(sd.query_devices())
        if dev["max_input_channels"] >= 1
    ]


def _device_rate(device: int) -> int:
    """Fréquence native du micro (44100 ou 48000 selon la machine)."""
    override = (os.environ.get("JARVIS_SAMPLE_RATE") or "").strip()
    if override.isdigit():
        return int(override)
    return int(sd.query_devices(device)["default_samplerate"])


def _blocksize(rate: int) -> int:
    return max(int(rate * BLOCK_MS / 1000), 1)


def _resolve_input_device_index(spec: str) -> int:
    spec = spec.strip()
    if spec.isdigit():
        idx = int(spec)
        sd.query_devices(idx)
        return idx
    needle = spec.lower()
    for idx, dev in _input_devices():
        if needle in dev["name"].lower():
            return idx
    raise ValueError(f"Aucun micro ne correspond à {spec!r}")


def _probe_input_max_rms(device: int) -> float | None:
    rate = _device_rate(device)
    bs = _blocksize(rate)
    try:
        with sd.InputStream(
            device=device, samplerate=rate, channels=CHANNELS, dtype="float32", blocksize=bs
        ) as stream:
            peak = 0.0
            deadline = time.monotonic() + INPUT_PROBE_S
            while time.monotonic() < deadline:
                data, _ = stream.read(bs)
                peak = max(peak, rms_mono(data))
            return peak
    except sd.PortAudioError:
        return None


def _choose_input_device() -> int:
    log.info("Micros détectés :\n%s", sd.query_devices())

    override = (os.environ.get("JARVIS_INPUT_DEVICE") or "").strip()
    if override:
        try:
            idx = _resolve_input_device_index(override)
        except ValueError as e:
            log.error("%s", e)
            log.error("JARVIS_INPUT_DEVICE doit être un numéro ou un bout du nom du micro.")
            raise SystemExit(1) from e
        log.info("Micro choisi (JARVIS_INPUT_DEVICE) [%d] : %s", idx, sd.query_devices(idx)["name"])
        return idx

    default = sd.default.device[0]
    if default is not None and default >= 0:
        peak = _probe_input_max_rms(default)
        if peak is not None and peak >= INPUT_SILENT_RMS:
            log.info("Micro par défaut [%d] : %s", default, sd.query_devices(default)["name"])
            return default
        log.warning("Le micro par défaut est muet ou indisponible, je cherche un autre micro...")

    best_idx: int | None = None
    best_peak = -1.0
    for idx, _dev in _input_devices():
        if default is not None and idx == default:
            continue
        peak = _probe_input_max_rms(idx)
        if peak is not None and peak > best_peak:
            best_peak, best_idx = peak, idx
    if best_idx is not None and best_peak >= INPUT_SILENT_RMS:
        log.info("Micro choisi automatiquement [%d] : %s", best_idx, sd.query_devices(best_idx)["name"])
        return best_idx

    if default is not None and default >= 0:
        return default
    inputs = _input_devices()
    if not inputs:
        log.error("Aucun micro trouvé.")
        raise SystemExit(1)
    return inputs[0][0]


# --- voix ElevenLabs --------------------------------------------------------
def _elevenlabs_pcm_sample_rate(output_format: str) -> int:
    override = (os.environ.get("ELEVENLABS_PCM_SAMPLE_RATE") or "").strip()
    if override.isdigit():
        return int(override)
    if output_format.startswith("pcm_"):
        try:
            return int(output_format.split("_", maxsplit=1)[1])
        except (ValueError, IndexError):
            pass
    return 24000


def elevenlabs_env_config() -> tuple[str, str, str, int]:
    """voice_id, model_id, output_format, pcm_sample_rate."""
    voice = (os.environ.get("ELEVENLABS_VOICE_ID") or "").strip()
    model = (os.environ.get("ELEVENLABS_MODEL_ID") or "eleven_multilingual_v2").strip()
    fmt = (os.environ.get("ELEVENLABS_OUTPUT_FORMAT") or "pcm_24000").strip()
    return voice, model, fmt, _elevenlabs_pcm_sample_rate(fmt)


def _welcome_cache_path(text: str, voice_id: str, model_id: str, output_format: str) -> Path:
    base = Path(__file__).resolve().parent / ".cache" / "jarvis_welcome"
    override = (os.environ.get("JARVIS_WELCOME_CACHE_DIR") or "").strip()
    if override:
        base = Path(override).expanduser().resolve()
    digest = hashlib.sha256(f"{text}|{voice_id}|{model_id}|{output_format}".encode()).hexdigest()[:24]
    return base / f"{digest}.wav"


def _play_pcm_wav_file(path: Path) -> bool:
    try:
        with wave.open(str(path), "rb") as wf:
            if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
                return False
            rate = wf.getframerate()
            raw = wf.readframes(wf.getnframes())
    except (OSError, wave.Error) as e:
        log.warning("Lecture du cache impossible : %s", e)
        return False
    if not raw:
        return False
    try:
        sd.play(np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0, rate)
        sd.wait()
    except Exception as e:  # noqa: BLE001
        log.warning("Lecture audio impossible : %s", e)
        return False
    return True


def _save_pcm_wav_file(path: Path, pcm_bytes: bytes, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with wave.open(str(tmp), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    tmp.replace(path)


def say_jarvis_welcome() -> None:
    text = JARVIS_WELCOME_PHRASE.strip()
    if not JARVIS_WELCOME_ENABLED or not text:
        return
    vid, model_id, output_format, pcm_rate = elevenlabs_env_config()
    if not vid:
        log.warning("ELEVENLABS_VOICE_ID manquant dans .env : pas de voix.")
        return

    cache_path = _welcome_cache_path(text, vid, model_id, output_format)
    if JARVIS_WELCOME_CACHE_ENABLED and cache_path.is_file():
        if _play_pcm_wav_file(cache_path):
            return

    api_key = (os.environ.get("ELEVENLABS_API_KEY") or "").strip()
    if not api_key:
        log.warning("ELEVENLABS_API_KEY manquant dans .env : pas de voix.")
        return
    try:
        from elevenlabs.client import ElevenLabs

        client = ElevenLabs(api_key=api_key)
        raw = b"".join(
            client.text_to_speech.convert(
                voice_id=vid, text=text, model_id=model_id, output_format=output_format
            )
        )
    except Exception as e:  # noqa: BLE001
        log.warning(
            "ElevenLabs a refusé la demande : %s\n"
            "(Erreur 402 = voix de la Voice Library non autorisée sur le plan gratuit : "
            "choisis une voix native.)",
            e,
        )
        return
    if not raw:
        log.warning("ElevenLabs a renvoyé un audio vide.")
        return
    if JARVIS_WELCOME_CACHE_ENABLED:
        try:
            _save_pcm_wav_file(cache_path, raw, pcm_rate)
        except OSError as e:
            log.warning("Cache impossible : %s", e)
    try:
        sd.play(np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0, pcm_rate)
        sd.wait()
    except Exception as e:  # noqa: BLE001
        log.warning("Lecture audio impossible : %s", e)


# --- ouverture du bureau ----------------------------------------------------
def open_whatsapp() -> None:
    if sys.platform == "win32":
        try:
            os.startfile(WHATSAPP_APP_URI)  # type: ignore[attr-defined]
            return
        except OSError as e:
            log.warning("Application WhatsApp introuvable (%s), ouverture de la version web.", e)
    webbrowser.open_new_tab(WHATSAPP_WEB_URL)


def run_double_clap_actions() -> None:
    """Tourne dans un thread à part pour ne pas bloquer l'écoute du micro."""
    if JARVIS_WELCOME_ENABLED and JARVIS_WELCOME_PHRASE.strip():
        threading.Thread(target=say_jarvis_welcome, daemon=True).start()
    for url in OPEN_URLS:
        webbrowser.open_new_tab(url)
        time.sleep(OPEN_DELAY_S)
    open_whatsapp()


# --- boucle principale ------------------------------------------------------
def main() -> int:
    input_idx = _choose_input_device()
    rate = _device_rate(input_idx)
    bs = _blocksize(rate)
    detector = ClapDetector()
    sequence_done = False

    log.info(
        "J'écoute (micro à %d Hz). Fais deux claps espacés de %.2f à %.2f s. Ctrl+C pour arrêter.",
        rate,
        MIN_DOUBLE_GAP_S,
        MAX_DOUBLE_GAP_S,
    )
    if DEBUG:
        log.info("Mode debug actif : niveau et seuil affichés à chaque pic.")

    try:
        with sd.InputStream(
            device=input_idx, samplerate=rate, channels=CHANNELS, dtype="float32", blocksize=bs
        ) as stream:
            while True:
                data, overflowed = stream.read(bs)
                if overflowed:
                    log.warning("Débordement du micro")
                level = rms_mono(data)
                if detector.feed(level, time.monotonic()) and not sequence_done:
                    sequence_done = True  # une seule fois par lancement
                    threading.Thread(target=run_double_clap_actions, daemon=True).start()
    except KeyboardInterrupt:
        log.info("Arrêté.")
        return 0
    except sd.PortAudioError as e:
        log.error("Erreur audio : %s", e)
        log.error("Essaie un autre micro avec JARVIS_INPUT_DEVICE ou JARVIS_SAMPLE_RATE (.env).")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
