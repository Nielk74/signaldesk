import array
import wave

from signaldesk import sounds


def test_generate_builtin_creates_valid_cached_wav(tmp_path) -> None:
    path = sounds.generate_builtin("chime", tmp_path)
    assert path.exists()
    assert path.name == f"chime-v{sounds.CACHE_VERSION}.wav"
    with wave.open(str(path)) as handle:
        assert handle.getframerate() == sounds.SAMPLE_RATE
        assert handle.getnchannels() == 1
        assert handle.getnframes() > 0
    # Second call is cached, returns the same file.
    assert sounds.generate_builtin("chime", tmp_path) == path


def test_resolve_source_builtin_none_and_custom(tmp_path) -> None:
    assert sounds.resolve_source("none", tmp_path) is None
    assert sounds.resolve_source("", tmp_path) is None

    builtin = sounds.resolve_source("ping", tmp_path)
    assert builtin is not None and builtin.exists()

    # A real .wav path resolves to itself.
    custom = sounds.generate_builtin("bell", tmp_path)
    assert sounds.resolve_source(str(custom), tmp_path) == custom

    # Missing file or wrong extension -> silence.
    assert sounds.resolve_source(str(tmp_path / "missing.wav"), tmp_path) is None
    assert sounds.resolve_source(str(tmp_path / "note.txt"), tmp_path) is None


def test_all_builtins_synthesize(tmp_path) -> None:
    payloads = set()
    for sound_id in sounds.BUILTIN_SOUNDS:
        path = sounds.generate_builtin(sound_id, tmp_path)
        with wave.open(str(path)) as handle:
            assert 0.25 * sounds.SAMPLE_RATE <= handle.getnframes() <= sounds.SAMPLE_RATE
            frames = handle.readframes(handle.getnframes())
        samples = array.array("h", frames)
        assert max(abs(sample) for sample in samples) < 32767
        assert abs(samples[0]) < 32
        assert abs(samples[-1]) < 32
        payloads.add(frames)
    assert len(payloads) == len(sounds.BUILTIN_SOUNDS)


def test_display_name_and_metadata() -> None:
    assert sounds.display_name("chime") == "Bloom"
    assert sounds.display_name("none") == "None"
    assert sounds.display_name("") == "None"
    assert sounds.display_name("/tmp/beep.wav") == "beep.wav"
    assert sounds.is_builtin("siren")
    assert not sounds.is_builtin("nope")
    assert sounds.NONE_ID in sounds.selectable_ids()
