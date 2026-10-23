"""Runs inside the separate .venv-voice environment (PyTorch + Chatterbox).

    python chatterbox_worker.py job.json

job.json: {"reference": "voice.wav" | "", "cfg_weight": 0.35, "temperature": 0.8,
           "items": [{"text": "...", "out": "line_000.wav", "exaggeration": 0.7}, ...]}

Prints "DONE <index>" after each line so the main app can show progress.
Kept free of imports from the rest of the app on purpose.
"""
import json
import sys
import wave

import numpy as np


def write_wav(path, samples, rate):
    a = np.clip(np.asarray(samples, dtype=np.float32).reshape(-1), -1, 1)
    # trim near-silent edges, keep a short tail so lines don't sound clipped
    loud = np.flatnonzero(np.abs(a) > 0.01)
    if loud.size:
        a = a[max(0, loud[0] - int(0.05 * rate)): min(a.size, loud[-1] + int(0.15 * rate))]
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((a * 32767).astype("<i2").tobytes())


def main():
    job = json.load(open(sys.argv[1], encoding="utf-8"))
    import torch
    from chatterbox.tts import ChatterboxTTS

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"INFO device={device}", flush=True)
    if device == "cpu":
        print("INFO no CUDA GPU found - this will be slow", flush=True)
    model = ChatterboxTTS.from_pretrained(device=device)
    if job.get("reference"):
        model.prepare_conditionals(job["reference"], exaggeration=0.5)
    for i, it in enumerate(job["items"]):
        if it.get("seed") is not None:
            torch.manual_seed(int(it["seed"]))
        wav = model.generate(
            it["text"],
            exaggeration=float(it.get("exaggeration", 0.6)),
            cfg_weight=float(job.get("cfg_weight", 0.35)),
            temperature=float(job.get("temperature", 0.8)),
        )
        write_wav(it["out"], wav.squeeze(0).numpy(), model.sr)
        print(f"DONE {i}", flush=True)


if __name__ == "__main__":
    main()
