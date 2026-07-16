"""Alert sound catalog, synthesis, and playback.

Built-in notification sounds are synthesized to 16-bit PCM WAV files with the
standard library and cached in the app data directory on first use, so no
binary audio assets need to be bundled. A sound id is one of:

* a built-in key (see ``BUILTIN_SOUNDS``) -> a generated, cached WAV,
* ``"none"`` -> silence,
* an absolute path to a user-provided ``.wav`` file.
"""

from __future__ import annotations

import array
import contextlib
import logging
import math
import wave
from pathlib import Path

from PySide6.QtCore import QObject, QUrl

from signaldesk.config import app_data_dir

try:  # QtMultimedia is optional at runtime (e.g. headless CI without audio libs).
    from PySide6.QtMultimedia import QSoundEffect
except Exception:  # pragma: no cover - depends on the platform's Qt build
    QSoundEffect = None  # type: ignore[assignment,misc]

LOGGER = logging.getLogger(__name__)

NONE_ID = "none"
SAMPLE_RATE = 44100
CACHE_VERSION = 2

# The ids pre-date the current collection and are intentionally kept stable so
# existing config files continue to resolve. Labels describe the modernized set.
# id -> human label, in display order.
BUILTIN_SOUNDS: dict[str, str] = {
    "chime": "Bloom",
    "ping": "Glass",
    "bell": "Halo",
    "alert": "Pulse",
    "siren": "Beacon",
    "pop": "Drop",
}


def is_builtin(sound_id: str) -> bool:
    return sound_id in BUILTIN_SOUNDS


def display_name(sound_id: str) -> str:
    if not sound_id or sound_id == NONE_ID:
        return "None"
    if is_builtin(sound_id):
        return BUILTIN_SOUNDS[sound_id]
    return Path(sound_id).name


def selectable_ids() -> list[str]:
    return [*BUILTIN_SOUNDS.keys(), NONE_ID]


def default_sound_dir() -> Path:
    return app_data_dir() / "sounds"


# --- synthesis -------------------------------------------------------------


Partial = tuple[float, float, float]  # frequency ratio, amplitude, decay multiplier
Layer = tuple[float, list[float]]  # start time, samples

SOFT_PARTIALS: tuple[Partial, ...] = (
    (1.0, 1.0, 1.0),
    (2.0, 0.16, 1.8),
    (3.0, 0.045, 2.5),
)
GLASS_PARTIALS: tuple[Partial, ...] = (
    (1.0, 1.0, 1.0),
    (2.01, 0.32, 1.45),
    (3.98, 0.11, 2.1),
    (6.03, 0.035, 3.0),
)
WARM_PARTIALS: tuple[Partial, ...] = (
    (0.5, 0.12, 0.72),
    (1.0, 1.0, 1.0),
    (2.0, 0.1, 1.7),
)


def _voice(
    frequency: float,
    duration: float,
    gain: float,
    *,
    attack: float = 0.008,
    decay: float = 4.5,
    pitch_drop: float = 0.0,
    partials: tuple[Partial, ...] = SOFT_PARTIALS,
) -> list[float]:
    """Create a softly struck tonal voice with independently decaying partials."""
    total = max(1, round(SAMPLE_RATE * duration))
    attack_samples = max(1, round(SAMPLE_RATE * attack))
    release_samples = max(1, min(round(SAMPLE_RATE * 0.035), total // 3))
    partial_norm = sum(amplitude for _, amplitude, _ in partials)
    samples: list[float] = []
    phase = 0.0
    for i in range(total):
        time = i / SAMPLE_RATE
        # A squared sine ramp avoids the click of a linear attack.
        attack_env = (
            math.sin((math.pi / 2) * i / attack_samples) ** 2 if i < attack_samples else 1.0
        )
        remaining = total - 1 - i
        release_env = (
            math.sin((math.pi / 2) * remaining / release_samples) ** 2
            if remaining < release_samples
            else 1.0
        )
        current_frequency = frequency * (1.0 + pitch_drop * math.exp(-18.0 * time))
        phase += math.tau * current_frequency / SAMPLE_RATE
        value = 0.0
        for index, (ratio, amplitude, decay_multiplier) in enumerate(partials):
            partial_env = math.exp(-decay * decay_multiplier * time)
            value += amplitude * partial_env * math.sin(phase * ratio + index * 0.23)
        samples.append(attack_env * release_env * gain * value / partial_norm)
    return samples


def _sweep(
    start_frequency: float,
    end_frequency: float,
    duration: float,
    gain: float,
) -> list[float]:
    """Create a rounded logarithmic pitch sweep for a compact UI accent."""
    total = max(1, round(SAMPLE_RATE * duration))
    attack_samples = max(1, round(SAMPLE_RATE * 0.004))
    release_samples = max(1, min(round(SAMPLE_RATE * 0.045), total // 3))
    samples: list[float] = []
    phase = 0.0
    for i in range(total):
        fraction = i / max(1, total - 1)
        frequency = start_frequency * (end_frequency / start_frequency) ** fraction
        phase += math.tau * frequency / SAMPLE_RATE
        attack_env = min(1.0, i / attack_samples)
        remaining = total - 1 - i
        release_env = min(1.0, remaining / release_samples)
        decay_env = math.exp(-5.5 * i / SAMPLE_RATE)
        tone = math.sin(phase) + 0.12 * math.sin(2.0 * phase + 0.4)
        samples.append(attack_env * release_env * decay_env * gain * tone / 1.12)
    return samples


def _mix(duration: float, *layers: Layer) -> list[float]:
    """Mix sample layers into one fixed-duration mono buffer."""
    mixed = [0.0] * max(1, round(SAMPLE_RATE * duration))
    for start, samples in layers:
        offset = max(0, round(start * SAMPLE_RATE))
        available = len(mixed) - offset
        if available <= 0:
            continue
        for index, sample in enumerate(samples[:available]):
            mixed[offset + index] += sample
    return mixed


def _polish(samples: list[float], target_peak: float = 0.78) -> list[float]:
    """Remove DC, apply edge fades, and set a consistent non-clipping peak."""
    if not samples:
        return samples
    polished: list[float] = []
    previous_input = 0.0
    previous_output = 0.0
    for sample in samples:
        # A light one-pole high-pass removes DC without lifting silent tails.
        output = sample - previous_input + 0.995 * previous_output
        polished.append(output)
        previous_input = sample
        previous_output = output
    fade_samples = min(round(SAMPLE_RATE * 0.006), len(polished) // 2)
    for index in range(fade_samples):
        fade = math.sin((math.pi / 2) * index / max(1, fade_samples)) ** 2
        polished[index] *= fade
        polished[-1 - index] *= fade
    peak = max(abs(sample) for sample in polished)
    if peak:
        scale = target_peak / peak
        polished = [sample * scale for sample in polished]
    return polished


def _bloom() -> list[float]:
    # A compact major arpeggio that opens upward, then leaves a quiet shimmer.
    return _polish(
        _mix(
            0.82,
            (0.00, _voice(523.25, 0.48, 0.28, decay=4.2, pitch_drop=0.006)),
            (0.07, _voice(659.25, 0.5, 0.3, decay=4.0, pitch_drop=0.005)),
            (0.14, _voice(783.99, 0.54, 0.31, decay=3.8, pitch_drop=0.004)),
            (0.22, _voice(1046.5, 0.5, 0.2, decay=4.8, partials=GLASS_PARTIALS)),
            (0.46, _voice(1567.98, 0.26, 0.055, decay=7.0, partials=GLASS_PARTIALS)),
        )
    )


def _glass() -> list[float]:
    # Clear enough for an information event without the shrillness of a single ping.
    return _polish(
        _mix(
            0.55,
            (0.00, _voice(1046.5, 0.38, 0.43, decay=5.8, partials=GLASS_PARTIALS)),
            (0.055, _voice(1567.98, 0.34, 0.25, decay=6.5, partials=GLASS_PARTIALS)),
            (0.22, _voice(1046.5, 0.24, 0.07, decay=7.5, partials=GLASS_PARTIALS)),
        )
    )


def _halo() -> list[float]:
    # A calm suspended fifth with bell-like, slightly inharmonic overtones.
    return _polish(
        _mix(
            0.9,
            (0.00, _voice(440.0, 0.74, 0.19, decay=2.8, partials=WARM_PARTIALS)),
            (0.00, _voice(659.25, 0.8, 0.3, decay=3.0, partials=GLASS_PARTIALS)),
            (0.025, _voice(880.0, 0.72, 0.2, decay=3.8, partials=GLASS_PARTIALS)),
            (0.29, _voice(1318.51, 0.38, 0.055, decay=6.0, partials=GLASS_PARTIALS)),
        ),
        target_peak=0.74,
    )


def _pulse() -> list[float]:
    # Two rounded warning pulses: noticeable, but less fatiguing than three hard beeps.
    first = _mix(
        0.25,
        (
            0.0,
            _voice(
                369.99,
                0.22,
                0.33,
                decay=5.4,
                pitch_drop=0.012,
                partials=WARM_PARTIALS,
            ),
        ),
        (0.0, _voice(554.37, 0.2, 0.24, decay=6.0, pitch_drop=0.009)),
    )
    second = _mix(
        0.28,
        (
            0.0,
            _voice(
                415.3,
                0.24,
                0.35,
                decay=5.1,
                pitch_drop=0.012,
                partials=WARM_PARTIALS,
            ),
        ),
        (0.0, _voice(622.25, 0.22, 0.25, decay=5.8, pitch_drop=0.009)),
    )
    return _polish(_mix(0.65, (0.0, first), (0.28, second)))


def _beacon() -> list[float]:
    # A firm three-part motif carries urgency without imitating a retro siren.
    low_hit = _mix(
        0.23,
        (
            0.0,
            _voice(
                293.66,
                0.2,
                0.36,
                decay=5.6,
                pitch_drop=0.016,
                partials=WARM_PARTIALS,
            ),
        ),
        (0.0, _voice(587.33, 0.19, 0.24, decay=6.2, pitch_drop=0.012)),
    )
    high_hit = _mix(
        0.23,
        (
            0.0,
            _voice(
                349.23,
                0.2,
                0.37,
                decay=5.4,
                pitch_drop=0.016,
                partials=WARM_PARTIALS,
            ),
        ),
        (0.0, _voice(698.46, 0.19, 0.25, decay=6.0, pitch_drop=0.012)),
    )
    return _polish(
        _mix(0.82, (0.0, low_hit), (0.22, high_hit), (0.44, low_hit)),
        target_peak=0.82,
    )


def _drop() -> list[float]:
    # A tactile, unobtrusive accent for users who prefer a very short sound.
    return _polish(
        _mix(
            0.32,
            (0.0, _sweep(520.0, 165.0, 0.27, 0.52)),
            (0.008, _sweep(1040.0, 330.0, 0.18, 0.11)),
            (0.115, _voice(164.81, 0.16, 0.09, decay=7.0, partials=WARM_PARTIALS)),
        ),
        target_peak=0.72,
    )


def _synthesize(sound_id: str) -> list[float]:
    if sound_id == "chime":
        return _bloom()
    if sound_id == "ping":
        return _glass()
    if sound_id == "bell":
        return _halo()
    if sound_id == "alert":
        return _pulse()
    if sound_id == "siren":
        return _beacon()
    if sound_id == "pop":
        return _drop()
    raise KeyError(sound_id)


def _write_wav(path: Path, samples: list[float]) -> None:
    frames = array.array("h")
    for sample in samples:
        clipped = max(-1.0, min(1.0, sample))
        frames.append(int(clipped * 32767))
    temporary = path.with_suffix(".wav.tmp")
    with wave.open(str(temporary), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(frames.tobytes())
    temporary.replace(path)


def generate_builtin(sound_id: str, directory: Path | None = None) -> Path:
    """Return the cached WAV for a built-in sound, generating it if missing."""
    if not is_builtin(sound_id):
        raise KeyError(sound_id)
    target_dir = directory or default_sound_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    # Including the synthesis version makes updated recipes audible immediately
    # instead of silently reusing an older WAV from a previous installation.
    path = target_dir / f"{sound_id}-v{CACHE_VERSION}.wav"
    if not path.exists():
        _write_wav(path, _synthesize(sound_id))
    return path


def resolve_source(sound_id: str, directory: Path | None = None) -> Path | None:
    """Resolve a sound id to a playable WAV path, or None for silence/invalid."""
    if not sound_id or sound_id == NONE_ID:
        return None
    if is_builtin(sound_id):
        try:
            return generate_builtin(sound_id, directory)
        except OSError as exc:
            LOGGER.warning("Could not generate sound %s: %s", sound_id, exc)
            return None
    candidate = Path(sound_id)
    if candidate.suffix.lower() == ".wav" and candidate.is_file():
        return candidate
    return None


# --- playback --------------------------------------------------------------


class SoundPlayer(QObject):
    """Plays alert sounds, caching one QSoundEffect per resolved file."""

    def __init__(self, parent: QObject | None = None, volume: float = 0.6) -> None:
        super().__init__(parent)
        self._volume = volume
        self._effects: dict[str, QSoundEffect] = {}

    def play(self, sound_id: str) -> None:
        path = resolve_source(sound_id)
        if path is None:
            return
        effect = self._effect_for(str(path))
        if effect is not None:
            effect.play()

    def _effect_for(self, path: str):
        if QSoundEffect is None:
            return None
        effect = self._effects.get(path)
        if effect is None:
            with contextlib.suppress(Exception):
                effect = QSoundEffect(self)
                effect.setSource(QUrl.fromLocalFile(path))
                effect.setVolume(self._volume)
                self._effects[path] = effect
        return effect
