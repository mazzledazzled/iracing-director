"""Write the commentary script with a local LLM (Ollama).

Each clip gets one or more "cues": a moment in race time where the
commentator starts talking, plus a word budget so the line fits before the
next cue. The LLM only sees real race facts, so it can't make things up
(much)."""
from __future__ import annotations

import json
import random
import re
import urllib.error
import urllib.request

from .detect import Race

CUE_GAP = 4.0


# ---------------------------------------------------------------- cue planning
def _cue_time(ev: dict, clip: dict) -> float:
    t, typ = ev["t"], ev["type"]
    if typ == "start":
        at = t - 3.0
    elif typ == "finish":
        at = t - (6 if ev["data"].get("focus_finish") else 9)
    elif typ == "overtake":
        at = t - 3.0
    elif typ == "battle":
        at = clip["start"] + 0.8
    elif typ == "incident":
        at = t - 0.3
    else:
        at = t - 2
    return min(max(at, clip["start"] + 0.5), clip["end"] - 2.0)


def plan_cues(plan: dict, det: dict, wps: float) -> list[dict]:
    by_id = {e["id"]: e for e in det["events"]}
    cues = []
    for clip in plan["clips"]:
        evs = sorted((by_id[i] for i in clip["events"]), key=lambda e: e["t"])
        cs = []
        for e in evs:
            at = _cue_time(e, clip)
            if cs and at - cs[-1]["at"] < CUE_GAP:
                if e["score"] > by_id[cs[-1]["event"]]["score"]:
                    cs[-1].update(event=e["id"])  # keep the time, talk about the bigger moment
                continue
            cs.append(dict(clip=clip["id"], at=round(at, 2), event=e["id"]))
        if cs and cs[0]["at"] - clip["start"] > 7:
            cs.insert(0, dict(clip=clip["id"], at=round(clip["start"] + 0.5, 2), event=cs[0]["event"], setup=True))
        for i, c in enumerate(cs):
            nxt = cs[i + 1]["at"] if i + 1 < len(cs) else clip["end"] - 0.3
            c["words"] = int(max(5, min(40, (nxt - c["at"] - 0.7) * wps)))
        cues += cs
    return cues


# ---------------------------------------------------------------- facts
class Facts:
    def __init__(self, race: dict, det: dict, pronouns: dict | None = None):
        self.pronouns = pronouns
        self.race = race
        self.det = det
        self.r = Race(race)
        self.drv = {d["idx"]: d for d in race["drivers"]}

    def name(self, idx):
        return self.drv.get(idx, {}).get("name", "Unknown")

    def short(self, idx):
        return self.drv.get(idx, {}).get("short", "Unknown")

    def car(self, idx) -> str:
        """Short, speakable car name: 'Ford Mustang GT4' -> 'Mustang'."""
        full = self.drv.get(idx, {}).get("car", "") or ""
        known = {"mustang": "Mustang", "bmw": "BMW", "mercedes": "Mercedes", "aston": "Aston Martin",
                 "porsche": "Porsche", "mclaren": "McLaren", "audi": "Audi", "toyota": "Toyota",
                 "chevrolet": "Chevrolet", "corvette": "Corvette", "ferrari": "Ferrari", "lamborghini": "Lamborghini",
                 "mazda": "Mazda", "mx-5": "MX-5", "honda": "Honda", "acura": "Acura", "hyundai": "Hyundai"}
        low = full.lower()
        for k, v in known.items():
            if k in low:
                return v
        return full.split()[0] if full else ""

    def who(self, idx, t):
        return dict(surname=self.short(idx), car=self.car(idx), position=self.r.position(idx, t))

    def base(self) -> dict:
        r = self.race
        f = self.det.get("focus")
        pron = {k: v for k, v in (self.pronouns or {}).items()}
        extra = dict(driver_pronouns=pron) if pron else {}
        return dict(**extra,
            track=r["track"] + (f" ({r['track_config']})" if r.get("track_config") else ""),
            drivers_and_cars={self.short(d["idx"]): self.car(d["idx"]) for d in r["drivers"]},
            field_size=len(r["drivers"]),
            race_laps=r.get("session_laps"),
            featured_driver=self.name(f) if f is not None else None,
        )

    def top(self, t, n=5):
        order = self.r.order_at(t)
        return [f"P{i + 1} {self.short(c)}" for i, c in enumerate(order[:n])]

    def moment(self, ev: dict, at: float, setup: bool = False) -> tuple[dict, str]:
        d, typ, t = ev["data"], ev["type"], ev["t"]
        f = self.det.get("focus")
        m: dict = dict(running_order_top5=self.top(at))
        if self.r.under_caution(at):
            m["race_status"] = "full-course caution: the field is running slowly behind the leader, no racing"
        if f is not None:
            m["featured_driver_position"] = self.r.position(f, at)
        if setup:
            m["situation"] = "the clip is starting; set the scene before the action"
        if typ == "start":
            grid = d.get("grid", [])
            m.update(event="race start", pole_sitter=self.who(grid[0], t - 1) if grid else None,
                     front_row=[self.short(c) for c in grid[:2]], weather=self.race.get("weather"))
            ask = "Call the start of the race: the green flag and the field getting away."
        elif typ == "finish" and not d.get("focus_finish"):
            order = d.get("order", [])
            m.update(event="chequered flag", winner=self.who(order[0], t) if order else None,
                     podium=[self.short(c) for c in order[:3]])
            ask = "Call the winner taking the chequered flag."
        elif typ == "finish":
            m.update(event="featured driver finishes", driver=self.who(ev["cars"][0], t + 1))
            ask = "Call the featured driver crossing the line and where they finished."
        elif typ == "overtake":
            m.update(event="overtake" if d.get("completed") else "attempted pass, defender held on",
                     lap=d["lap"], attacker=self.who(d["passer"], t - 5), defender=self.who(d["passed"], t - 5),
                     for_position=d["position"], lead_change=d.get("lead_change", False),
                     )
            ask = ("Call this pass as it happens." if d.get("completed") else
                   "Call this attack - they go side by side but the defender keeps the place.")
        elif typ == "battle":
            m.update(event="close battle", lap=self.r.lap_of(d["defender"], at), battling_for_position=d["position"],
                     defender=self.who(d["defender"], at), attacker=self.who(d["attacker"], at),
                     battle_running_since_lap=d["lap_from"],
                     passes_in_battle=d.get("passes", 0))
            ask = "Describe this battle building, without claiming a pass has happened yet." if not setup else \
                "Set up this battle: who is chasing whom and for what position."
        elif typ == "restart":
            m.update(event="restart after a caution", lap=d.get("lap"),
                     leader=self.who(d["leader"], t) if d.get("leader") is not None else None,
                     order=[self.short(c) for c in d.get("order", [])])
            ask = "Call the restart: the leader gets going again and the field races behind."
        elif typ == "incident":
            m.update(event={"crash": "multi-car crash", "spin": "spin", "contact": "incident",
                            "offtrack": "off the track"}[d["severity"]],
                     lap=d["lap"], drivers=[self.who(c, t - 1) for c in ev["cars"][:4]])
            ask = ("React to this as it happens - say who is involved, don't describe how it happened."
                   if d["severity"] != "offtrack" else "Say this driver has run off the track, nothing more.")
        elif typ == "custom":
            m.update(event="a moment picked by the race director", lap=d.get("lap"),
                     drivers=[self.who(c, t) for c in ev["cars"][:3]])
            ask = "Call this moment, built around the race director's note if there is one."
        else:
            ask = "Comment on what's happening."
        return m, ask


# ---------------------------------------------------------------- LLM
SYSTEM = """You are {style}, calling the highlights of an online car race.
FACTS - you CANNOT see the video, you only know the facts given:
- A race_director_note is a TRUE account of what happened, written by a human who watched it. Build the line around it and trust it over everything else.
- Describe only what the facts say happened. If the facts say "overtake", say who passed whom and for what position.
- NEVER mention corners, turn numbers, inside/outside lines, braking, contact, damage, the safety car, weather, tyres, strategy or cameras unless that exact thing is in the facts.
- NEVER invent nationalities, ages, experience, teams, sponsors, lap times, gaps or history. Never say a driver is out of the race unless the facts say so.
- A "battle" means two cars running close together. Don't claim a pass happened unless the facts say so.
- Use the exact positions given. Only call someone the winner if the facts name them as the winner.
- If driver_pronouns are given, use exactly those pronouns for those drivers. Otherwise use their name, not he or she.
- Each driver's car is listed with them. Never give a driver a different car. Don't use car numbers.
- Don't say "side by side", "wheel to wheel", "down the inside" or "around the outside".
VOICE - sound like a real person on the radio, not a hype reel:
- Relaxed, conversational and dry. The humour comes from understatement and wry observation about what just happened, never from puns or shouting.
- Short sentences, contractions, plain words. At most one exclamation mark, and only for a pass for the lead, a crash or the finish.
- A little cheeky about mistakes is fine, never mean.
- Banned (corny): "what a move", "what a pass", "absolutely", "incredible", "thrilling", "stunning", "folks", "ladies and gentlemen", "masterclass", "commanding", "intense", "the battle intensifies", "edge of your seat", "here we go".
- The tone, as examples only ([A] and [B] stand for drivers' names; don't copy the lines):
  "And [A] goes by. [B] will be thinking about that one tonight."
  "[A]'s mirrors are getting a real workout this lap."
  "Well, that's one way to have a look at the grass."
  "[A] back in front. Nobody's more pleased about it than [A]."
FORMAT:
- Write exactly what is said out loud: one or two short sentences, at most {words} words.
- No stage directions, sound effects, emojis, hashtags, quotation marks, lists or speaker labels.
- Refer to drivers by the "surname" field. Say positions as words ("for third", "into the lead").
- Don't reuse openings or jokes from the previous lines. Don't mention iRacing, simulators or video games.
"""

BANNED = ["what a move", "what a pass", "absolutely", "incredible", "thrilling", "stunning", "folks",
          "ladies and gentlemen", "masterclass", "commanding", "intense", "intensifies", "edge of your seat",
          "camera", "here we go", "wheel-to-wheel", "wheel to wheel", "side by side", "side-by-side"]


PERSONAS = {
    "gilmore": """You rewrite lines of car-race commentary as if Happy Gilmore - the hot-headed ex-hockey player
from the 1996 comedy - had grabbed the microphone. His voice: a short fuse, yelling at nobody in particular,
taking every pass personally, hockey comparisons (checking, the penalty box, the boards, a slapshot, dropping the
gloves), mock outrage at anyone slow, cocky trash talk, and the odd sudden soft moment (his grandma would love this).
Blue-collar, loud, funny.""",
}

PERSONA_RULES = """Rewrite the line below in that voice.
- Keep EVERY fact exactly: the same drivers, cars, positions, laps and who passed or beat whom. Add no new facts.
- At most {words} words. One or two short sentences. It's spoken out loud.
- No swearing or slurs (it goes on YouTube). Don't quote lines from the film or name its characters.
- Output only the new line - no quotes, labels or stage directions."""


class Writer:
    def __init__(self, cfg):
        self.c = cfg.commentary
        self.offline = False

    def _ollama(self, system: str, user: str) -> str:
        body = json.dumps(dict(
            model=self.c.model, stream=False,
            messages=[dict(role="system", content=system), dict(role="user", content=user)],
            options=dict(temperature=float(self.c.temperature), num_predict=200, num_ctx=4096),
            keep_alive="10m",
        )).encode()
        req = urllib.request.Request(self.c.ollama_url.rstrip("/") + "/api/chat", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read())["message"]["content"]

    timeout = 900  # the first request also loads the model into the GPU

    def warm_up(self, progress=print):
        """Load the model once and measure its speed, so problems show up clearly."""
        import time

        progress(f"  loading {self.c.model} into the GPU (first time can take a minute or two)...")
        t0 = time.time()
        try:
            self._ollama("Reply with one word.", "Say ready.")
        except (TimeoutError, OSError) as e:
            raise RuntimeError(
                f"Ollama didn't answer within {self.timeout // 60} minutes ({e}). The model is probably too big to fit "
                f"in free GPU memory. Close iRacing and OBS (they use GPU memory) and try again, or switch to a "
                f"smaller model in config.toml, e.g.  model = \"gemma3:12b\"  (run: ollama pull gemma3:12b)."
            ) from e
        t1 = time.time()
        self._ollama("Reply with one short sentence.", "Describe a race start in one sentence.")
        per_line = time.time() - t1
        progress(f"  model ready ({t1 - t0:.0f}s to load, {per_line:.1f}s per line)")
        self.timeout = max(120, int(per_line * 8))
        if per_line > 20:
            progress("  NOTE: that's slow - the model probably doesn't fit in your GPU memory and is partly running on the CPU.\n"
                     "  Close iRacing/OBS while this runs, or use a smaller model (gemma3:12b).")

    def unload(self):
        """Free the GPU memory straight away so the voice step has room."""
        if self.offline:
            return
        try:
            body = json.dumps(dict(model=self.c.model, keep_alive=0)).encode()
            req = urllib.request.Request(self.c.ollama_url.rstrip("/") + "/api/generate", data=body,
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=30).read()
        except Exception:
            pass

    def check(self) -> str | None:
        try:
            with urllib.request.urlopen(self.c.ollama_url.rstrip("/") + "/api/tags", timeout=5) as resp:
                tags = [m["name"] for m in json.loads(resp.read()).get("models", [])]
        except (urllib.error.URLError, OSError) as e:
            return f"Ollama isn't running at {self.c.ollama_url} ({e})."
        want = self.c.model if ":" in self.c.model else self.c.model + ":latest"
        if want not in tags:
            if tags:  # use whatever model is installed rather than the basic built-in lines
                print(f"  NOTE: '{self.c.model}' isn't downloaded (run: ollama pull {self.c.model}). "
                      f"Using '{tags[0]}' for now.")
                self.c["model"] = tags[0]
                return None
            return f"Ollama doesn't have the model '{self.c.model}'. Run:  ollama pull {self.c.model}"
        return None

    def persona(self, text: str, words: int, previous: list[str]) -> str:
        """Second pass: rewrite a finished line in the chosen persona's voice."""
        key = str(self.c.get("persona", "") or "").strip()
        if not key or key.lower() == "none" or self.offline:
            return text
        desc = PERSONAS.get(key.lower(), f"You rewrite lines of car-race commentary in this voice: {key}.")
        system = desc + "\n\n" + PERSONA_RULES.format(words=words + 4)
        user = ("Earlier lines (don't repeat their jokes):\n" + "\n".join(previous[-4:]) + "\n\n" if previous else "") + \
               f"Line to rewrite: {text}"
        for _ in range(3):
            out = clean(self._ollama(system, user), words + 4)
            if out:
                return out
        return text

    def line(self, facts: dict, ask: str, words: int, previous: list[str]) -> str:
        if self.offline:
            return template_line(facts, words)
        system = SYSTEM.format(style=self.c.style, words=words)
        user = (
            "Race facts:\n" + json.dumps(facts, ensure_ascii=False, indent=1) +
            ("\n\nPrevious lines (don't repeat them):\n" + "\n".join(previous[-4:]) if previous else "") +
            f"\n\n{ask} Maximum {words} words."
        )
        best = ""
        for _ in range(4):
            text = clean(self._ollama(system, user), words)
            if not text or any(text.lower() == p.lower() for p in previous):
                continue
            hits = [b for b in BANNED if b in text.lower()]
            if not hits:
                return text
            best = best or text
            user += f"\n\nDon't use these words: {', '.join(hits)}. Say it more plainly."
        return best or template_line(facts, words)


def clean(text: str, words: int) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = re.sub(r"\*[^*]*\*|\([^)]*\)|\[[^\]]*\]", "", text)
    text = re.sub(r"^\s*[\w ]{0,20}:\s", "", text)  # "Commentator: "
    text = text.replace('"', "").replace("“", "").replace("”", "").replace("#", "")
    text = re.sub(r"\s+", " ", text).strip()
    w = text.split()
    if len(w) > words:
        cut = " ".join(w[:words])
        enough = max(4, words // 2)
        m = re.search(r"^(.*[.!?])", cut)  # last full sentence that fits
        c = re.search(r"^(.*),", cut)  # else last clause that fits
        if m and len(m.group(1).split()) >= enough:
            text = m.group(1)
        elif c and len(c.group(1).split()) >= enough:
            text = c.group(1) + "!"
        else:
            text = cut.rstrip(",;:") + "!"
    return text


ORD = ["", "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth",
       "eleventh", "twelfth", "thirteenth", "fourteenth", "fifteenth", "sixteenth", "seventeenth",
       "eighteenth", "nineteenth", "twentieth"]


def ordinal(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    if 0 < n < len(ORD):
        return ORD[n]
    suf = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def template_line(m: dict, words: int) -> str:
    """Used when no LLM is available, so the pipeline still produces a video."""
    ev = m.get("event", "")
    s = lambda k: (m.get(k) or {}).get("surname", "")
    pick = random.choice
    if ev == "race start":
        return pick(["Green flag, and away they go.", "And they're off. Everybody behave."])
    if ev == "chequered flag":
        return f"{s('winner')} takes the chequered flag and wins it!"
    if ev == "featured driver finishes":
        d = m.get("driver") or {}
        return f"{d.get('surname', '')} brings it home in {ordinal(d.get('position'))}."
    if ev == "overtake":
        return pick([f"{s('attacker')} gets by {s('defender')} for {ordinal(m['for_position'])}!",
                     f"And {s('attacker')} goes by {s('defender')}. That's {ordinal(m['for_position'])}."])
    if ev.startswith("attempted"):
        return f"{s('attacker')} has a look, but {s('defender')} holds on!"
    if ev == "close battle":
        return f"{s('attacker')} all over the back of {s('defender')} in the fight for {ordinal(m['battling_for_position'])}."
    if ev.startswith("a moment picked"):
        names = " and ".join(d["surname"] for d in m.get("drivers", [])[:2])
        return (m.get("race_director_note") or f"Keep an eye on {names} here.").rstrip(".") + "."
    if "drivers" in m:
        names = " and ".join(d["surname"] for d in m["drivers"][:2])
        return {"spin": f"Oh, {names} has spun!", "multi-car crash": f"Big trouble! {names} are involved!",
                "off the track": f"{names} runs wide, off the circuit!"}.get(ev, f"Trouble for {names}!")
    return "What a race this is."


def mood_for(ev: dict, setup: bool = False) -> str:
    """How excited the voice should sound for this moment."""
    if setup:
        return "normal"
    t, d = ev["type"], ev["data"]
    if t in ("start", "finish", "restart"):
        return "hype"
    if t == "overtake":
        return "hype" if d.get("completed") and (d.get("lead_change") or (d.get("position") or 99) <= 3) else "normal"
    if t == "incident":
        return "hype" if d.get("severity") in ("crash", "spin") else "normal"
    return "normal"


def write_script(race: dict, det: dict, plan: dict, cfg, progress=print) -> dict:
    facts = Facts(race, det, cfg.get("pronouns"))
    writer = Writer(cfg)
    problem = writer.check()
    if problem:
        progress(f"  WARNING: {problem}\n  Using simple built-in lines instead. Fix that and re-run 'commentary' for real commentary.")
        writer.offline = True
    else:
        writer.warm_up(progress)
    by_id = {e["id"]: e for e in det["events"]}
    wps = float(cfg.commentary.words_per_second)
    cues = plan_cues(plan, det, wps)
    base = facts.base()
    previous: list[str] = []
    lines = []

    # intro over the title card
    start = next((e for e in det["events"] if e["type"] == "start"), None)
    intro_facts = dict(base, **(facts.moment(start, start["t"] - 2)[0] if start else {}))
    intro_facts["event"] = "welcome / intro before the start"
    text = writer.line(intro_facts, "Welcome viewers to these race highlights: the track, the cars, who's on pole.", 30, previous) \
        if not writer.offline else f"Welcome to {race['track']}, {len(race['drivers'])} drivers ready to go racing."
    lines.append(dict(kind="intro", text=text, mood="calm"))
    previous.append(text)
    progress(f"  intro: {text}")

    clips = {c["id"]: c for c in plan["clips"]}
    for c in cues:
        ev = by_id[c["event"]]
        m, ask = facts.moment(ev, c["at"], c.get("setup", False))
        shots = [s for s in clips[c["clip"]]["shots"] if s["at"] <= c["at"] + 0.5] or clips[c["clip"]]["shots"][:1]
        if shots:
            m["talk_mostly_about"] = facts.short(shots[-1]["car"])  # the car on screen
        note = clips[c["clip"]].get("note")
        if note:
            m["race_director_note"] = note
        text = writer.line(dict(base, **m), ask, c["words"], previous)
        lines.append(dict(kind="clip", clip=c["clip"], at=c["at"], event=c["event"], words=c["words"], text=text,
                          mood=mood_for(ev, c.get("setup", False))))
        previous.append(text)
        progress(f"  clip {c['clip'] + 1}: {text}")

    # outro over the results card
    fin = next((e for e in det["events"] if e["type"] == "finish" and not e["data"].get("focus_finish")), None)
    order = fin["data"].get("order", []) if fin else []
    f = det.get("focus")
    outro = dict(base, event="wrap-up after the race", podium=[facts.short(x) for x in order[:3]],
                 winner=facts.who(order[0], det["checkered"] + 30) if order else None)
    if f is not None and f in order:
        outro["featured_driver_result"] = f"P{order.index(f) + 1}"
    wrap = ("Wrap up the race: the winner, the podium, and the featured driver's result. Sign off."
            if f is not None else "Wrap up the race: the winner and the podium. Sign off.")
    text = writer.line(outro, wrap, 35, previous) \
        if not writer.offline else (f"{facts.short(order[0])} wins at {race['track_short']}. Thanks for watching." if order else "Thanks for watching.")
    lines.append(dict(kind="outro", text=text, mood="calm"))
    progress(f"  outro: {text}")
    persona = str(cfg.commentary.get("persona", "") or "").strip()
    if persona and persona.lower() != "none" and not writer.offline:
        progress(f"  rewriting in the '{persona}' voice...")
        done: list[str] = []
        for ln in lines:
            words = ln.get("words", 35)
            ln["plain"] = ln["text"]
            ln["text"] = writer.persona(ln["text"], words, done)
            if "!" in ln["text"] and ln.get("mood") in ("normal", "calm"):
                ln["mood"] = "hype"  # he's yelling, let the voice yell too
            done.append(ln["text"])
            progress(f"    {ln['text']}")
    writer.unload()
    return dict(lines=lines, llm=None if writer.offline else cfg.commentary.model,
                persona=persona or None)
