"""Broadcast-style graphics drawn with Pillow: position tower, lower thirds,
title and results cards, and the transition stinger. Everything is drawn at
1080p proportions and scaled to the output height."""
from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .config import ROOT

FONT_DIR = ROOT / "assets" / "fonts"

# palette
BG = (12, 15, 20)
PANEL = (16, 19, 25, 235)
PANEL_2 = (26, 31, 40, 240)
ROW_HI = (40, 47, 60, 245)
ACCENT = (225, 6, 0)
WHITE = (255, 255, 255)
GREY = (154, 164, 178)
DIM = (100, 110, 125)
GOLD = (245, 197, 24)
SILVER = (200, 205, 212)
BRONZE = (205, 127, 50)
YELLOW = (255, 210, 0)

MAKE_COLORS = {
    "mustang": (30, 91, 198), "ford": (30, 91, 198),
    "bmw": (79, 163, 224),
    "mercedes": (0, 161, 156), "amg": (0, 161, 156),
    "aston": (43, 182, 115),
    "porsche": (213, 0, 28), "mclaren": (255, 128, 0), "audi": (187, 10, 48),
    "toyota": (235, 10, 30), "chevrolet": (242, 197, 0), "corvette": (242, 197, 0),
    "ferrari": (220, 0, 0), "lamborghini": (141, 182, 0), "mazda": (160, 160, 170),
    "honda": (200, 16, 46), "acura": (200, 16, 46), "hyundai": (0, 44, 95), "cadillac": (180, 150, 90),
}


def make_color(car: str):
    low = (car or "").lower()
    for k, v in MAKE_COLORS.items():
        if k in low:
            return v
    h = hashlib.md5(low.encode()).digest()
    return (60 + h[0] % 180, 60 + h[1] % 180, 60 + h[2] % 180)


FONT_FILES = ["BarlowCondensed-BlackItalic.ttf", "BarlowCondensed-ExtraBold.ttf", "BarlowCondensed-ExtraBoldItalic.ttf",
              "BarlowCondensed-Bold.ttf", "BarlowCondensed-SemiBold.ttf", "BarlowCondensed-Medium.ttf"]
FONT_URL = "https://raw.githubusercontent.com/google/fonts/main/ofl/barlowcondensed/"


def ensure_fonts(progress=print) -> bool:
    """Download the Barlow Condensed fonts (SIL Open Font License) once, if they're missing."""
    import urllib.request

    FONT_DIR.mkdir(parents=True, exist_ok=True)
    ok = True
    for f in FONT_FILES + ["OFL.txt"]:
        p = FONT_DIR / f
        if p.exists() and p.stat().st_size > 1000:
            continue
        try:
            progress(f"  downloading font {f} (one time)...")
            urllib.request.urlretrieve(FONT_URL + f, p)
        except Exception as e:  # noqa: BLE001
            progress(f"  couldn't download {f} ({e}) - using a Windows font instead")
            ok = False
    return ok


@lru_cache(maxsize=64)
def font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    names = {"black": "BarlowCondensed-BlackItalic.ttf", "xbold": "BarlowCondensed-ExtraBold.ttf",
             "xboldi": "BarlowCondensed-ExtraBoldItalic.ttf", "bold": "BarlowCondensed-Bold.ttf",
             "semi": "BarlowCondensed-SemiBold.ttf", "med": "BarlowCondensed-Medium.ttf"}
    for p in (FONT_DIR / names[weight], Path("C:/Windows/Fonts/bahnschrift.ttf"),
              Path("C:/Windows/Fonts/arialbd.ttf"), Path("/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf")):
        if p.exists():
            return ImageFont.truetype(str(p), size)
    return ImageFont.load_default()


class G:
    """Graphics factory bound to an output size."""

    def __init__(self, width: int, height: int):
        self.W, self.H = width, height
        self.s = height / 1080.0

    def px(self, v) -> int:
        return int(round(v * self.s))

    def f(self, weight, size):
        return font(weight, max(8, self.px(size)))

    @staticmethod
    def tw(d: ImageDraw.ImageDraw, text: str, fnt) -> int:
        return int(d.textlength(text, font=fnt))

    def text_v(self, d, xy, text, fnt, fill, box_h):
        """Draw text vertically centred in a box of height box_h starting at xy[1]."""
        a, b = fnt.getbbox("HAg")[1], fnt.getbbox("HAg")[3]
        y = xy[1] + (box_h - (b - a)) / 2 - a
        d.text((xy[0], y), text, font=fnt, fill=fill)

    # ------------------------------------------------------------- tower
    def tower(self, header: str, rows: list[dict], caution: bool = False) -> Image.Image:
        """rows: {pos, name, color, gap, focus, hot, gap_row(bool separator before)}"""
        p = self.px
        w, rh, hh = p(330), p(38), p(46)
        n = len(rows) + sum(1 for r in rows if r.get("sep"))
        img = Image.new("RGBA", (w + p(8), hh + p(6) + n * rh + p(8)), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        # header
        d.rectangle([0, 0, w, hh], fill=YELLOW + (255,) if caution else ACCENT + (255,))
        self.text_v(d, (p(14), 0), header, self.f("xboldi", 30), (20, 20, 20) if caution else WHITE, hh)
        y = hh + p(6)
        for r in rows:
            if r.get("sep"):
                d.rectangle([0, y + rh // 2 - 1, w, y + rh // 2 + 1], fill=(60, 66, 78, 200))
                y += rh
            bg = ROW_HI if r.get("hot") else PANEL
            d.rectangle([0, y, w, y + rh - p(2)], fill=bg)
            # position box
            pos_fill = {1: GOLD, 2: SILVER, 3: BRONZE}.get(r["pos"])
            d.rectangle([0, y, p(44), y + rh - p(2)], fill=(pos_fill or (36, 41, 52)) + (255,))
            pf = self.f("xbold", 26)
            txt = str(r["pos"])
            self.text_v(d, ((p(44) - self.tw(d, txt, pf)) // 2, y), txt, pf, (15, 15, 15) if pos_fill else WHITE, rh - p(2))
            # make colour chip
            d.rectangle([p(50), y + p(7), p(56), y + rh - p(9)], fill=r["color"] + (255,))
            nf = self.f("bold" if not r.get("focus") else "xbold", 26)
            self.text_v(d, (p(66), y), r["name"].upper()[:14], nf, WHITE if (r.get("focus") or r.get("hot")) else (225, 230, 236), rh - p(2))
            gf = self.f("semi", 22)
            self.text_v(d, (w - p(10) - self.tw(d, r["gap"], gf), y), r["gap"], gf, GREY, rh - p(2))
            if r.get("focus"):
                d.rectangle([w - p(4), y, w, y + rh - p(2)], fill=ACCENT + (255,))
            y += rh
        return img

    # ------------------------------------------------------------- lower third
    def lower_third(self, tag: str, main: str, sub: str, chips: list = ()) -> Image.Image:
        p = self.px
        tag, main, sub = tag.upper(), main.upper(), sub.upper()
        tf, mf, sf = self.f("black", 34), self.f("xbold", 54), self.f("semi", 30)
        tmp = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        tag_w = self.tw(tmp, tag, tf) + p(40)
        main_w = max(self.tw(tmp, main, mf), self.tw(tmp, sub, sf)) + p(60) + p(14) * len(chips)
        h_tag, h_main = p(46), p(118)
        w = max(tag_w + p(20), main_w)
        img = Image.new("RGBA", (w + p(30), h_tag + h_main), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        # tag: slanted accent block
        sl = p(16)
        d.polygon([(0, 0), (tag_w + sl, 0), (tag_w, h_tag), (0, h_tag)], fill=ACCENT + (255,))
        self.text_v(d, (p(16), 0), tag, tf, WHITE, h_tag)
        # main panel
        d.polygon([(0, h_tag), (w + sl, h_tag), (w, h_tag + h_main), (0, h_tag + h_main)], fill=PANEL)
        x = p(24)
        for c in chips:
            d.rectangle([x, h_tag + p(18), x + p(8), h_tag + p(68)], fill=c + (255,))
            x += p(14)
        x += p(8) if chips else 0
        d.text((x, h_tag + p(10)), main, font=mf, fill=WHITE)
        d.text((x, h_tag + p(72)), sub, font=sf, fill=GREY)
        return img

    # ------------------------------------------------------------- title
    def title(self, track: str, layout: str, makes: list[str], info: str) -> Image.Image:
        p = self.px
        img = Image.new("RGBA", (self.W, self.H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        # dark gradient from the left so text reads over footage
        grad = Image.new("L", (self.W, 1))
        for x in range(self.W):
            grad.putpixel((x, 0), int(210 * max(0.0, 1 - x / (self.W * 0.75)) ** 1.4))
        shade = Image.new("RGBA", (self.W, self.H), BG + (0,))
        shade.putalpha(grad.resize((self.W, self.H)))
        img.alpha_composite(shade)
        x0, y0 = p(120), p(330)
        tag = "RACE HIGHLIGHTS"
        tf = self.f("black", 40)
        tw = self.tw(d, tag, tf) + p(44)
        d.polygon([(x0, y0), (x0 + tw + p(18), y0), (x0 + tw, y0 + p(56)), (x0, y0 + p(56))], fill=ACCENT + (255,))
        self.text_v(d, (x0 + p(20), y0), tag, tf, WHITE, p(56))
        size = 150
        while size > 60 and self.tw(d, track.upper(), self.f("black", size)) > self.W - x0 - p(80):
            size -= 6
        big = self.f("black", size)
        d.text((x0 - p(6), y0 + p(64) + p(150 - size) // 2), track.upper(), font=big, fill=WHITE)
        y = y0 + p(64) + p(170)
        if layout:
            d.text((x0, y), layout.upper(), font=self.f("semi", 44), fill=GREY)
            y += p(64)
        d.rectangle([x0, y + p(6), x0 + p(520), y + p(10)], fill=ACCENT + (255,))
        y += p(30)
        mx = x0
        mf = self.f("bold", 34)
        for m in makes:
            d.rectangle([mx, y + p(8), mx + p(10), y + p(40)], fill=make_color(m) + (255,))
            d.text((mx + p(18), y), m.upper(), font=mf, fill=WHITE)
            mx += p(18) + self.tw(d, m.upper(), mf) + p(34)
        y += p(58)
        d.text((x0, y), info.upper(), font=self.f("semi", 32), fill=GREY)
        return img

    # ------------------------------------------------------------- results
    def results(self, track: str, rows: list[dict], fastest: str | None) -> Image.Image:
        """rows: {pos, name, car, color, best, status, focus}"""
        p = self.px
        img = Image.new("RGBA", (self.W, self.H), BG + (150,))
        d = ImageDraw.Draw(img)
        x0, y0, w = p(260), p(110), p(1400)
        hf = self.f("black", 64)
        d.polygon([(x0, y0), (x0 + p(420), y0), (x0 + p(400), y0 + p(84)), (x0, y0 + p(84))], fill=ACCENT + (255,))
        self.text_v(d, (x0 + p(26), y0), "RACE RESULT", hf, WHITE, p(84))
        self.text_v(d, (x0 + p(440), y0), track.upper(), self.f("bold", 40), GREY, p(84))
        y = y0 + p(110)
        cols = dict(driver=p(120), car=p(640), best=p(1080), status=p(1250))
        lf = self.f("semi", 24)
        for k, label in (("driver", "DRIVER"), ("car", "CAR"), ("best", "BEST LAP"), ("status", "")):
            d.text((x0 + cols[k], y), label, font=lf, fill=DIM)
        y += p(38)
        for r in rows:
            podium = r["pos"] <= 3
            rh = p(66) if podium else p(50)
            d.rectangle([x0, y, x0 + w, y + rh - p(4)], fill=ROW_HI if r.get("focus") else PANEL)
            pos_fill = {1: GOLD, 2: SILVER, 3: BRONZE}.get(r["pos"])
            d.rectangle([x0, y, x0 + p(90), y + rh - p(4)], fill=(pos_fill or (36, 41, 52)) + (255,))
            pf = self.f("black", 44 if podium else 32)
            t = str(r["pos"])
            self.text_v(d, (x0 + (p(90) - self.tw(d, t, pf)) // 2, y), t, pf, (15, 15, 15) if pos_fill else WHITE, rh - p(4))
            d.rectangle([x0 + p(100), y + p(8), x0 + p(108), y + rh - p(12)], fill=r["color"] + (255,))
            nf = self.f("xbold", 40 if podium else 32)
            self.text_v(d, (x0 + cols["driver"], y), r["name"].upper(), nf, WHITE, rh - p(4))
            cf = self.f("semi", 30 if podium else 26)
            self.text_v(d, (x0 + cols["car"], y), r["car"], cf, (205, 212, 220), rh - p(4))
            self.text_v(d, (x0 + cols["best"], y), r["best"], cf, (205, 212, 220), rh - p(4))
            self.text_v(d, (x0 + cols["status"], y), r["status"], cf, GREY, rh - p(4))
            if r.get("focus"):
                d.rectangle([x0 + w - p(8), y, x0 + w, y + rh - p(4)], fill=ACCENT + (255,))
            y += rh
        if fastest:
            y += p(20)
            ff = self.f("bold", 32)
            lab = "FASTEST LAP"
            d.rectangle([x0, y, x0 + self.tw(d, lab, ff) + p(36), y + p(52)], fill=(130, 40, 200, 255))
            self.text_v(d, (x0 + p(18), y), lab, ff, WHITE, p(52))
            self.text_v(d, (x0 + self.tw(d, lab, ff) + p(56), y), fastest.upper(), ff, WHITE, p(52))
        return img

    # ------------------------------------------------------------- transition
    def stinger(self) -> tuple[Image.Image, int]:
        """A slanted band wider than the frame; returns (image, slant)."""
        W, H = self.W, self.H
        S = int(W * 0.25)
        img = Image.new("RGBA", (W + 2 * S, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.polygon([(S, 0), (W + 2 * S, 0), (W + S, H), (0, H)], fill=BG + (255,))
        band = int(S * 0.35)
        d.polygon([(S, 0), (S + band, 0), (band, H), (0, H)], fill=ACCENT + (255,))
        d.polygon([(W + 2 * S - band, 0), (W + 2 * S, 0), (W + S, H), (W + S - band, H)], fill=ACCENT + (255,))
        return img, S


def blurred(img: Image.Image, r: int) -> Image.Image:
    return img.filter(ImageFilter.GaussianBlur(r))
