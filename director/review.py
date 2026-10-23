"""Review page: approve, fix and add highlight clips in your browser before (or after) recording.

    run.bat review

Opens a local page (only on this PC). Saving writes the changes into plan.json and
events.json; capture then records only what's new or changed.
"""
from __future__ import annotations

import json
import socket
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def fmt(t: float) -> str:
    t = max(0.0, float(t))
    return f"{int(t // 60)}:{t % 60:04.1f}"


def page_data(proj, race, det, plan, manifest_name: str = "clips.json") -> dict:
    from .detect import Race
    from .render import caption_for

    R = Race(race)
    by_id = {e["id"]: e for e in det["events"]}
    manifest = proj.load(manifest_name) if proj.has(manifest_name) else {}
    clips = []
    for c in plan["clips"]:
        ev = by_id.get(c.get("primary"))
        if c.get("caption") is not None:
            cap = c["caption"]
        else:
            d = caption_for(ev, race, R, (c["start"] + c["end"]) / 2) if ev else None
            cap = dict(tag=d[0], main=d[1], sub=d[2]) if d else dict(tag="", main="", sub="")
        lead = R.order_at(c["start"])
        man = manifest.get(str(c["id"]))
        video = None
        if man and proj.path("clips").joinpath(man["file"]).exists() and \
                abs(man.get("session_start", -99) - c["start"]) < 5:
            video = f"/video/{c['id']}"
        clips.append(dict(
            id=c["id"], keep=True, start=fmt(c["start"]), end=fmt(c["end"]), label=c.get("label", ""),
            type=c.get("type", ""), lap=R.lap_of(lead[0], c["start"]) if lead else 0,
            shots=[dict(at=round(s["at"] - c["start"], 1), car=s["car"], cam=s["cam"]) for s in c.get("shots", [])],
            caption=cap, note=c.get("note", ""), video=video))
    return dict(
        track=race["track"],
        drivers=[dict(idx=d["idx"], name=d["short"], number=d["number"], car=d.get("car", "")) for d in race["drivers"]],
        cameras=race.get("cameras", []),
        clips=clips,
        laps=[dict(lap=n, t=fmt(t)) for n, t in lap_starts(R)],
    )


def lap_starts(R) -> list[tuple[int, float]]:
    """(lap number, race time the leader started it) - helps find moments in the replay."""
    import numpy as np

    out, last = [], 0
    for i in range(R.i_green, len(R.t)):
        row = R.dff[i]
        if np.isnan(row).all():
            continue
        lap = max(1, int(np.nanmax(row)))
        if lap != last:
            out.append((lap, float(R.t[i])))
            last = lap
        if R.t[i] > R.checkered:
            break
    return out


def parse_t(s: str) -> float:
    s = str(s).strip()
    if ":" in s:
        m, sec = s.split(":", 1)
        return int(m) * 60 + float(sec)
    return float(s)


def apply_edits(proj, race, det, plan, edits: list[dict], plan_name: str = "plan.json") -> dict:
    """Turn the page's clip list back into plan.json / events.json."""
    from .detect import Race

    R = Race(race)
    drv = {d["idx"]: d for d in race["drivers"]}
    cams = {c["num"]: c["name"] for c in race.get("cameras", [])}
    old = {c["id"]: c for c in plan["clips"]}
    events = det["events"]
    next_ev = max([e["id"] for e in events] + [0]) + 1
    clips = []
    for e in edits:
        if not e.get("keep", True):
            continue
        start, end = parse_t(e["start"]), parse_t(e["end"])
        if end <= start + 1:
            end = start + 6
        o = old.get(e.get("id"))
        shots = []
        for s in sorted(e.get("shots") or [], key=lambda s: float(s["at"])):
            car = int(s["car"])
            cam = int(s["cam"])
            shots.append(dict(at=round(start + max(0.0, float(s["at"])), 2), car=car,
                              number=drv.get(car, {}).get("number", "1"), cam=cam, cam_name=cams.get(cam, str(cam)),
                              role="manual", ev=o.get("primary") if o else None))
        if not shots:
            continue  # a clip needs at least one car to follow
        shots[0]["at"] = round(start, 2)
        c = dict(o) if o else dict(events=[], type="custom", label="")
        c.update(start=round(start, 2), end=round(end, 2), shots=shots, note=(e.get("note") or "").strip())
        cap = e.get("caption") or {}
        c["caption"] = dict(tag=cap.get("tag", "").strip(), main=cap.get("main", "").strip(), sub=cap.get("sub", "").strip())
        if not o:  # a moment the app missed: give it an event so commentary has something to work with
            cars = list(dict.fromkeys(s["car"] for s in shots))
            ev = dict(id=next_ev, type="custom", t=round(start + 2.5, 2), start=start, end=end, cars=cars, score=5.0,
                      data=dict(lap=R.lap_of(cars[0], start), note=c["note"]))
            next_ev += 1
            events.append(ev)
            c["events"], c["primary"] = [ev["id"]], ev["id"]
            c["label"] = "  |  ".join(x for x in (c["caption"]["tag"], c["caption"]["main"]) if x) or "CUSTOM"
        elif c["caption"]["main"]:
            c["label"] = "  |  ".join(x for x in (c["caption"]["tag"], c["caption"]["main"], c["caption"]["sub"]) if x)
        clips.append(c)
    clips.sort(key=lambda c: c["start"])
    for i, c in enumerate(clips):
        c["id"] = i
    plan = dict(plan, clips=clips, total_seconds=round(sum(c["end"] - c["start"] for c in clips), 1), reviewed=True)
    proj.save("events.json", det)
    proj.save(plan_name, plan)
    return plan


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def review(proj, cfg, progress=print, plan_name: str = "plan.json", manifest_name: str = "clips.json") -> dict:
    race, det, plan = proj.load("race.json"), proj.load("events.json"), proj.load(plan_name)
    data = page_data(proj, race, det, plan, manifest_name)
    manifest = proj.load(manifest_name) if proj.has(manifest_name) else {}
    done = threading.Event()
    result = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body: bytes, ctype="application/json", extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/":
                return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            if self.path == "/data":
                return self._send(200, json.dumps(data).encode())
            if self.path.startswith("/video/"):
                m = manifest.get(self.path.split("/")[-1])
                if not m:
                    return self._send(404, b"")
                return self._video(proj.path("clips") / m["file"])
            self._send(404, b"")

        def _video(self, path: Path):
            size = path.stat().st_size
            rng = self.headers.get("Range")
            start, end = 0, size - 1
            if rng and rng.startswith("bytes="):
                a, _, b = rng[6:].partition("-")
                start = int(a or 0)
                end = int(b) if b else min(size - 1, start + 8 * 1024 * 1024)
            ctype = "video/mp4" if path.suffix.lower() in (".mp4", ".mov") else "video/x-matroska"
            self.send_response(206 if rng else 200)
            self.send_header("Content-Type", ctype)
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
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if self.path == "/save":
                try:
                    new = apply_edits(proj, race, det, plan, body.get("clips", []), plan_name)
                except Exception as e:  # noqa: BLE001
                    return self._send(400, json.dumps(dict(error=str(e))).encode())
                result["plan"] = new
                self._send(200, json.dumps(dict(ok=True, clips=len(new["clips"]),
                                                minutes=round(new["total_seconds"] / 60, 1))).encode())
                done.set()
                return
            self._send(404, b"")

    port = _free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    url = f"http://127.0.0.1:{port}/"
    progress(f"  review page: {url}")
    progress("  (opening it in your browser - check the clips, then click 'Save and continue')")
    webbrowser.open(url)
    try:
        while not done.wait(1.0):
            pass
    finally:
        import time

        time.sleep(1.0)  # let the page get its reply
        srv.shutdown()
    p = result["plan"]
    progress(f"  saved: {len(p['clips'])} clips, {p['total_seconds'] / 60:.1f} min")
    return p


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Highlight review</title>
<style>
:root{--bg:#0c0f14;--panel:#151a22;--panel2:#1d232e;--line:#2a3240;--text:#e8edf3;--dim:#93a0b2;--accent:#e10600;--ok:#2bb673}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.4 "Segoe UI",system-ui,sans-serif}
header{position:sticky;top:0;z-index:5;background:#0c0f14ee;backdrop-filter:blur(6px);border-bottom:1px solid var(--line);
padding:14px 20px;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
h1{font-size:20px;margin:0;font-weight:700;letter-spacing:.3px}h1 b{color:var(--accent)}
.sum{color:var(--dim)}.sp{flex:1}
button{background:var(--panel2);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:8px 14px;font:inherit;cursor:pointer}
button:hover{border-color:#4a5568}button.primary{background:var(--accent);border-color:var(--accent);font-weight:700}
button.small{padding:4px 9px;font-size:13px}
main{max-width:1180px;margin:0 auto;padding:18px 20px 80px}
.help{color:var(--dim);margin:0 0 16px}.laps{color:var(--dim);font-size:13px;margin:-6px 0 18px}
.clip{background:var(--panel);border:1px solid var(--line);border-left:4px solid var(--accent);border-radius:8px;padding:14px 16px;margin-bottom:14px}
.clip.off{opacity:.45;border-left-color:#555}
.row{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.num{font-weight:800;font-size:18px;width:34px}.label{font-weight:600;flex:1;min-width:240px}
.type{font-size:12px;color:var(--dim);text-transform:uppercase;letter-spacing:.6px}
label{color:var(--dim);font-size:13px;display:flex;gap:6px;align-items:center}
input,select,textarea{background:#0f131a;color:var(--text);border:1px solid var(--line);border-radius:5px;padding:6px 8px;font:inherit}
input.t{width:78px}textarea{width:100%;min-height:38px;resize:vertical}
.grid{display:grid;grid-template-columns:minmax(0,1.1fr) minmax(0,1fr);gap:16px;margin-top:12px}
@media (max-width:860px){.grid{grid-template-columns:1fr}}
.box{background:var(--panel2);border-radius:6px;padding:10px 12px}.box h3{margin:0 0 8px;font-size:13px;color:var(--dim);font-weight:600;text-transform:uppercase;letter-spacing:.6px}
.shot{display:flex;gap:8px;align-items:center;margin-bottom:6px;flex-wrap:wrap}.shot input{width:64px}
.cap input{width:100%;margin-bottom:6px}video{width:100%;border-radius:6px;background:#000;margin-top:10px}
.keep{display:flex;gap:6px;align-items:center;font-weight:600}
.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:var(--ok);color:#06210f;padding:12px 20px;border-radius:8px;font-weight:700;display:none}
</style></head><body>
<header><h1><b>HIGHLIGHTS</b> &nbsp;<span id="track"></span></h1><span class="sum" id="sum"></span><span class="sp"></span>
<button onclick="addClip()">+ Add a moment it missed</button><button class="primary" onclick="save()">Save and continue</button></header>
<main>
<p class="help">Untick clips you don't want. Times are race time (m:ss) as used by the app. Shots are seconds into the clip.
Notes go straight to the commentator as facts, e.g. <i>"the car in second pushed the leader wide"</i>. After recording, clips you change get re-recorded; the rest are kept.</p>
<div class="laps" id="laps"></div>
<div id="list"></div></main><div class="toast" id="toast"></div>
<script>
let D=null;
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const secs=s=>{s=String(s).trim();if(s.includes(":")){const[m,x]=s.split(":");return(+m)*60+(+x)}return +s};
function carOpts(sel){return D.drivers.map(d=>`<option value="${d.idx}" ${d.idx==sel?"selected":""}>${esc(d.name)} (#${esc(d.number)})</option>`).join("")}
function camOpts(sel){return D.cameras.map(c=>`<option value="${c.num}" ${c.num==sel?"selected":""}>${esc(c.name)}</option>`).join("")}
function render(){
  document.getElementById("track").textContent=D.track;
  document.getElementById("laps").textContent="Lap starts: "+D.laps.map(l=>`L${l.lap} ${l.t}`).join(" · ");
  const list=document.getElementById("list");list.innerHTML="";
  D.clips.forEach((c,i)=>{
    const el=document.createElement("div");el.className="clip"+(c.keep?"":" off");
    el.innerHTML=`<div class="row"><span class="num">${i+1}</span>
      <label class="keep"><input type="checkbox" ${c.keep?"checked":""} onchange="D.clips[${i}].keep=this.checked;render()">Keep</label>
      <span class="label">${esc(c.label||"New moment")}<br><span class="type">${esc(c.type||"custom")} · lap ${c.lap||"?"}</span></span>
      <label>Start <input class="t" value="${esc(c.start)}" onchange="D.clips[${i}].start=this.value;sum()"></label>
      <label>End <input class="t" value="${esc(c.end)}" onchange="D.clips[${i}].end=this.value;sum()"></label></div>
      <div class="grid"><div class="box"><h3>Camera shots</h3>${c.shots.map((s,j)=>`<div class="shot">
        <label>at <input value="${s.at}" onchange="D.clips[${i}].shots[${j}].at=+this.value">s</label>
        <select onchange="D.clips[${i}].shots[${j}].car=+this.value">${carOpts(s.car)}</select>
        <select onchange="D.clips[${i}].shots[${j}].cam=+this.value">${camOpts(s.cam)}</select>
        <button class="small" onclick="D.clips[${i}].shots.splice(${j},1);render()">✕</button></div>`).join("")}
        <button class="small" onclick="addShot(${i})">+ shot</button>
        ${c.video?`<video src="${c.video}" controls preload="metadata"></video>`:""}</div>
      <div class="box cap"><h3>Caption</h3>
        <input placeholder="Tag (e.g. OVERTAKE)" value="${esc(c.caption.tag)}" onchange="D.clips[${i}].caption.tag=this.value">
        <input placeholder="Main line" value="${esc(c.caption.main)}" onchange="D.clips[${i}].caption.main=this.value">
        <input placeholder="Sub line" value="${esc(c.caption.sub)}" onchange="D.clips[${i}].caption.sub=this.value">
        <h3 style="margin-top:6px">Note for the commentator</h3>
        <textarea placeholder="What really happened, in plain words" onchange="D.clips[${i}].note=this.value">${esc(c.note)}</textarea></div></div>`;
    list.appendChild(el)});
  sum()}
function sum(){const k=D.clips.filter(c=>c.keep);const t=k.reduce((a,c)=>a+Math.max(0,secs(c.end)-secs(c.start)),0);
  document.getElementById("sum").textContent=`${k.length} clips · ${(t/60).toFixed(1)} min`}
function addShot(i){const c=D.clips[i];const last=c.shots[c.shots.length-1]||{car:D.drivers[0].idx,cam:(D.cameras[0]||{}).num||1,at:0};
  c.shots.push({at:Math.round((+last.at+6)*10)/10,car:last.car,cam:last.cam});render()}
function addClip(){const cam=(D.cameras.find(c=>/tv1/i.test(c.name))||D.cameras[0]||{num:1}).num;
  D.clips.push({id:null,keep:true,start:"0:00.0",end:"0:12.0",label:"",type:"custom",lap:"",shots:[{at:0,car:D.drivers[0].idx,cam}],
  caption:{tag:"HIGHLIGHT",main:"",sub:""},note:"",video:null});render();window.scrollTo(0,document.body.scrollHeight)}
async function save(){const r=await fetch("/save",{method:"POST",body:JSON.stringify({clips:D.clips})});const j=await r.json();
  const t=document.getElementById("toast");
  if(j.ok){t.textContent=`Saved ${j.clips} clips (${j.minutes} min). You can close this tab - the app carries on.`;t.style.background="var(--ok)"}
  else{t.textContent="Couldn't save: "+j.error;t.style.background="#e1a000"}t.style.display="block"}
fetch("/data").then(r=>r.json()).then(d=>{D=d;render()});
</script></body></html>
"""
