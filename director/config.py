"""Settings loading and project-folder handling."""
from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Section(dict):
    """dict with attribute access: cfg.highlights.target_seconds"""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as e:
            raise AttributeError(f"config is missing '{name}'") from e


def load_config(path: str | Path | None = None) -> Section:
    if not path and not (ROOT / "config.toml").exists() and (ROOT / "config.example.toml").exists():
        import shutil  # first run: your own settings file (kept out of git - it holds your OBS password)

        shutil.copy(ROOT / "config.example.toml", ROOT / "config.toml")
    path = Path(path) if path else ROOT / "config.toml"
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    return Section({k: Section(v) if isinstance(v, dict) else v for k, v in raw.items()})


def slugify(text: str) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")
    return text[:60] or "race"


def _jsonable(o):
    if hasattr(o, "item"):  # numpy scalars
        return o.item()
    if hasattr(o, "tolist"):
        return o.tolist()
    if isinstance(o, set):
        return sorted(o)
    raise TypeError(f"can't save {type(o).__name__}")


class Project:
    """One replay's working folder: race.json, events.json, plan.json, clips/, audio/ ..."""

    def __init__(self, folder: Path):
        self.dir = Path(folder)
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "clips").mkdir(exist_ok=True)
        (self.dir / "audio").mkdir(exist_ok=True)
        (self.dir / "render").mkdir(exist_ok=True)

    # file helpers
    def path(self, name: str) -> Path:
        return self.dir / name

    def save(self, name: str, data) -> Path:
        p = self.path(name)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, ensure_ascii=False, default=_jsonable)
        return p

    def load(self, name: str):
        p = self.path(name)
        if not p.exists():
            raise FileNotFoundError(
                f"{p.name} not found in {self.dir}. Run the earlier step first "
                f"(see README: scan -> plan -> capture -> commentary -> voice -> render)."
            )
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    def has(self, name: str) -> bool:
        return self.path(name).exists()


def output_root(cfg) -> Path:
    p = Path(cfg.project.output_dir)
    if not p.is_absolute():
        p = ROOT / p
    p.mkdir(parents=True, exist_ok=True)
    return p


def new_project(cfg, name: str) -> Project:
    root = output_root(cfg)
    proj = Project(root / slugify(name))
    (root / "latest.txt").write_text(proj.dir.name, encoding="utf-8")
    return proj


def open_project(cfg, name: str | None = None) -> Project:
    root = output_root(cfg)
    if not name:
        latest = root / "latest.txt"
        if not latest.exists():
            raise SystemExit("No project yet. Load a replay in iRacing and run: run.bat scan")
        name = latest.read_text(encoding="utf-8").strip()
    folder = root / name
    if not folder.exists():
        raise SystemExit(f"Project folder not found: {folder}")
    (root / "latest.txt").write_text(folder.name, encoding="utf-8")
    return Project(folder)
