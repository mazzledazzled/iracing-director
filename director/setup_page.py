"""Race setup page: shown right after the replay is scanned, when the drivers are known.
Pick the featured driver (or type a name / car number) and the video length, then
the app carries on picking highlights with those choices.
"""
from __future__ import annotations

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .detect import match_driver


def choose(proj, race: dict, mode: str, defaults: dict, progress=print) -> dict:
    """mode: "highlights" or "short". Returns {"featured": "all"|"auto"|<number>, "length": seconds}."""
    from .review import _free_port

    last = proj.load("setup.json") if proj.has("setup.json") else {}
    rec = next((d for d in race["drivers"] if d["idx"] == race.get("focus_idx")), None)
    data = dict(
        mode=mode, track=race.get("track", ""),
        drivers=[dict(number=d["number"], name=d["name"], car=d.get("car", "")) for d in race["drivers"]],
        recorder=f"#{rec['number']} {rec['name']}" if rec else None,
        featured=str(last.get(f"featured_{mode}", defaults.get("featured", "all"))),
        length=float(last.get(f"length_{mode}", defaults.get("length", 360 if mode == "highlights" else 30))),
    )
    done = threading.Event()
    result: dict = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body: bytes, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/":
                return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            if self.path == "/data":
                return self._send(200, json.dumps(data).encode())
            self._send(404, b"")

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            featured = str(body.get("featured", "all")).strip() or "all"
            try:
                idx = match_driver(race, featured)
            except ValueError as e:
                return self._send(400, json.dumps(dict(error=str(e))).encode())
            num = next((d["number"] for d in race["drivers"] if d["idx"] == idx), None) if idx is not None else None
            try:
                length = max(5.0, float(body.get("length")))
            except (TypeError, ValueError):
                return self._send(400, json.dumps(dict(error="Length must be a number")).encode())
            result.update(featured=num if num else "all", length=length)
            name = next((f"#{d['number']} {d['name']}" for d in race["drivers"] if d["idx"] == idx), "All drivers")
            self._send(200, json.dumps(dict(ok=True, who=name)).encode())
            done.set()

    port = _free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    progress(f"  setup page: {url}")
    progress("  (opening it in your browser - pick the featured driver and length, then click Continue)")
    webbrowser.open(url)
    try:
        while not done.wait(1.0):
            pass
    finally:
        import time

        time.sleep(1.0)
        srv.shutdown()
    last[f"featured_{mode}"], last[f"length_{mode}"] = result["featured"], result["length"]
    proj.save("setup.json", last)
    who = next((f"#{d['number']} {d['name']}" for d in race["drivers"] if d["number"] == result["featured"]), "all drivers")
    progress(f"  featured: {who}  |  length: {result['length']:.0f}s")
    return result


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Race setup</title>
<style>
:root{--bg:#0b0e13;--panel:#141922;--panel2:#1b212c;--line:#283041;--text:#e9eef4;--dim:#93a0b2;--accent:#e10600;--ok:#2bb673}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 "Segoe UI",system-ui,sans-serif}
main{max-width:860px;margin:0 auto;padding:28px 20px 60px}
h1{margin:0 0 4px;font-size:24px}h1 b{color:var(--accent)}.dim{color:var(--dim)}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:18px;margin-top:16px}
h2{margin:0 0 10px;font-size:17px}
.opts{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:8px}
.opt{display:flex;gap:10px;align-items:center;background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:9px 12px;cursor:pointer}
.opt:hover{border-color:#4a5568}.opt.on{border-color:var(--accent);background:#2a1416}
.opt input{accent-color:var(--accent)}.opt small{display:block;color:var(--dim);font-size:12px}
.num{display:inline-block;font-weight:800;min-width:56px}
input[type=text],input[type=number]{background:#0e1218;color:var(--text);border:1px solid var(--line);border-radius:6px;padding:9px 11px;font:inherit}
input[type=text]{width:100%}input[type=number]{width:110px}
button{background:var(--accent);color:#fff;border:none;border-radius:8px;padding:12px 22px;font:inherit;font-weight:700;cursor:pointer;margin-top:18px}
.err{color:#ff8a8a;margin-top:10px}.ok{background:var(--ok);color:#06210f;padding:12px 16px;border-radius:8px;font-weight:700;margin-top:16px;display:none}
</style></head><body><main>
<h1><b>RACE SETUP</b> <span id="track"></span></h1>
<div class="dim" id="sub"></div>
<div class="card"><h2>Featured driver</h2>
  <div class="opts" id="opts"></div>
  <p class="dim" style="margin:14px 0 6px">Or type a car number or name:</p>
  <input type="text" id="typed" placeholder="Car number or driver name" oninput="typedIn()">
  <div class="dim" id="hint" style="margin-top:10px;font-size:14px"></div></div>
<div class="card"><h2 id="lenlabel"></h2><input type="number" id="len" step="0.5" min="0.1"> <span class="dim" id="unit"></span></div>
<button onclick="go()">Continue</button><div class="err" id="err"></div><div class="ok" id="ok"></div>
<script>
let D,pick="all";const $=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function render(){$("track").textContent=D.track;
  $("sub").textContent=D.mode==="short"?"Race in 30: whose race should the short tell?":"Highlights video: who gets the spotlight?";
  const items=[{v:"all",t:"All drivers",s:"Everyone equal - screen time shared around the field"}];
  if(D.recorder)items.push({v:"auto",t:"Whoever recorded the replay",s:D.recorder});
  D.drivers.forEach(d=>items.push({v:d.number,t:`<span class="num">#${esc(d.number)}</span>${esc(d.name)}`,s:d.car,raw:true}));
  $("opts").innerHTML=items.map(i=>`<label class="opt ${i.v===pick?"on":""}"><input type="radio" name="f" value="${esc(i.v)}" ${i.v===pick?"checked":""} onchange="pick=this.value;$('typed').value='';render()">
    <span>${i.raw?i.t:esc(i.t)}<small>${esc(i.s)}</small></span></label>`).join("");
  $("hint").textContent=pick==="all"?"Every driver is treated the same.":
    (D.mode==="short"?"The short follows this driver: their start, passes, spins and finish.":"This driver gets extra screen time, onboard shots and a highlight in the position tower.")}
function typedIn(){if($("typed").value.trim()){pick="";document.querySelectorAll(".opt").forEach(o=>o.classList.remove("on"));
  document.querySelectorAll('input[name=f]').forEach(r=>r.checked=false)}}
async function go(){const featured=$("typed").value.trim()||pick||"all";
  let len=parseFloat($("len").value);if(!(len>0)){$("err").textContent="Enter a length";return}
  if(D.mode!=="short")len=len*60;
  const r=await fetch("/",{method:"POST",body:JSON.stringify({featured,length:len})});const j=await r.json();
  if(!j.ok){$("err").textContent=j.error;return}$("err").textContent="";
  $("ok").textContent=`Got it: ${j.who}. You can close this tab - the app carries on.`;$("ok").style.display="block"}
fetch("/data").then(r=>r.json()).then(d=>{D=d;
  const known=["all","auto"].concat(D.drivers.map(x=>x.number));
  if(known.includes(D.featured))pick=D.featured;else{pick="";$("typed").value=D.featured}
  $("lenlabel").textContent=D.mode==="short"?"Length of the short":"Length of the highlights video";
  $("unit").textContent=D.mode==="short"?"seconds":"minutes";
  $("len").value=D.mode==="short"?D.length:Math.round(D.length/30)/2;render()});
</script></main></body></html>
"""
