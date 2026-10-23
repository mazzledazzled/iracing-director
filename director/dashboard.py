"""Control panel: run every step, watch progress, change settings, edit the script and
watch the videos - all from one page in your browser.

    dashboard.bat        (or: run.bat dashboard)

The only things you do by hand: start iRacing (open the replay) and OBS.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import tomllib
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .config import ROOT, load_config, output_root

CONFIG = ROOT / "config.toml"

# ------------------------------------------------------------------ settings form
# (section, key, type, label, help, options)
FIELDS = [
    ("Highlights video", [
        ("highlights", "target_seconds", "num", "Default length (seconds)", "Suggested on the race setup page", None),
        ("highlights", "spread", "num", "Share screen time", "0 = only the best moments, 0.35 = balanced, 0.5+ = spread wide", None),
        ("highlights", "review_before_capture", "bool", "Review page before recording", "", None),
        ("highlights", "battle_gap", "num", "Battle gap (s)", "How close two cars must be to count as a battle", None),
        ("highlights", "min_battle_seconds", "num", "Battle minimum (s)", "How long they must stay close", None),
        ("highlights", "pre_roll", "num", "Seconds before a moment", "", None),
        ("highlights", "post_roll", "num", "Seconds after a moment", "", None),
    ]),
    ("Commentary", [
        ("commentary", "model", "model", "AI model (Ollama)", "16 GB GPU: mistral-small3.2. 8-12 GB: gemma3:12b", None),
        ("commentary", "style", "text", "Commentator style", "Describe the announcer in plain words", None),
        ("commentary", "persona", "persona", "Rewrite voice", "Second pass in a character's voice", ["", "gilmore"]),
        ("commentary", "temperature", "num", "Creativity", "0.5 = steady, 0.9 = wilder", None),
    ]),
    ("Voice", [
        ("tts", "engine", "select", "Voice engine", "", ["chatterbox", "kokoro", "sapi", "piper"]),
        ("tts", "chatterbox_reference", "str", "Voice sample file", "e.g. voice.wav in the app folder (empty = British default)", None),
        ("tts", "chatterbox_exaggeration", "num", "Energy", "0.5 natural, 0.7+ excited", None),
        ("tts", "chatterbox_cfg", "num", "Pace", "0.5 natural, lower = punchier", None),
        ("tts", "kokoro_voice", "select", "Kokoro voice", "", ["bm_george", "bm_lewis", "bm_daniel", "am_michael", "am_adam", "bf_emma", "af_heart"]),
    ]),
    ("Video look and sound", [
        ("render", "show_lower_thirds", "bool", "Captions", "", None),
        ("render", "game_volume", "num", "Game volume", "", None),
        ("render", "commentary_volume", "num", "Commentary volume", "", None),
        ("render", "whoosh_volume", "num", "Transition swoosh", "0 = off", None),
        ("render", "voice_polish", "bool", "Radio polish on the voice", "", None),
    ]),
    ("Recording", [
        ("capture", "auto_close_apps", "bool", "Close iRacing + OBS after recording", "Frees the GPU for commentary and voice", None),
        ("capture", "settle_seconds", "num", "Camera settle time (s)", "Raise if clips start on the wrong car", None),
        ("capture", "obs_password", "secret", "OBS WebSocket password", "OBS > Tools > WebSocket Server Settings. Never shown here once saved.", None),
        ("capture", "custom_cameras", "cams", "Your custom cameras", "Camera groups you made/saved in iRacing's camera tool (exact names). Load that camera set in iRacing before recording.", None),
        ("capture", "start_cameras", "cams", "Start cameras", "", None),
        ("capture", "chase_cameras", "cams", "Battle / build-up cameras", "", None),
        ("capture", "trackside_cameras", "cams", "Pass cameras", "", None),
        ("capture", "incident_cameras", "cams", "Crash / spin cameras", "", None),
        ("capture", "finish_cameras", "cams", "Finish cameras", "", None),
        ("capture", "onboard_cameras", "cams", "Onboard cameras", "", None),
    ]),
    ("Race in 30 (TikTok)", [
        ("short", "seconds", "num", "Default length (seconds)", "Suggested on the race setup page", None),
        ("short", "beat_seconds", "num", "Seconds per moment", "", None),
        ("short", "layout", "select", "Layout", "fit = zoomed middle + blurred fill, crop = full screen", ["fit", "crop"]),
        ("short", "zoom", "num", "Zoom (fit layout)", "", None),
        ("short", "review", "bool", "Review page before recording", "", None),
        ("short", "music", "str", "Music file", "Full path to an mp3/wav, or empty", None),
        ("short", "music_volume", "num", "Music volume", "", None),
        ("short", "start_cameras", "cams", "Start cameras", "", None),
        ("short", "overtake_cameras", "cams", "You pass someone", "", None),
        ("short", "lost_cameras", "cams", "Someone passes you", "", None),
        ("short", "battle_cameras", "cams", "Battle cameras", "", None),
        ("short", "incident_cameras", "cams", "Crash / spin cameras", "", None),
        ("short", "restart_cameras", "cams", "Restart cameras", "", None),
        ("short", "finish_cameras", "cams", "Finish cameras", "", None),
    ]),
]


def toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=False)


def set_config_values(changes: list[tuple[str, str, object]]):
    """Edit config.toml in place, keeping comments and layout. Validates before writing."""
    text = CONFIG.read_text(encoding="utf-8").replace("\r\n", "\n")
    for section, key, value in changes:
        lines = text.split("\n")
        start = next((i for i, l in enumerate(lines) if l.strip() == f"[{section}]"), None)
        if start is None:
            lines += ["", f"[{section}]", f"{key} = {toml_value(value)}"]
            text = "\n".join(lines)
            continue
        end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("[")), len(lines))
        pat = re.compile(rf"^{re.escape(key)}\s*=\s*(.*?)(\s+#[^\"'\]]*)?$")
        for i in range(start + 1, end):
            m = pat.match(lines[i])
            if m:
                lines[i] = f"{key} = {toml_value(value)}" + (m.group(2) or "")
                break
        else:
            ins = end
            while ins > start + 1 and not lines[ins - 1].strip():
                ins -= 1
            lines.insert(ins, f"{key} = {toml_value(value)}")
        text = "\n".join(lines)
    tomllib.loads(text)  # raises if something went wrong - nothing is written
    CONFIG.write_text(text, encoding="utf-8")


# ------------------------------------------------------------------ status checks
def _running(exe: str) -> bool:
    if sys.platform != "win32":
        return False
    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {exe}", "/NH"], capture_output=True, text=True).stdout
    return exe.lower() in out.lower()


def status(cfg) -> dict:
    st = {}
    st["iracing"] = _running("iRacingSim64DX11.exe")
    try:  # iRacing's own status page says whether a session/replay is loaded
        with urllib.request.urlopen("http://127.0.0.1:32034/get_sim_status?object=simStatus", timeout=1) as r:
            st["replay"] = "running:1" in r.read().decode("utf-8", "replace")
    except Exception:
        st["replay"] = False
    st["obs"] = _running("obs64.exe")
    st["obs_ws"] = False
    if st["obs"]:
        try:
            import obsws_python as obs

            c = cfg.capture
            cl = obs.ReqClient(host=c.obs_host, port=int(c.obs_port), password=c.obs_password or "", timeout=2)
            st["obs_ws"] = True
            try:
                cl.base_client.ws.close()
            except Exception:
                pass
        except Exception:
            pass
    st["ollama"], st["models"] = False, []
    try:
        with urllib.request.urlopen(cfg.commentary.ollama_url.rstrip("/") + "/api/tags", timeout=2) as r:
            st["models"] = [m["name"] for m in json.loads(r.read()).get("models", [])]
            st["ollama"] = True
    except Exception:
        pass
    want = cfg.commentary.model if ":" in cfg.commentary.model else cfg.commentary.model + ":latest"
    st["model_ok"] = want in st["models"]
    st["chatterbox"] = (ROOT / ".venv-voice").exists()
    return st


# ------------------------------------------------------------------ jobs
class Job:
    def __init__(self):
        self.proc = None
        self.lines: list[str] = []
        self.cmd = ""
        self.started = 0.0
        self.code = None
        self.lock = threading.Lock()

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, args: list[str]):
        if self.running:
            raise RuntimeError("Something is already running - wait for it or press Stop.")
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        self.lines, self.code, self.cmd, self.started = [], None, " ".join(args), time.time()
        self.proc = subprocess.Popen([sys.executable, "-m", "director"] + args, cwd=str(ROOT), env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8", errors="replace", bufsize=1)
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.proc.stdout:
            with self.lock:
                self.lines.append(line.rstrip("\n"))
        self.code = self.proc.wait()

    def enter(self):
        if self.running:
            try:
                self.proc.stdin.write("\n")
                self.proc.stdin.flush()
            except Exception:
                pass

    def stop(self):
        if self.running:
            self.proc.terminate()
            with self.lock:
                self.lines.append("Stopped.")

    def snapshot(self, since: int) -> dict:
        with self.lock:
            lines = self.lines[since:]
            total = len(self.lines)
        step = next((l.strip("= ").strip() for l in reversed(self.lines) if l.startswith("== ")), "")
        done = next((l.split("DONE:", 1)[1].strip() for l in reversed(self.lines) if "DONE:" in l), None)
        err = next((l.split("ERROR:", 1)[1].strip() for l in reversed(self.lines) if l.startswith("ERROR:")), None)
        waiting = None  # a setup or review page the user still has to fill in
        for i in range(len(self.lines) - 1, -1, -1):
            l = self.lines[i]
            m = re.search(r"(setup|review) page: (http://127\.0\.0\.1:\d+/)", l)
            if m:
                later = self.lines[i + 1:]
                finished = any(x.strip().startswith("featured:" if m.group(1) == "setup" else "saved:") for x in later)
                if not finished:
                    waiting = dict(kind=m.group(1), url=m.group(2))
                break
        return dict(running=self.running, cmd=self.cmd, lines=lines, total=total, step=step, done=done,
                    error=err, code=self.code, waiting=waiting if self.running else None,
                    elapsed=int(time.time() - self.started) if self.started else 0)


JOB = Job()


def live_cameras() -> dict:
    """Camera groups loaded in iRacing right now, including custom ones from the camera tool."""
    try:
        import irsdk

        ir = irsdk.IRSDK()
        if not ir.startup():
            return dict(ok=False, error="iRacing isn't running with a session or replay open.")
        try:
            groups = [str(g["GroupName"]) for g in (ir["CameraInfo"] or {}).get("Groups", [])]
            track = (ir["WeekendInfo"] or {}).get("TrackDisplayName", "")
        finally:
            ir.shutdown()
        return dict(ok=True, cameras=groups, track=track)
    except Exception as e:  # noqa: BLE001
        return dict(ok=False, error=str(e))


# ------------------------------------------------------------------ data
def projects(cfg) -> list[dict]:
    root = output_root(cfg)
    latest = (root / "latest.txt").read_text(encoding="utf-8").strip() if (root / "latest.txt").exists() else ""
    out = []
    for d in sorted((p for p in root.iterdir() if p.is_dir() and (p / "race.json").exists()),
                    key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            race = json.loads((d / "race.json").read_text(encoding="utf-8"))
        except Exception:
            continue
        vids = [dict(name=v.name, rel=str(v.relative_to(root)).replace("\\", "/"), mb=round(v.stat().st_size / 1e6))
                for v in sorted(d.glob("*.mp4"), key=lambda v: v.stat().st_mtime, reverse=True)]
        clean = sorted((d / "short" / "clean").glob("*.mp4")) if (d / "short" / "clean").exists() else []
        out.append(dict(name=d.name, track=race.get("track", ""), laps=race.get("session_laps"),
                        when=time.strftime("%d %b %H:%M", time.localtime(d.stat().st_mtime)), latest=d.name == latest,
                        videos=vids, clean=[str(c.relative_to(root)).replace("\\", "/") for c in clean],
                        drivers=[dict(number=x["number"], name=x["name"]) for x in race.get("drivers", [])],
                        cameras=[c["name"] for c in race.get("cameras", [])],
                        has_script=(d / "script.json").exists()))
    return out


def settings_payload(cfg) -> dict:
    raw = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    groups = []
    for title, fields in FIELDS:
        items = []
        for sec, key, typ, label, hint, opts in fields:
            val = (raw.get(sec) or {}).get(key)
            item = dict(section=sec, key=key, type=typ, label=label, hint=hint, options=opts, value=val)
            if typ == "secret":  # the browser only learns whether one is saved
                item["value"], item["is_set"] = "", bool(val)
            items.append(item)
        groups.append(dict(title=title, fields=items))
    return dict(groups=groups, pronunciations=raw.get("pronunciations", {}), pronouns=raw.get("pronouns", {}),
                raw=mask_secrets(CONFIG.read_text(encoding="utf-8")))


SECRET_KEYS = ("obs_password",)
MASK = "********"


def mask_secrets(text: str) -> str:
    """Hide secret values in config text sent to the browser."""
    for k in SECRET_KEYS:
        text = re.sub(rf'(?m)^({k}\s*=\s*)"(.+)"', lambda m: f'{m.group(1)}"{MASK}"', text)
    return text


def unmask_secrets(new_text: str) -> str:
    """Put the real secret back where the browser sent the mask unchanged."""
    current = CONFIG.read_text(encoding="utf-8")
    for k in SECRET_KEYS:
        m = re.search(rf'(?m)^{k}\s*=\s*"(.*)"', current)
        if m:
            new_text = re.sub(rf'(?m)^({k}\s*=\s*)"{re.escape(MASK)}"', lambda x: f'{x.group(1)}"{m.group(1)}"', new_text)
    return new_text


def coerce(typ: str, v):
    if typ == "bool":
        return bool(v)
    if typ == "num":
        f = float(v)
        return int(f) if f.is_integer() and "." not in str(v) else f
    if typ == "cams":
        return [x.strip() for x in (v if isinstance(v, list) else str(v).split(",")) if x.strip()]
    return str(v)


# ------------------------------------------------------------------ server
def serve(port: int = 8760, open_browser: bool = True):
    load_config()  # makes config.toml from config.example.toml on first run
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)

        def _body(self):
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n) or b"{}")

        def _local(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            origin = self.headers.get("Origin")
            ok_host = host in ("127.0.0.1", "localhost")
            ok_origin = origin is None or re.match(r"^http://(127\.0\.0\.1|localhost)(:\d+)?$", origin)
            if not (ok_host and ok_origin):
                self._json({"error": "forbidden"}, 403)
                return False
            return True

        def do_GET(self):
            if not self._local():
                return
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            cfg = load_config()
            if u.path == "/":
                b = PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                return self.wfile.write(b)
            if u.path == "/api/status":
                return self._json(status(cfg))
            if u.path == "/api/projects":
                return self._json(projects(cfg))
            if u.path == "/api/job":
                return self._json(JOB.snapshot(int(q.get("since", 0))))
            if u.path == "/api/settings":
                return self._json(settings_payload(cfg))
            if u.path == "/api/livecams":
                return self._json(live_cameras())
            if u.path == "/api/script":
                p = output_root(cfg) / q.get("project", "") / "script.json"
                return self._json(json.loads(p.read_text(encoding="utf-8")) if p.exists() else {})
            if u.path == "/file":
                return self._file(output_root(cfg), unquote(q.get("p", "")))
            self._json({"error": "not found"}, 404)

        def _file(self, root: Path, rel: str):
            path = (root / rel).resolve()
            if root.resolve() not in path.parents or not path.exists():
                return self._json({"error": "not found"}, 404)
            size = path.stat().st_size
            rng = self.headers.get("Range")
            start, end = 0, size - 1
            if rng and rng.startswith("bytes="):
                a, _, b = rng[6:].partition("-")
                start = int(a or 0)
                end = int(b) if b else min(size - 1, start + 8 * 1024 * 1024)
            self.send_response(206 if rng else 200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if rng:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (ConnectionError, OSError):
                        return
                    left -= len(chunk)

        def do_POST(self):
            if not self._local():
                return
            u = urlparse(self.path)
            body = self._body()
            cfg = load_config()
            try:
                if u.path == "/api/run":
                    args = [str(a) for a in body.get("args", [])]
                    allowed = {"scan", "plan", "review", "capture", "commentary", "voice", "render", "all",
                               "remake", "short", "fake"}
                    if not args or args[0] not in allowed:
                        raise ValueError("unknown command")
                    JOB.start(args)
                    return self._json({"ok": True})
                if u.path == "/api/enter":
                    JOB.enter()
                    return self._json({"ok": True})
                if u.path == "/api/stop":
                    JOB.stop()
                    return self._json({"ok": True})
                if u.path == "/api/settings":
                    types = {(s, k): t for _, fs in FIELDS for s, k, t, *_ in fs}
                    raw = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
                    changes = []
                    for c in body.get("changes", []):
                        typ = types.get((c["section"], c["key"]), "str")
                        if typ == "secret":
                            if c.get("clear"):
                                changes.append((c["section"], c["key"], ""))
                            elif str(c.get("value", "")).strip():
                                changes.append((c["section"], c["key"], str(c["value"]).strip()))
                            continue  # empty box = keep the saved one
                        v = coerce(typ, c["value"])
                        old = (raw.get(c["section"]) or {}).get(c["key"])
                        if old is None or old != v or type(old) is bool != (type(v) is bool):
                            changes.append((c["section"], c["key"], v))  # only touch what changed
                    for tbl in ("pronunciations", "pronouns"):
                        if tbl in body:
                            for k, v in body[tbl].items():
                                if re.fullmatch(r"[A-Za-z0-9_\-]+", k) and (raw.get(tbl) or {}).get(k) != str(v):
                                    changes.append((tbl, k, str(v)))
                    set_config_values(changes)
                    return self._json({"ok": True, "saved": len(changes)})
                if u.path == "/api/rawconfig":
                    text = unmask_secrets(body.get("text", ""))
                    tomllib.loads(text)
                    CONFIG.write_text(text.replace("\r\n", "\n"), encoding="utf-8")
                    return self._json({"ok": True})
                if u.path == "/api/script":
                    p = output_root(cfg) / body["project"] / "script.json"
                    data = json.loads(p.read_text(encoding="utf-8"))
                    for i, t in body.get("texts", {}).items():
                        data["lines"][int(i)]["text"] = str(t).strip()
                    p.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
                    return self._json({"ok": True})
                if u.path == "/api/latest":
                    (output_root(cfg) / "latest.txt").write_text(body["project"], encoding="utf-8")
                    return self._json({"ok": True})
                if u.path == "/api/openfolder":
                    target = (output_root(cfg) / body.get("project", "")).resolve()
                    if sys.platform == "win32" and target.exists():
                        os.startfile(str(target))  # noqa: S606
                    return self._json({"ok": True})
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": False, "error": str(e)}, 400)
            self._json({"error": "not found"}, 404)

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    except OSError:
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"\n  iRacing Director dashboard: {url}\n  Keep this window open while you use it. Close it to stop the dashboard.\n")
    if open_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>iRacing Director</title>
<style>
:root{--bg:#0b0e13;--panel:#141922;--panel2:#1b212c;--line:#283041;--text:#e9eef4;--dim:#93a0b2;--accent:#e10600;--ok:#2bb673;--warn:#f0b400;--bad:#e5484d}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 "Segoe UI",system-ui,sans-serif}
header{position:sticky;top:0;z-index:10;background:#0b0e13f0;border-bottom:1px solid var(--line);padding:12px 20px;display:flex;gap:18px;align-items:center;flex-wrap:wrap}
.brand{font-weight:800;font-size:19px;letter-spacing:.4px}.brand b{color:var(--accent)}
.lights{display:flex;gap:14px;flex-wrap:wrap;font-size:13px;color:var(--dim)}
.light{display:flex;gap:6px;align-items:center}.dot{width:10px;height:10px;border-radius:50%;background:#444}
.dot.ok{background:var(--ok)}.dot.warn{background:var(--warn)}.dot.bad{background:var(--bad)}
nav{display:flex;gap:4px;margin-left:auto}nav button{background:none;border:none;color:var(--dim);padding:8px 14px;border-radius:6px;font:inherit;cursor:pointer}
nav button.on{background:var(--panel2);color:var(--text)}
main{max-width:1200px;margin:0 auto;padding:20px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px}
.card h3{margin:0 0 4px;font-size:17px}.card p{margin:0 0 12px;color:var(--dim);font-size:14px}
.card.hero{border-left:4px solid var(--accent)}
button.go{background:var(--accent);color:#fff;border:none;border-radius:7px;padding:10px 16px;font:inherit;font-weight:700;cursor:pointer}
button.go:disabled,button.sec:disabled{opacity:.4;cursor:not-allowed}
button.sec{background:var(--panel2);color:var(--text);border:1px solid var(--line);border-radius:7px;padding:8px 13px;font:inherit;cursor:pointer}
.steps{display:flex;flex-wrap:wrap;gap:8px}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
label{color:var(--dim);font-size:13px}
input,select,textarea{background:#0e1218;color:var(--text);border:1px solid var(--line);border-radius:6px;padding:7px 9px;font:inherit}
input[type=number]{width:96px}textarea{width:100%;min-height:60px}
.log{background:#07090c;border:1px solid var(--line);border-radius:8px;padding:12px;height:360px;overflow:auto;font:13px/1.45 Consolas,monospace;white-space:pre-wrap;color:#c9d3df}
.jobbar{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:18px 0 10px}
.pill{background:var(--panel2);border:1px solid var(--line);border-radius:999px;padding:4px 12px;font-size:13px}
.pill.run{border-color:var(--accent);color:#fff}.pill.ok{border-color:var(--ok)}.pill.err{border-color:var(--bad)}
.banner{background:#1f2a1c;border:1px solid var(--ok);border-radius:8px;padding:10px 14px;margin:10px 0}
.banner.err{background:#2a1a1a;border-color:var(--bad)}.banner.rev{background:#2a2410;border-color:var(--warn)}
.banner a{color:#fff;font-weight:700}
.fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px 18px}
.field label{display:block;margin-bottom:4px;color:var(--text);font-size:14px}.field small{display:block;color:var(--dim);font-size:12px;margin-top:3px}
.field input[type=text],.field select,.field textarea{width:100%}
.chips{display:flex;flex-wrap:wrap;gap:5px;margin-top:6px}.chip{font-size:12px;background:var(--panel2);border:1px solid var(--line);border-radius:999px;padding:2px 9px;cursor:pointer}
.proj{display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding:12px 0;border-bottom:1px solid var(--line)}
.proj:last-child{border-bottom:none}.proj .t{flex:1;min-width:220px}.proj .t b{display:block}
video{width:100%;max-height:70vh;background:#000;border-radius:8px}
.line{display:grid;grid-template-columns:60px 1fr;gap:10px;margin-bottom:8px;align-items:start}.line .k{color:var(--dim);font-size:12px;padding-top:8px}
.hide{display:none}.dim{color:var(--dim)}
.toast{position:fixed;bottom:18px;right:18px;background:var(--ok);color:#06210f;padding:10px 16px;border-radius:8px;font-weight:700;display:none;z-index:20}
</style></head><body>
<header><div class="brand"><b>iRACING</b> DIRECTOR</div>
<div class="lights" id="lights"></div>
<nav><button data-tab="run" class="on">Run</button><button data-tab="videos">Videos</button><button data-tab="script">Script</button><button data-tab="settings">Settings</button></nav></header>
<main>
<section id="tab-run">
  <div class="row"><label>Race</label><select id="proj" style="min-width:320px"></select><span class="dim" id="projinfo"></span></div>
  <p class="dim" style="margin:0 0 14px">After each scan, a <b>race setup</b> page opens with that race's drivers: pick the featured driver (or type a name or car number) and the video length there.</p>
  <div class="grid">
    <div class="card hero"><h3>Full highlights video</h3><p>Scans the replay open in iRacing, picks the moments, records them, writes and voices the commentary, builds the video. Closes iRacing + OBS when recording is done.</p>
      <div class="row"><label>Voice</label><select id="persona_all"><option value="">Normal announcer</option><option value="gilmore">Happy Gilmore</option></select></div>
      <button class="go" onclick="runAll()">Start</button></div>
    <div class="card hero"><h3>A race in 30 (TikTok)</h3><p>A short vertical video of one driver's race (or the race's best moments), plus clean clips for TikTok's editor. No commentary.</p>
      <div class="row"><label><input type="checkbox" id="short_reuse"> Use the selected race (don't scan again)</label></div>
      <button class="go" onclick="runShort()">Start</button></div>
    <div class="card"><h3>Rebuild from the last scan</h3><p>Changed settings? Re-picks highlights for the selected race and rebuilds everything. Needs the same replay open.</p>
      <div class="row"><label>Voice</label><select id="persona_remake"><option value="">Normal announcer</option><option value="gilmore">Happy Gilmore</option></select></div>
      <button class="go" onclick="run(['remake'].concat(pa('persona_remake')),true)">Start</button></div>
  </div>
  <div class="card" style="margin-top:14px"><h3>Single steps</h3><p>For fixing one part. iRacing + OBS are needed for <i>Scan</i> and <i>Record</i>; close them before <i>Commentary</i> and <i>Voice</i>.</p>
    <div class="steps">
      <button class="sec" onclick="run(['scan'])">1 · Scan</button>
      <button class="sec" onclick="run(['plan','--setup'],true)">2 · Pick highlights</button>
      <button class="sec" onclick="run(['review'],true)">3 · Review clips</button>
      <button class="sec" onclick="run(['capture'],true)">4 · Record changed clips</button>
      <button class="sec" onclick="run(['commentary'].concat(pa('persona_all')),true)">5 · Commentary</button>
      <button class="sec" onclick="run(['voice'],true)">6 · Voice</button>
      <button class="sec" onclick="run(['render'],true)">7 · Build video</button>
      <button class="sec" onclick="run(['fake'])">Test without iRacing</button>
    </div></div>
  <div class="jobbar"><span class="pill" id="jobpill">Idle</span><span id="jobstep" class="dim"></span><span class="dim" id="jobtime"></span>
    <span style="flex:1"></span><button class="sec" id="btn_enter" onclick="post('/api/enter',{})">Press Enter</button><button class="sec" id="btn_stop" onclick="if(confirm('Stop the running job?'))post('/api/stop',{})">Stop</button></div>
  <div id="jobbanner"></div>
  <div class="log" id="log"></div>
</section>

<section id="tab-videos" class="hide"><div class="card"><h3>Your videos</h3><p>Every race you've scanned. Click a video to watch it here.</p><div id="vlist"></div></div>
  <div class="card hide" id="player" style="margin-top:14px"><h3 id="ptitle"></h3><video id="pvideo" controls></video></div></section>

<section id="tab-script" class="hide"><div class="card"><h3>Commentary script</h3>
  <p>Edit any line, save, then re-voice and rebuild. <span class="dim">The selected race on the Run tab is used.</span></p>
  <div id="slist" class="dim">No script yet - run Commentary first.</div>
  <div class="row" style="margin-top:12px"><button class="go" onclick="saveScript(false)">Save</button><button class="sec" onclick="saveScript(true)">Save, re-voice and rebuild video</button></div></div></section>

<section id="tab-settings" class="hide"><div id="sgroups"></div>
  <div class="card" style="margin-top:14px"><h3>Name pronunciations and pronouns</h3><p>How the voice says a name (write it the way it sounds), and which pronouns to use.</p>
    <div id="prons"></div><button class="sec" onclick="addPron()">+ add a name</button></div>
  <div class="card" style="margin-top:14px"><h3>Cameras from iRacing</h3><p>Reads the camera groups loaded in iRacing right now, including custom groups you made or loaded in iRacing's camera tool. Click one to add it to <i>Your custom cameras</i>; after that it appears in every camera list and on the review page.</p>
    <button class="sec" onclick="readLive()">Read cameras from iRacing</button><div class="chips" id="livecams" style="margin-top:10px"></div></div>
  <div class="row" style="margin-top:14px"><button class="go" onclick="saveSettings()">Save settings</button><span class="dim">Saved straight into config.toml.</span></div>
  <details class="card" style="margin-top:14px"><summary style="cursor:pointer">Advanced: edit config.toml directly</summary>
    <textarea id="raw" style="min-height:420px;font:13px Consolas,monospace;margin-top:10px"></textarea>
    <div class="row" style="margin-top:8px"><button class="sec" onclick="saveRaw()">Save config.toml</button></div></details>
</section>
</main><div class="toast" id="toast"></div>
<script>
const $=id=>document.getElementById(id);const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
let P=[],S=null,ST={},since=0,jobRunning=false;
function toast(t,bad){const e=$("toast");e.textContent=t;e.style.background=bad?"#e5484d":"";e.style.color=bad?"#fff":"";e.style.display="block";setTimeout(()=>e.style.display="none",3500)}
async function post(u,b){const r=await fetch(u,{method:"POST",body:JSON.stringify(b)});const j=await r.json();if(!j.ok&&j.error)toast(j.error,true);return j}
document.querySelectorAll("nav button").forEach(b=>b.onclick=()=>{document.querySelectorAll("nav button").forEach(x=>x.classList.toggle("on",x===b));
  ["run","videos","script","settings"].forEach(t=>$("tab-"+t).classList.toggle("hide",t!==b.dataset.tab));
  if(b.dataset.tab==="script")loadScript();if(b.dataset.tab==="settings")loadSettings();if(b.dataset.tab==="videos")renderVideos()});
const cur=()=>$("proj").value;
const pa=id=>$(id).value?["--persona",$(id).value]:["--persona","none"];
async function run(args,needsProj){if(jobRunning){toast("Something is already running",true);return}
  if(needsProj&&cur())args=args.concat(["--name",cur()]);const j=await post("/api/run",{args});if(j.ok){since=0;$("log").textContent="";toast("Started: "+args[0])}}
function runAll(){const need=[];if(!ST.replay)need.push("the replay in iRacing");if(!ST.obs_ws)need.push("OBS (with WebSocket on)");
  if(need.length&&!confirm("Not ready: "+need.join(" and ")+". Start anyway?"))return;run(["all"].concat(pa("persona_all")))}
function runShort(){const a=["short"];
  if($("short_reuse").checked){a.push("--reuse","--name",cur())}else{if(!ST.replay&&!confirm("The replay doesn't look open in iRacing. Start anyway?"))return}
  if(!ST.obs_ws&&!confirm("OBS isn't reachable (open it, WebSocket on). Start anyway?"))return;run(a)}
function light(name,state,text){return `<span class="light"><span class="dot ${state}"></span>${esc(name)}${text?`: ${esc(text)}`:""}</span>`}
async function pollStatus(){try{ST=await (await fetch("/api/status")).json();
  $("lights").innerHTML=light("iRacing",ST.replay?"ok":ST.iracing?"warn":"bad",ST.replay?"replay open":ST.iracing?"no replay":"closed")+
   light("OBS",ST.obs_ws?"ok":ST.obs?"warn":"bad",ST.obs_ws?"ready":ST.obs?"WebSocket off?":"closed")+
   light("Ollama",ST.ollama&&ST.model_ok?"ok":ST.ollama?"warn":"bad",ST.ollama?(ST.model_ok?"ready":"model missing"):"not running")+
   light("Chatterbox",ST.chatterbox?"ok":"warn",ST.chatterbox?"installed":"not installed")}catch(e){}}
async function loadProjects(){P=await (await fetch("/api/projects")).json();const sel=$("proj"),keep=sel.value;
  sel.innerHTML=P.map(p=>`<option value="${esc(p.name)}" ${(keep?p.name===keep:p.latest)?"selected":""}>${esc(p.track)} - ${esc(p.name)} (${esc(p.when)})</option>`).join("")||"<option value=''>No races yet - run a scan</option>";
  projChanged()}
function projChanged(){const p=P.find(x=>x.name===cur());$("projinfo").textContent=p?`${p.drivers.length} drivers · ${p.videos.length} video(s)`:"";
  if(cur())post("/api/latest",{project:cur()})}
$("proj").onchange=projChanged;
function renderVideos(){$("vlist").innerHTML=P.map(p=>`<div class="proj"><div class="t"><b>${esc(p.track)}</b><span class="dim">${esc(p.name)} · ${esc(p.when)}</span></div>
  ${p.videos.map(v=>`<button class="sec" onclick="play('${esc(v.rel)}','${esc(v.name)}')">▶ ${esc(v.name.includes("race_in")?"Race in 30":"Highlights")} (${v.mb} MB)</button>`).join("")||'<span class="dim">no video yet</span>'}
  ${p.clean.length?`<span class="dim">${p.clean.length} TikTok clips</span>`:""}
  <button class="sec" onclick="post('/api/openfolder',{project:'${esc(p.name)}'})">Open folder</button></div>`).join("")||'<span class="dim">Nothing yet.</span>'}
function play(rel,name){$("player").classList.remove("hide");$("ptitle").textContent=name;$("pvideo").src="/file?p="+encodeURIComponent(rel);$("pvideo").play();$("player").scrollIntoView({behavior:"smooth"})}
async function pollJob(){try{const j=await (await fetch("/api/job?since="+since)).json();jobRunning=j.running;
  if(j.lines.length){const l=$("log");const atBottom=l.scrollTop+l.clientHeight>=l.scrollHeight-30;l.textContent+=j.lines.join("\n")+"\n";if(atBottom)l.scrollTop=l.scrollHeight}since=j.total;
  const pill=$("jobpill");pill.className="pill "+(j.running?"run":j.error?"err":j.done?"ok":"");
  const fin=!j.running&&j.code===0;pill.className="pill "+(j.running?"run":j.error?"err":fin?"ok":"");
  pill.textContent=j.running?"Running: "+j.cmd.split(" ")[0]:j.error?"Failed: "+j.cmd.split(" ")[0]:fin?"Finished: "+j.cmd.split(" ")[0]:j.cmd?"Stopped":"Idle";
  $("jobstep").textContent=j.step?("· "+j.step):"";$("jobtime").textContent=j.running?`· ${Math.floor(j.elapsed/60)}:${String(j.elapsed%60).padStart(2,"0")}`:"";
  $("btn_stop").disabled=$("btn_enter").disabled=!j.running;document.querySelectorAll(".go,.steps .sec").forEach(b=>b.disabled=j.running);
  let b="";if(j.waiting)b=j.waiting.kind==="setup"?`<div class="banner rev"><b>Race setup is waiting for you:</b> <a href="${j.waiting.url}" target="_blank">open it</a>, pick the featured driver and length, then click <b>Continue</b>.</div>`:
    `<div class="banner rev"><b>The review page is waiting for you:</b> <a href="${j.waiting.url}" target="_blank">open it</a>, check the clips, then click <b>Save and continue</b>.</div>`;
  else if(j.error&&!j.running)b=`<div class="banner err"><b>Problem:</b> ${esc(j.error)}</div>`;
  else if(j.done&&!j.running)b=`<div class="banner"><b>Done.</b> ${esc(j.done)} - see the Videos tab.</div>`;
  $("jobbanner").innerHTML=b;if(!j.running&&(j.done||j.error)&&!window._refreshed){window._refreshed=true;loadProjects()}if(j.running)window._refreshed=false}catch(e){}}
async function loadScript(){const p=cur();if(!p){return}const s=await (await fetch("/api/script?project="+encodeURIComponent(p))).json();
  if(!s.lines){$("slist").innerHTML='<span class="dim">No script yet for this race - run Commentary first.</span>';return}
  $("slist").innerHTML=s.lines.map((l,i)=>`<div class="line"><span class="k">${l.kind==="clip"?"clip "+(l.clip+1):esc(l.kind)}</span><textarea data-i="${i}">${esc(l.text)}</textarea></div>`).join("")}
async function saveScript(rebuild){const texts={};document.querySelectorAll("#slist textarea").forEach(t=>texts[t.dataset.i]=t.value);
  const j=await post("/api/script",{project:cur(),texts});if(!j.ok)return;toast("Script saved");
  if(rebuild){await run(["voice"],true);const w=setInterval(async()=>{if(!jobRunning){clearInterval(w);const j2=await (await fetch("/api/job")).json();if(!j2.error)run(["render"],true)}},2500);
    document.querySelector('nav button[data-tab="run"]').click()}}
let LIVE=[];
async function loadSettings(){S=await (await fetch("/api/settings")).json();const p=P.find(x=>x.name===cur());const drivers=p?p.drivers:[];
  const custom=((S.groups.flatMap(g=>g.fields).find(f=>f.key==="custom_cameras")||{}).value)||[];
  const cams=[...new Set([...(p?p.cameras:[]),...LIVE,...custom])];
  const models=ST.models||[];
  $("sgroups").innerHTML=S.groups.map((g,gi)=>`<div class="card" style="margin-bottom:14px"><h3>${esc(g.title)}</h3><div class="fields">${g.fields.map((f,fi)=>{
    const id=`f_${gi}_${fi}`,v=f.value;let inp="";
    if(f.type==="bool")inp=`<label><input type="checkbox" id="${id}" ${v?"checked":""}> on</label>`;
    else if(f.type==="num")inp=`<input type="number" step="any" id="${id}" value="${esc(v)}">`;
    else if(f.type==="select")inp=`<select id="${id}">${f.options.map(o=>`<option ${o===v?"selected":""}>${esc(o)}</option>`).join("")}</select>`;
    else if(f.type==="persona")inp=`<select id="${id}"><option value="" ${!v?"selected":""}>None (normal announcer)</option><option value="gilmore" ${v==="gilmore"?"selected":""}>Happy Gilmore</option>${v&&v!=="gilmore"?`<option selected>${esc(v)}</option>`:""}</select>`;
    else if(f.type==="model")inp=`<input type="text" id="${id}" list="models" value="${esc(v)}"><datalist id="models">${models.map(m=>`<option value="${esc(m)}">`).join("")}</datalist>`;
    else if(f.type==="driver")inp=`<input type="text" id="${id}" list="drv_${id}" value="${esc(v)}"><datalist id="drv_${id}"><option value="auto">${drivers.map(d=>`<option value="${esc(d.number)}">${esc(d.name)}</option>`).join("")}</datalist>`;
    else if(f.type==="secret")inp=`<input type="password" id="${id}" autocomplete="new-password" placeholder="${f.is_set?"Saved - type to replace":"Not set"}">`+
      (f.is_set?`<label style="margin-top:6px;display:block"><input type="checkbox" id="${id}_clear"> remove saved password</label>`:"");
    else if(f.type==="cams")inp=`<input type="text" id="${id}" value="${esc((v||[]).join(", "))}"><div class="chips">${cams.map(c=>`<span class="chip" onclick="addCam('${id}','${esc(c)}')">${esc(c)}</span>`).join("")}</div>`;
    else if(f.type==="text")inp=`<textarea id="${id}">${esc(v)}</textarea>`;
    else inp=`<input type="text" id="${id}" value="${esc(v)}">`;
    return `<div class="field"><label for="${id}">${esc(f.label)}</label>${inp}${f.hint?`<small>${esc(f.hint)}</small>`:""}</div>`}).join("")}</div></div>`).join("");
  const names=[...new Set([...Object.keys(S.pronunciations),...Object.keys(S.pronouns)])];
  $("prons").innerHTML=names.map(n=>pronRow(n,S.pronunciations[n]||"",S.pronouns[n]||"")).join("");$("raw").value=S.raw}
function pronRow(n,say,pro){return `<div class="row pron"><input type="text" class="pn" placeholder="Name" value="${esc(n)}" style="width:150px"><label>says</label><input type="text" class="ps" placeholder="How to say it" value="${esc(say)}" style="width:150px"><label>pronouns</label><input type="text" class="pp" placeholder="he/him" value="${esc(pro)}" style="width:110px"></div>`}
function addPron(){$("prons").insertAdjacentHTML("beforeend",pronRow("","",""))}
function addCam(id,c){const e=$(id);const l=e.value.split(",").map(x=>x.trim()).filter(Boolean);if(!l.includes(c))l.push(c);e.value=l.join(", ")}
async function saveSettings(){const changes=[];S.groups.forEach((g,gi)=>g.fields.forEach((f,fi)=>{const e=$(`f_${gi}_${fi}`);if(!e)return;
  if(f.type==="secret"){const c=$(`f_${gi}_${fi}_clear`);changes.push({section:f.section,key:f.key,value:e.value,clear:!!(c&&c.checked)});e.value="";return}
  let v=f.type==="bool"?e.checked:e.value;if(f.type==="num"&&v==="")return;changes.push({section:f.section,key:f.key,value:v})}));
  const pron={},pro={};document.querySelectorAll(".pron").forEach(r=>{const n=r.querySelector(".pn").value.trim();if(!n)return;
    const s=r.querySelector(".ps").value.trim(),p=r.querySelector(".pp").value.trim();if(s)pron[n]=s;if(p)pro[n]=p});
  const j=await post("/api/settings",{changes,pronunciations:pron,pronouns:pro});if(j.ok){toast("Settings saved");loadSettings()}}
async function readLive(){const j=await (await fetch("/api/livecams")).json();
  if(!j.ok){toast(j.error,true);return}LIVE=j.cameras;
  const fid=(()=>{let r=null;S.groups.forEach((g,gi)=>g.fields.forEach((f,fi)=>{if(f.key==="custom_cameras")r=`f_${gi}_${fi}`}));return r})();
  $("livecams").innerHTML=`<span class="dim" style="width:100%">${esc(j.track)}: ${j.cameras.length} camera groups</span>`+
    j.cameras.map(c=>`<span class="chip" onclick="addCam('${fid}','${esc(c)}');toast('Added ${esc(c)} - click Save settings')">${esc(c)}</span>`).join("")}
async function saveRaw(){const j=await post("/api/rawconfig",{text:$("raw").value});if(j.ok){toast("config.toml saved");loadSettings()}}
pollStatus();loadProjects();pollJob();setInterval(pollJob,1000);setInterval(pollStatus,6000);
</script></body></html>
"""
