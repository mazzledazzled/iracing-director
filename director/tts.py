"""Turn the script into speech, one WAV per line."""
from __future__ import annotations

import math
import shutil
import subprocess
import sys
import wave
from pathlib import Path


def wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def _write_wav(path: Path, samples, rate: int):
    import numpy as np

    a = np.clip(np.asarray(samples, dtype=np.float32), -1, 1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((a * 32767).astype("<i2").tobytes())


MODEL_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
MODEL_FILES = ["kokoro-v1.0.onnx", "voices-v1.0.bin"]


def ensure_kokoro_models(progress=print) -> Path:
    from .config import ROOT

    d = ROOT / "models"
    d.mkdir(exist_ok=True)
    for f in MODEL_FILES:
        p = d / f
        if p.exists() and p.stat().st_size > 1_000_000:
            continue
        progress(f"  downloading voice model {f} (one time, ~{'310' if f.endswith('onnx') else '27'} MB)...")
        import urllib.request

        tmp = p.with_suffix(".part")
        urllib.request.urlretrieve(MODEL_URL + f, tmp)
        tmp.replace(p)
    return d


class Kokoro:
    """Kokoro-82M through ONNX runtime: good quality, runs on CPU, no PyTorch needed."""

    LANG = {"a": "en-us", "b": "en-gb", "e": "es", "f": "fr-fr", "i": "it", "p": "pt-br", "j": "ja", "z": "cmn"}

    def __init__(self, cfg, progress=print):
        try:
            from kokoro_onnx import Kokoro as K
        except ImportError as e:
            raise RuntimeError("kokoro-onnx isn't installed (run setup.bat, or set engine = \"sapi\").") from e
        d = ensure_kokoro_models(progress)
        self.k = K(str(d / MODEL_FILES[0]), str(d / MODEL_FILES[1]))
        self.voice = cfg.tts.kokoro_voice
        self.speed = float(cfg.tts.kokoro_speed)
        self.lang = self.LANG.get(self.voice[0], "en-us")

    def say(self, text: str, path: Path):
        samples, rate = self.k.create(text, voice=self.voice, speed=self.speed, lang=self.lang)
        _write_wav(path, samples, rate)


MOODS = {"calm": 0.4, "normal": 0.5, "hype": 0.7}  # relative energy; scaled by chatterbox_exaggeration


class Chatterbox:
    """Resemble AI's Chatterbox: expressive, can copy a voice from a short sample.
    Runs in its own environment (.venv-voice) - install with setup_chatterbox.bat."""

    def __init__(self, cfg, progress=print):
        from .setup_voice import voice_python

        self.py = voice_python()
        if not self.py.exists():
            raise RuntimeError("Chatterbox isn't installed yet - run setup_chatterbox.bat.")
        self.cfg = cfg
        self.progress = progress
        t = cfg.tts
        self.scale = float(t.get("chatterbox_exaggeration", 0.5)) / 0.5
        self.cfg_weight = float(t.get("chatterbox_cfg", 0.5))
        self.reference = self._reference(str(t.get("chatterbox_reference", "") or ""))

    def _reference(self, ref: str) -> str:
        from .config import ROOT

        if ref:
            p = Path(ref)
            if not p.is_absolute():
                p = ROOT / p
            if not p.exists():
                raise RuntimeError(f"chatterbox_reference file not found: {p}")
            # accept any recording format (m4a, mp3, ...): convert to clean mono WAV, trim silence at the ends
            from .render import ffmpeg_exe

            out = ROOT / "models" / f"ref_{p.stem}.wav"
            out.parent.mkdir(exist_ok=True)
            if not out.exists() or out.stat().st_mtime < p.stat().st_mtime:
                trim = "silenceremove=start_periods=1:start_threshold=-45dB,areverse," \
                       "silenceremove=start_periods=1:start_threshold=-45dB,areverse,loudnorm"
                subprocess.run([ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(p),
                                "-ac", "1", "-ar", "24000", "-af", trim, "-t", "25", str(out)], check=True)
                secs = wav_seconds(out)
                if secs < 6:
                    self.progress(f"  WARNING: your voice sample is only {secs:.0f}s - 10-20s copies the voice much better.")
            return str(out)
        # no sample given: make one with the Kokoro voice, Chatterbox copies its accent and adds the energy
        seed = ROOT / "models" / f"seed_{self.cfg.tts.kokoro_voice}.wav"
        if not seed.exists():
            self.progress("  making a reference voice sample with Kokoro (one time)...")
            Kokoro(self.cfg, self.progress).say(
                "Welcome back to the circuit. It's been a thrilling race so far, and the battle for the lead "
                "is really heating up now. He's right on the gearbox, looking for a way through. "
                "Can he make it stick? Yes! He's done it!", seed)
        return str(seed)

    def say(self, text: str, path: Path):
        self.say_many([dict(text=text, path=path, mood="normal")])

    def say_many(self, items: list[dict]):
        import json
        import tempfile

        job = dict(reference=self.reference, cfg_weight=self.cfg_weight, temperature=0.8, items=[
            dict(text=it["text"], out=str(it["path"]), seed=1234 + i,
                 exaggeration=round(min(1.5, MOODS.get(it.get("mood", "normal"), 0.5) * self.scale), 2))
            for i, it in enumerate(items)])
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(job, f)
            job_path = f.name
        worker = Path(__file__).with_name("chatterbox_worker.py")
        proc = subprocess.Popen([str(self.py), str(worker), job_path], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        tail = []
        for line in proc.stdout:
            line = line.rstrip()
            if line.startswith("DONE "):
                i = int(line.split()[1])
                self.progress(f"    voiced {i + 1}/{len(items)}")
            elif line.startswith("INFO "):
                self.progress("  " + line[5:])
            elif line:
                tail = (tail + [line])[-25:]
        if proc.wait() != 0:
            raise RuntimeError("Chatterbox failed:\n" + "\n".join(tail))
        Path(job_path).unlink(missing_ok=True)


class Piper:
    def __init__(self, cfg):
        self.model = cfg.tts.piper_model
        self.exe = shutil.which("piper") or str(Path(sys.executable).parent / "piper")
        if not self.model or not Path(self.model).exists():
            raise RuntimeError("Set tts.piper_model in config.toml to a downloaded piper .onnx voice.")

    def say(self, text: str, path: Path):
        subprocess.run([self.exe, "--model", self.model, "--output_file", str(path)],
                       input=text.encode("utf-8"), check=True, capture_output=True)


class Sapi:
    """Built-in Windows voice through PowerShell - always available on Windows."""

    def __init__(self, cfg):
        if sys.platform != "win32":
            raise RuntimeError("The SAPI voice only exists on Windows.")

    def say(self, text: str, path: Path):
        txt = path.with_suffix(".txt")
        txt.write_text(text, encoding="utf-8")
        q = lambda p: str(p).replace("'", "''")
        ps = (
            "Add-Type -AssemblyName System.Speech;"
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
            "$s.Rate = 2;"
            f"$s.SetOutputToWaveFile('{q(path)}');"
            f"$s.Speak([IO.File]::ReadAllText('{q(txt)}'));"
            "$s.Dispose()"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, capture_output=True)
        txt.unlink(missing_ok=True)


class TestTone:
    """No real voice: a quiet tone as long as the line would take. For testing the pipeline."""

    def __init__(self, cfg):
        self.wps = float(cfg.commentary.words_per_second)

    def say(self, text: str, path: Path):
        secs = max(1.0, len(text.split()) / self.wps)
        rate = 24000
        samples = [0.15 * math.sin(2 * math.pi * 220 * i / rate) * (1 if (i // 2400) % 2 == 0 else 0.4)
                   for i in range(int(secs * rate))]
        _write_wav(path, samples, rate)


ENGINES = dict(chatterbox=Chatterbox, kokoro=Kokoro, piper=Piper, sapi=Sapi, test=TestTone)


def make_engine(cfg, progress=print):
    name = cfg.tts.engine.lower()
    order = [name] + [e for e in ("kokoro", "sapi") if e != name]
    for n in order:
        try:
            eng = ENGINES[n](cfg, progress) if n in ("chatterbox", "kokoro") else ENGINES[n](cfg)
            if n != name:
                progress(f"  WARNING: couldn't start '{name}' voice, using '{n}' instead.")
            return n, eng
        except Exception as e:  # noqa: BLE001
            progress(f"  {n}: {e}")
    raise SystemExit("No text-to-speech engine available. See README > Voice.")


def spoken(text: str, cfg) -> str:
    """Text as the voice should say it: [pronunciations] from config.toml, and
    gamer tags made speakable (Fast_Eddie -> Fast Eddie)."""
    import re

    for name, say in (cfg.get("pronunciations") or {}).items():
        text = re.sub(rf"(?<![\w]){re.escape(name)}(?![\w])", str(say), text)
    text = re.sub(r"(?<=\w)_(?=\w)", " ", text)
    return text


def voice_script(proj, script: dict, cfg, progress=print) -> dict:
    name, eng = make_engine(cfg, progress)
    lines = [dict(l, text=spoken(l["text"], cfg)) for l in script["lines"]]
    paths = [proj.path("audio") / f"line_{i:03d}.wav" for i in range(len(lines))]
    if hasattr(eng, "say_many"):  # load the model once for all lines
        eng.say_many([dict(text=l["text"], path=p, mood=l.get("mood", "normal")) for l, p in zip(lines, paths)])
    out = []
    for i, line in enumerate(lines):
        path = paths[i]
        if not hasattr(eng, "say_many"):
            eng.say(line["text"], path)
        secs = wav_seconds(path)
        out.append(dict(line, text=script["lines"][i]["text"], spoken=line["text"], file=path.name, seconds=round(secs, 2)))
        if line["kind"] == "clip":
            budget = line["words"] / float(cfg.commentary.words_per_second)
            flag = "  (long - may be trimmed)" if secs > budget + 2.5 else ""
        else:
            flag = ""
        progress(f"  {i + 1}/{len(script['lines'])}  {secs:4.1f}s  {line['text'][:70]}{flag}")
    return dict(engine=name, lines=out)
