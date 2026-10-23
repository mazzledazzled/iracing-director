"""One-time install of the Chatterbox voice into its own environment (.venv-voice),
so PyTorch never clashes with the main app's packages.

    run from setup_chatterbox.bat
"""
from __future__ import annotations

import subprocess
import sys
import venv
from pathlib import Path

from .config import ROOT

VENV = ROOT / ".venv-voice"


def voice_python() -> Path:
    win = VENV / "Scripts" / "python.exe"
    return win if win.exists() else VENV / "bin" / "python"


def run(cmd: list[str]):
    print("  >", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True)


def main():
    print("== Installing the Chatterbox voice (one time, ~5 GB download) ==")
    if not voice_python().exists():
        print("  creating .venv-voice ...")
        venv.EnvBuilder(with_pip=True).create(VENV)
    py = voice_python()
    ver = subprocess.run([str(py), "-c", "import sys;print(sys.version_info[0]*100+sys.version_info[1])"],
                         capture_output=True, text=True, check=True).stdout.strip()
    ver = int(ver)
    run([py, "-m", "pip", "install", "--upgrade", "pip", "-q"])
    # PyTorch with CUDA (NVIDIA GPU) - pinned to what chatterbox-tts expects
    if ver < 314:
        run([py, "-m", "pip", "install", "torch==2.6.0", "torchaudio==2.6.0",
             "--index-url", "https://download.pytorch.org/whl/cu124"])
    else:
        run([py, "-m", "pip", "install", "torch>=2.9.0", "torchaudio>=2.9.0",
             "--index-url", "https://download.pytorch.org/whl/cu128"])
    run([py, "-m", "pip", "install", "chatterbox-tts"])
    ok = subprocess.run([str(py), "-c", "import torch;print(torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else '')"],
                        capture_output=True, text=True)
    print("  CUDA:", ok.stdout.strip() or ok.stderr.strip()[-300:])
    # warm-up: downloads the model weights and proves it works
    from .config import load_config
    from .tts import Chatterbox

    cfg = load_config()
    out = ROOT / "models" / "chatterbox_test.wav"
    Chatterbox(cfg).say_many([dict(text="And he's done it! What a move, right at the end of the lap!",
                                   path=out, mood="hype")])
    print(f"\nChatterbox is ready. Test clip: {out}")
    print('Make sure config.toml has  engine = "chatterbox"  under [tts].')


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        print(f"\nInstall step failed ({e}). Scroll up for the error.")
        sys.exit(1)
