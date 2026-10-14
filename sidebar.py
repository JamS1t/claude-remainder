"""Claude usage dock: liquid-glass tab on the right screen edge, always visible.

Hover a ring for details, click the chevron for the full panel, drag to move, right-click to refresh/quit.

Sources:
  - https://api.anthropic.com/api/oauth/usage (session/weekly limits, your own OAuth token)
  - ~/.claude/projects/**/*.jsonl transcripts (tokens per project, tools, context size)
  - `rtk gain` (RTK savings)
Run: pythonw sidebar.py   (self-check: python sidebar.py --check)
"""
import ctypes
import ctypes.wintypes as wt
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import (Image, ImageChops, ImageColor, ImageDraw, ImageEnhance, ImageFilter, ImageGrab,
                 ImageOps, ImageStat, ImageTk)

HOME = Path.home() / ".claude"
PROJECTS = HOME / "projects"
CACHE = Path(__file__).with_name(".scan-cache.json")
USAGE_EVERY, SCAN_EVERY = 120, 60  # seconds
# debug: windows stay screenshot-able and the glass backdrop freezes (it would capture itself)
DEBUG = bool(os.environ.get("CLAUDE_DOCK_DEBUG"))

# ponytail: rough cost weights in "input-token equivalents", mirrors API price ratios
# (cache read 0.1x, 5m write 1.25x, 1h write 2x, output 5x). Plan limits are not
# published per token, so these only rank things against each other.
W = {"in": 1, "cw5": 1.25, "cw1h": 2, "cr": 0.1, "out": 5}
MODEL_W = {"haiku": 0.2, "sonnet": 0.6}  # opus/fable/other = 1


def model_weight(model):
    return next((v for k, v in MODEL_W.items() if k in (model or "")), 1)


def weigh(t, model=""):
    return sum(t.get(k, 0) * w for k, w in W.items()) * model_weight(model)


# ---------- transcript scan ----------

def parse_file(path):
    """Aggregate one transcript. Assistant messages are split per content block, dedupe by id."""
    seen, hours, tools, models = set(), defaultdict(lambda: [0] * 6), Counter(), Counter()
    cwd, peak_ctx, first_ctx, turns = None, 0, None, 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"assistant"' not in line:
                if cwd is None and '"cwd"' in line:
                    m = re.search(r'"cwd":"((?:[^"\\]|\\.)*)"', line)
                    cwd = m and json.loads('"%s"' % m.group(1))
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            msg = d.get("message") or {}
            cwd = cwd or d.get("cwd")
            for c in msg.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    tools[c.get("name", "?")] += 1
            u, mid = msg.get("usage"), msg.get("id")
            if not u or mid in seen or msg.get("model") == "<synthetic>":
                continue
            seen.add(mid)
            cc = u.get("cache_creation") or {}
            cw1h = cc.get("ephemeral_1h_input_tokens", 0)
            cw = u.get("cache_creation_input_tokens", 0)
            t = {"in": u.get("input_tokens", 0), "cw5": cw - cw1h, "cw1h": cw1h,
                 "cr": u.get("cache_read_input_tokens", 0), "out": u.get("output_tokens", 0)}
            ctx = t["in"] + cw + t["cr"]
            peak_ctx = max(peak_ctx, ctx)
            if first_ctx is None:
                first_ctx = ctx
            turns += 1
            model = msg.get("model", "?")
            models[model] += weigh(t, model)
            h = hours[(d.get("timestamp") or "")[:13]]
            for i, k in enumerate(("in", "cw5", "cw1h", "cr", "out")):
                h[i] += t[k]
            h[5] += weigh(t, model)
    return {"cwd": cwd, "hours": dict(hours), "tools": dict(tools), "models": dict(models),
            "peak_ctx": peak_ctx, "first_ctx": first_ctx, "turns": turns}


def scan(cache):
    """Re-parse only transcripts whose size/mtime changed. Returns updated cache."""
    out = {}
    for p in PROJECTS.glob("**/*.jsonl"):
        st = p.stat()
        key = str(p)
        old = cache.get(key)
        if old and old["sig"] == [st.st_size, st.st_mtime]:
            out[key] = old
            continue
        try:
            agg = parse_file(p)
        except OSError:
            continue
        rel = p.relative_to(PROJECTS).parts
        agg.update(sig=[st.st_size, st.st_mtime], projdir=rel[0], mtime=st.st_mtime,
                   subagent="subagents" in rel, session=rel[1].split(".")[0])
        out[key] = agg
    return out


def project_name(agg, cwd_by_dir):
    cwd = agg.get("cwd") or cwd_by_dir.get(agg["projdir"]) or agg["projdir"]
    return Path(cwd).name or cwd


def summarize(cache, now=None):
    now = now or datetime.now(timezone.utc)
    week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H")
    today = now.astimezone().strftime("%Y-%m-%d")
    cwd_by_dir = {a["projdir"]: a["cwd"] for a in cache.values() if a.get("cwd") and not a["subagent"]}
    proj, tools, models = Counter(), Counter(), Counter()
    tot = [0] * 6
    sub_w = today_w = 0
    sessions = {}
    for a in cache.values():
        name = project_name(a, cwd_by_dir)
        for hr, h in a["hours"].items():
            if hr < week_ago:
                continue
            proj[name] += h[5]
            tot = [x + y for x, y in zip(tot, h)]
            sub_w += h[5] if a["subagent"] else 0
            # ponytail: hour keys are UTC, "today" is local; off by tz offset at day edges
            today_w += h[5] if hr[:10] == today else 0
        if a["hours"] and max(a["hours"]) >= week_ago:
            tools.update(a["tools"])
            models.update(a["models"])
            if not a["subagent"]:
                sessions[a["session"]] = (name, a)
    live = sorted(((n, a) for n, a in sessions.values() if time.time() - a["mtime"] < 900),
                  key=lambda x: -x[1]["mtime"])
    firsts = sorted(a["first_ctx"] for _, a in sessions.values() if a["first_ctx"])
    return {"proj": proj.most_common(), "tools": tools.most_common(), "models": models,
            "tot": tot, "sub_w": sub_w, "today_w": today_w, "live": live,
            "sessions": list(sessions.values()),
            "base_ctx": firsts[len(firsts) // 2] if firsts else 0}


# ---------- limits + rtk ----------

def fetch_limits():
    tok = json.loads((HOME / ".credentials.json").read_text())["claudeAiOauth"]["accessToken"]
    req = urllib.request.Request("https://api.anthropic.com/api/oauth/usage", headers={
        "Authorization": "Bearer " + tok, "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "claude-sidebar"})
    return json.loads(urllib.request.urlopen(req, timeout=15).read())


def fetch_rtk():
    try:
        txt = subprocess.run(["rtk", "gain"], capture_output=True, text=True, timeout=30,
                             encoding="utf-8", errors="replace",
                             creationflags=0x08000000).stdout  # CREATE_NO_WINDOW
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"Tokens saved:\s*([\d.]+\w?) \(([\d.]+)%\)", txt)
    return m and (m.group(1), float(m.group(2)))


# ---------- advice ----------

def fmt(n):
    for d, s in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if n >= d:
            return f"{n / d:.1f}{s}"
    return str(int(n))


def until(iso):
    if not iso:
        return ""
    s = (datetime.fromisoformat(iso) - datetime.now(timezone.utc)).total_seconds()
    if s <= 0:
        return "now"
    d, h, m = int(s // 86400), int(s % 86400 // 3600), int(s % 3600 // 60)
    return f"{d}d {h}h" if d else f"{h}h {m}m"


def find_limit(limits, kind):
    return next((l for l in (limits or {}).get("limits", []) if l["kind"] == kind), None)


def weekly_pace(weekly):
    """(pace, seconds left): % of weekly budget used vs share of the week elapsed (100 = even burn)."""
    if not weekly or not weekly.get("resets_at"):
        return None
    left = (datetime.fromisoformat(weekly["resets_at"]) - datetime.now(timezone.utc)).total_seconds()
    return weekly["percent"] / max(0.01, 1 - left / (7 * 86400)), left


def advice(s, limits, rtk):
    """Returns (tips, plugin stats, token mix). Each tip is (severity, title, body)."""
    tips = []
    weekly, session = find_limit(limits, "weekly_all"), find_limit(limits, "session")
    wp = weekly_pace(weekly)
    if wp:
        pace, left = wp
        pct = weekly["percent"]
        if pace > 105:
            days_to_cap = (100 - pct) / (pace / 7)  # pace/7 = % burned per day at this rate
            tips.append(("warn", "Ahead of weekly pace",
                         f"At this rate the cap hits in ~{days_to_cap:.1f}d, reset is in {until(weekly['resets_at'])}. "
                         "Push heavy runs (agents, renders) past the reset."))
        elif pct < 80 and left < 2 * 86400:
            tips.append(("good", "Unused weekly budget",
                         f"{100 - pct}% left, resets in {until(weekly['resets_at'])}. "
                         "Spend it now: big refactors, reviews, research."))
        else:
            tips.append(("ok", "Weekly pace on track", f"{pace:.0f}% of an even burn rate."))
    if session and session["percent"] >= 80:
        tips.append(("high", "Session window almost full",
                     f"{session['percent']}% used. Wrap up or switch to Sonnet; resets in {until(session['resets_at'])}."))

    tot = s["tot"]
    wsum = tot[5] or 1
    part = {k: tot[i] * W[k] for i, k in enumerate(("in", "cw5", "cw1h", "cr", "out"))}
    psum = sum(part.values()) or 1
    cr_share, out_share = part["cr"] / psum, part["out"] / psum
    cw_share = (part["cw5"] + part["cw1h"]) / psum
    if cr_share + cw_share > 0.6:
        tips.append(("warn", "Context is the cost",
                     f"Re-reading and caching context = {cr_share + cw_share:.0%} of weighted usage. "
                     "/clear between unrelated tasks, /compact at milestones."))
    fat = sorted((a["peak_ctx"], n) for n, a in s["sessions"] if a["peak_ctx"] > 300_000)
    if fat:
        tips.append(("warn", "Oversized sessions",
                     f"{len(fat)} grew past 300k this week (biggest: {fat[-1][1]}, {fmt(fat[-1][0])}). "
                     "Every turn re-reads all of it."))
    if s["base_ctx"] > 40_000:
        tips.append(("warn", "Heavy starting context",
                     f"A fresh session holds ~{fmt(s['base_ctx'])} before you type: system prompt, "
                     "CLAUDE.md, skills, MCP tools, hooks. Turn off plugins and MCP servers a project doesn't use."))
    if s["sub_w"] / wsum > 0.25:
        tips.append(("ok", "Subagents are a big share",
                     f"{s['sub_w'] / wsum:.0%} of usage. Fine for parallel work; "
                     "for single lookups, Grep/Read directly is cheaper."))
    opus = sum(v for m, v in s["models"].items() if "opus" in m or "fable" in m)
    if opus / (sum(s["models"].values()) or 1) > 0.9:
        tips.append(("ok", "All on Opus/Fable", "/model sonnet for mechanical edits, renames, docs."))
    tools = dict(s["tools"])
    if tools.get("Read", 0) > 3 * max(1, tools.get("Grep", 0)):
        tips.append(("ok", "Read before Grep", "Grep first, then Read with offset/limit."))

    # caveman/ponytail only shrink output tokens; that share is their ceiling
    plug = {"out": out_share, "save": out_share / 2, "rtk": rtk,
            "verdict": "Keep them, but context size matters more." if out_share < 0.3
            else "Output is a big slice here: they pay off."}
    mix = {"cache read": cr_share, "cache write": cw_share, "output": out_share, "input": part["in"] / psum}
    return tips, plug, mix


# ---------- liquid glass rendering ----------
# Each window is excluded from screen capture, so grabbing the screen under it returns
# what is behind it. That backdrop is blurred, saturated, tinted, then lit with a rim
# highlight and top sheen. Pixels outside the shape become the transparent color key.

KEY = (1, 2, 3)
FG, DIM, FAINT = "#ffffff", "#cfccd6", "#9894a0"
ACC = "#ff9a76"
SEV = {"good": "#8ee0a0", "ok": "#a8c8ff", "warn": "#ffcc6b", "high": "#ff7d72"}
MIX = {"cache read": ACC, "cache write": SEV["warn"], "output": SEV["ok"], "input": SEV["good"]}
FONT, DISPLAY = "Segoe UI Variable Text", "Segoe UI Variable Display"
SS = 3  # supersampling for antialiased shapes
WHITE = (255, 255, 255)


def rgba(color, a=255):
    return (*ImageColor.getrgb(color)[:3], a) if isinstance(color, str) else (*color[:3], a)


def level(pct):
    return SEV["high"] if pct >= 90 else SEV["warn"] if pct >= 70 else ACC


def pill_mask(w, h, r):
    m = Image.new("L", (w * SS, h * SS), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, w * SS - 1, h * SS - 1), r * SS, fill=255)
    return m


def dock_mask(w, h, r, sh):
    """Tab hugging the right screen edge: rounded on the left, concave flares top and bottom."""
    W, H, R, SH = w * SS, (h + 2 * sh) * SS, r * SS, sh * SS
    m = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle((0, SH, W + R, H - SH), R, fill=255)
    d.rectangle((W - SH, 0, W, SH), fill=255)
    d.ellipse((W - 2 * SH, -SH, W, SH), fill=0)
    d.rectangle((W - SH, H - SH, W, H), fill=255)
    d.ellipse((W - 2 * SH, H - SH, W, H + SH), fill=0)
    return m


def glass_layers(mask_ss, scale):
    """Precompute per-shape layers: mask, key mask, rim light, top sheen (all final size)."""
    w, h = mask_ss.width // SS, mask_ss.height // SS
    down = lambda im: im.resize((w, h), Image.BOX)
    inner_ss = mask_ss.filter(ImageFilter.MinFilter(2 * max(1, int(1.2 * scale * SS)) + 1))
    mask, inner = down(mask_ss), down(inner_ss)
    rim = down(ImageChops.subtract(mask_ss, inner_ss))
    grad = ImageOps.invert(Image.linear_gradient("L")).resize((w, h))  # 255 top -> 0 bottom
    rim_a = ImageChops.multiply(rim, grad.point(lambda v: int(70 + v * 0.65)))
    sheen_a = ImageChops.multiply(inner, grad.point(lambda v: max(0, int((v - 140) * 0.32))))
    white = Image.new("RGBA", (w, h), (*WHITE, 0))
    rim_l, sheen_l = white.copy(), white.copy()
    rim_l.putalpha(rim_a)
    sheen_l.putalpha(sheen_a)
    return {"mask": mask, "key": mask.point(lambda v: 255 if v < 10 else 0), "rim": rim_l, "sheen": sheen_l}


def glass_image(big, off, layers, overlay, blur):
    """big: screen grab around the window, off: window origin inside it."""
    mask = layers["mask"]
    w, h = mask.size
    box = (off[0], off[1], off[0] + w, off[1] + h)
    raw = big.crop(box)
    bl = ImageEnhance.Color(big.filter(ImageFilter.GaussianBlur(blur)).crop(box)).enhance(1.45)
    lum = ImageStat.Stat(bl.convert("L")).mean[0] / 255
    g = Image.blend(bl, Image.new("RGB", (w, h), (16, 16, 22)), 0.34 + 0.36 * lum).convert("RGBA")
    for layer in (layers["sheen"], layers["rim"], overlay):
        if layer is not None:
            g = Image.alpha_composite(g, layer)
    out = Image.composite(g.convert("RGB"), raw, mask)
    out.paste(KEY, (0, 0, w, h), layers["key"])
    return out


class Ov:
    """RGBA overlay drawn supersampled: cards, rings, bars. Coordinates in final pixels."""

    def __init__(self, w, h):
        self.w, self.h = w, h
        self.im = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
        self.d = ImageDraw.Draw(self.im)

    def card(self, x, y, w, h, r, fill=(*WHITE, 20), outline=(*WHITE, 34)):
        self.d.rounded_rectangle([v * SS for v in (x, y, x + w, y + h)], r * SS, fill=fill,
                                 outline=outline, width=SS if outline else 0)

    def ring(self, cx, cy, r, width, pct, color):
        box = [v * SS for v in (cx - r, cy - r, cx + r, cy + r)]
        w = int(width * SS)
        self.d.ellipse(box, outline=(*WHITE, 40), width=w)
        if pct > 0:
            end = -90 + 3.6 * min(pct, 100)
            self.d.arc(box, -90, end, fill=rgba(color), width=w)
            rr = (r - width / 2) * SS
            for a in (-90, end):  # round caps
                x, y = cx * SS + rr * math.cos(math.radians(a)), cy * SS + rr * math.sin(math.radians(a))
                self.d.ellipse((x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill=rgba(color))

    def bar(self, x, y, w, h, frac, color, track=(*WHITE, 34)):
        self.card(x, y, w, h, h / 2, fill=track, outline=None)
        if frac > 0:
            self.card(x, y, max(h, w * min(frac, 1)), h, h / 2, fill=rgba(color), outline=None)

    def dot(self, cx, cy, r, color):
        self.d.ellipse([v * SS for v in (cx - r, cy - r, cx + r, cy + r)], fill=rgba(color))

    def chevron(self, cx, cy, s, left, width):
        dx = -s if left else s
        pts = [((cx - dx / 2) * SS, (cy - s) * SS), ((cx + dx / 2) * SS, cy * SS), ((cx - dx / 2) * SS, (cy + s) * SS)]
        self.d.line(pts, fill=(*WHITE, 235), width=int(width * SS), joint="curve")

    def done(self):
        return self.im.resize((self.w, self.h), Image.LANCZOS)


def work_area():
    r = wt.RECT()
    ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(r), 0)  # SPI_GETWORKAREA
    return r.left, r.top, r.right, r.bottom


def plan_name():
    try:
        tier = json.loads((HOME / ".credentials.json").read_text())["claudeAiOauth"].get("rateLimitTier", "")
    except (OSError, ValueError, KeyError):
        return ""
    m = re.search(r"(max|pro|team|enterprise)(?:_(\d+x))?", tier)
    return " ".join(p for p in (m.group(1).title(), m.group(2)) if p) if m else ""


class Glass:
    """One shaped, borderless, always-on-top glass window. Text sits on a canvas above the glass."""

    def __init__(self, app, win=None):
        self.app = app
        self.win = win or tk.Toplevel(app.root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-transparentcolor", "#%02x%02x%02x" % KEY)
        self.win.configure(bg="#%02x%02x%02x" % KEY)
        self.win.update_idletasks()
        if not DEBUG:
            ctypes.windll.user32.SetWindowDisplayAffinity(int(self.win.wm_frame(), 16), 0x11)  # exclude from capture
        self.cv = tk.Canvas(self.win, bg="#%02x%02x%02x" % KEY, highlightthickness=0, bd=0)
        self.cv.pack(fill="both", expand=True)
        self.geo = self.layers = self.overlay = self.thumb = self.photo = None
        self.visible = False

    def shape(self, x, y, mask_ss):
        w, h = mask_ss.width // SS, mask_ss.height // SS
        if not self.layers or self.layers["mask"].size != (w, h):
            self.layers = glass_layers(mask_ss, self.app.S)
        self.move(x, y, w, h)

    def move(self, x, y, w=None, h=None):
        w, h = w or self.geo[2], h or self.geo[3]
        self.geo = (x, y, w, h)
        self.cv.configure(width=w, height=h)
        self.win.geometry(f"{w}x{h}+{x}+{y}")
        self.thumb = None

    def content(self, overlay, texts):
        """texts: (x, y, text, font, color, anchor)."""
        self.overlay = overlay
        self.cv.delete("txt")
        for x, y, text, font, color, anchor in texts:
            self.cv.create_text(x, y, text=text, font=font, fill=color, anchor=anchor, tags="txt")
        self.compose(force=True)

    def compose(self, force=False):
        if not self.visible or not self.geo:
            return
        x, y, w, h = self.geo
        m = self.app.px(40)
        sw, sh = self.app.screen
        bx0, by0 = max(0, x - m), max(0, y - m)
        frozen = getattr(self, "frozen", None)
        if DEBUG and frozen and frozen[0] == self.geo:
            big = frozen[1]
        else:
            big = ImageGrab.grab((bx0, by0, min(sw, x + w + m), min(sh, y + h + m)))
            self.frozen = (self.geo, big)
        thumb = big.resize((16, 16)).tobytes()
        if thumb == self.thumb and not force:
            return
        self.thumb = thumb
        img = glass_image(big, (x - bx0, y - by0), self.layers, self.overlay, self.app.px(16))
        self.photo = ImageTk.PhotoImage(img)
        if self.cv.find_withtag("bg"):
            self.cv.itemconfig("bg", image=self.photo)
        else:
            self.cv.create_image(0, 0, image=self.photo, anchor="nw", tags="bg")
        self.cv.tag_lower("bg")

    def show(self):
        if self.visible:
            return
        self.visible = True
        self.compose(force=True)
        self.win.attributes("-alpha", 0)
        self.win.deiconify()
        self.app.fade(self.win)

    def hide(self):
        self.visible = False
        self.win.withdraw()


# ---------- app ----------

TABS = ("Overview", "Projects", "Tips", "Plugins")


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.S = self.root.winfo_fpixels("1i") / 96
        self.screen = (self.root.winfo_screenwidth(), self.root.winfo_screenheight())
        self.cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
        self.limits = self.rtk = self.summary = None
        self.err, self.plan = "", plan_name()
        self.tab, self.fly_kind, self.fly_hide, self.hover = 0, None, None, None
        self.panel_open = False
        F = lambda fam, size, bold=False: tkfont.Font(family=fam, size=size, weight="bold" if bold else "normal")
        self.f = {"title": F(DISPLAY, 15, True), "big": F(DISPLAY, 17, True), "num": F(DISPLAY, 14, True),
                  "ring": F(DISPLAY, 9, True), "h": F(FONT, 10, True), "b": F(FONT, 9),
                  "bb": F(FONT, 9, True), "s": F(FONT, 8), "xs": F(FONT, 7), "cap": F(FONT, 7, True)}

        p = self.px
        self.DW, self.DH, self.SH = p(58), p(196), p(16)
        self.FW, self.FH = p(256), p(62)
        self.PW, self.PH = p(360), p(600)
        self.wl, self.wtop, self.wr, self.wb = work_area()
        self.dock_x = self.wr - self.DW
        self.dock_y = self.wtop + (self.wb - self.wtop - self.DH - 2 * self.SH) // 2

        self.dock = Glass(self, self.root)
        self.fly = Glass(self)
        self.panel = Glass(self)
        self.fly.hide()
        self.panel.hide()
        self.dock.shape(self.dock_x, self.dock_y, dock_mask(self.DW, self.DH, p(22), self.SH))
        self.dock.visible = True
        self.panel_hits = []

        dc = self.dock.cv
        dc.bind("<Motion>", self.dock_motion)
        dc.bind("<Leave>", self.dock_leave)
        dc.bind("<ButtonPress-1>", self.press)
        dc.bind("<B1-Motion>", self.drag)
        dc.bind("<ButtonRelease-1>", self.release)
        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="Refresh now", command=self.refresh_all)
        self.menu.add_command(label="Quit", command=self.root.destroy)
        dc.bind("<Button-3>", lambda e: self.menu.tk_popup(e.x_root, e.y_root))
        pc = self.panel.cv
        pc.bind("<Button-1>", self.panel_click)
        pc.bind("<Motion>", lambda e: pc.configure(cursor="hand2" if self.hit(e) else ""))
        self.panel.win.bind("<Escape>", lambda e: self.toggle_panel(False))
        self.panel.win.bind("<Left>", lambda e: self.set_tab((self.tab - 1) % len(TABS)))
        self.panel.win.bind("<Right>", lambda e: self.set_tab((self.tab + 1) % len(TABS)))

        self.draw_dock()
        self.loop_limits()
        self.loop_scan()
        self.tick()
        self.glass_loop()

    def px(self, v):
        return int(round(v * self.S))

    def fade(self, win, step=0, steps=8):
        win.attributes("-alpha", (step + 1) / steps)
        if step + 1 < steps:
            self.root.after(14, self.fade, win, step + 1, steps)

    def glass_loop(self):  # keep the frosted backdrop live; recompose only when the screen behind changes
        for g in (self.dock, self.fly, self.panel):
            if not DEBUG:
                g.compose()
        self.root.after(700, self.glass_loop)

    # ----- data -----
    def bg(self, fn, done):
        def run():
            try:
                r = fn()
                self.root.after(0, done, r)
            except Exception as e:  # network/parse failure: show it, keep the last good data
                self.root.after(0, self.set_err, f"{fn.__name__}: {e}")
        threading.Thread(target=run, daemon=True).start()

    def set_err(self, e):
        self.err = e
        self.redraw()

    def loop_limits(self, once=False):
        def got(r):
            self.limits, self.err = r, ""
            self.redraw()
        self.bg(fetch_limits, got)
        self.bg(fetch_rtk, lambda r: (setattr(self, "rtk", r), self.redraw()))
        if not once:
            self.root.after(USAGE_EVERY * 1000, self.loop_limits)

    def loop_scan(self, once=False):
        def work():
            c = scan(self.cache)
            CACHE.write_text(json.dumps(c))
            return c
        def got(c):
            self.cache, self.summary = c, summarize(c)
            self.redraw()
        self.bg(work, got)
        if not once:
            self.root.after(SCAN_EVERY * 1000, self.loop_scan)

    def refresh_all(self):
        self.loop_limits(once=True)
        self.loop_scan(once=True)

    def tick(self):  # keep reset countdowns fresh without refetching
        self.redraw()
        self.root.after(30_000, self.tick)

    def redraw(self):
        self.draw_dock()
        if self.fly_kind:
            self.draw_fly(self.fly_kind)
        if self.panel_open:
            self.draw_panel()

    def wrap(self, text, font, width):
        lines = []
        for para in text.split("\n"):
            line = ""
            for word in para.split(" "):
                trial = f"{line} {word}".strip()
                if line and self.f[font].measure(trial) > width:
                    lines.append(line)
                    line = word
                else:
                    line = trial
            lines.append(line)
        return lines

    # ----- dock -----
    def dock_zones(self):
        p = self.px
        cx = self.DW // 2 + p(2)
        r1 = self.SH + p(14) + p(19)
        ex = self.SH + self.DH - p(14) - p(17)
        return cx, {"session": (r1, p(19)), "weekly": (r1 + p(68), p(19)), "expand": (ex, p(17))}

    def draw_dock(self):
        p = self.px
        cx, z = self.dock_zones()
        ov = Ov(self.DW, self.DH + 2 * self.SH)
        texts = []
        for kind, lim_kind, label in (("session", "session", "5h"), ("weekly", "weekly_all", "week")):
            lim = find_limit(self.limits, lim_kind)
            pct = lim["percent"] if lim else 0
            y, r = z[kind]
            ov.ring(cx, y, r, p(3.6), pct, level(pct))
            texts.append((cx, y, str(pct) if lim else "–", self.f["ring"], FG, "center"))
            texts.append((cx, y + r + p(11), label, self.f["xs"], DIM, "center"))
        y, r = z["expand"]
        hov = self.hover == "expand"
        ov.card(cx - r, y - r, 2 * r, 2 * r, p(11), fill=(*WHITE, 62 if hov else 30), outline=(*WHITE, 70))
        ov.chevron(cx, y, p(5), not self.panel_open, p(2))
        self.dock.content(ov.done(), texts)

    def dock_zone_at(self, e):
        cx, z = self.dock_zones()
        return next((k for k, (y, r) in z.items() if abs(e.x - cx) <= r + 4 and abs(e.y - y) <= r + 4), None)

    def dock_motion(self, e):
        if not getattr(self, "moved", False):
            self.dock_hover(self.dock_zone_at(e))

    def dock_leave(self, e):
        # mapping the flyout fires a spurious <Leave>; trust the pointer position instead
        x, y = self.root.winfo_pointerxy()
        gx, gy, gw, gh = self.dock.geo
        if not (gx <= x < gx + gw and gy <= y < gy + gh):
            self.dock_hover(None)

    def dock_hover(self, zone):
        if zone == self.hover:
            return
        self.hover = zone
        self.dock.cv.configure(cursor="hand2" if zone == "expand" else "")
        self.draw_dock()
        if zone in ("session", "weekly"):
            self.show_fly(zone)
        else:
            self.hide_fly_later()

    def press(self, e):
        self.drag_from, self.drag_y0, self.moved = e.y_root, self.dock_y, False
        self.press_zone = self.dock_zone_at(e)

    def drag(self, e):
        dy = e.y_root - self.drag_from
        if abs(dy) > 3:
            self.moved = True
            self.hide_fly()
            self.dock_y = min(max(self.drag_y0 + dy, self.wtop), self.wb - self.DH - 2 * self.SH)
            self.dock.move(self.dock_x, self.dock_y)
            self.dock.compose()
            if self.panel_open:
                self.place_panel()

    def release(self, e):
        if not self.moved and self.press_zone == "expand":
            self.toggle_panel()
        self.moved = False

    # ----- hover flyout -----
    def show_fly(self, kind):
        if self.fly_hide:
            self.root.after_cancel(self.fly_hide)
            self.fly_hide = None
        if self.panel_open or self.fly_kind == kind:
            return
        self.fly_kind = kind
        y = self.dock_y + self.dock_zones()[1][kind][0] - self.FH // 2
        x = self.dock_x - self.FW - self.px(8)
        self.fly.shape(x, y, pill_mask(self.FW, self.FH, self.FH // 2))
        self.fly.hide()
        self.draw_fly(kind)
        self.fly.show()

    def hide_fly_later(self):
        if self.fly_kind and not self.fly_hide:
            self.fly_hide = self.root.after(140, self.hide_fly)

    def hide_fly(self):
        self.fly_hide = self.fly_kind = None
        self.fly.hide()

    def draw_fly(self, kind):
        p = self.px
        lim = find_limit(self.limits, "session" if kind == "session" else "weekly_all")
        pct = lim["percent"] if lim else 0
        sub = f"resets in {until(lim['resets_at'])}" if lim else "loading…"
        wp = kind == "weekly" and weekly_pace(lim)
        if wp:
            sub += f" · pace {wp[0]:.0f}%"
        ov = Ov(self.FW, self.FH)
        x0, x1 = p(24), self.FW - p(24)
        ov.bar(x0, self.FH - p(15), x1 - x0, p(4), pct / 100, level(pct))
        self.fly.content(ov.done(), [
            (x0, p(12), "Session · 5 hours" if kind == "session" else "Weekly", self.f["h"], FG, "nw"),
            (x0, p(31), sub, self.f["s"], DIM, "nw"),
            (x1, p(25), f"{pct}%", self.f["big"], level(pct), "e")])

    # ----- panel -----
    def toggle_panel(self, state=None):
        self.panel_open = not self.panel_open if state is None else state
        self.hide_fly()
        if self.panel_open:
            self.place_panel()
            self.draw_panel()
            self.panel.show()
            self.panel.win.focus_force()
        else:
            self.panel.hide()
        self.draw_dock()

    def place_panel(self):
        mid = self.dock_y + self.SH + self.DH // 2
        y = min(max(mid - self.PH // 2, self.wtop + self.px(12)), self.wb - self.PH - self.px(12))
        self.panel.shape(self.dock_x - self.PW - self.px(10), y, pill_mask(self.PW, self.PH, self.px(26)))
        self.panel.compose(force=True)

    def set_tab(self, i):
        self.tab = i
        self.draw_panel()

    def hit(self, e):
        return next((a for x0, y0, x1, y1, a in self.panel_hits if x0 <= e.x <= x1 and y0 <= e.y <= y1), None)

    def panel_click(self, e):
        a = self.hit(e)
        if a == "close":
            self.toggle_panel(False)
        elif isinstance(a, int):
            self.set_tab(a)

    def draw_panel(self):
        p, f = self.px, self.f
        W, H, pad = self.PW, self.PH, p(20)
        cw = W - 2 * pad
        ov, T = Ov(W, H), []
        self.panel_hits = hits = []
        t = lambda x, y, s, font, col=FG, anchor="nw": T.append((x, y, s, f[font], col, anchor))

        t(pad, p(18), "Claude", "title", ACC)
        if self.plan:
            t(pad + f["title"].measure("Claude") + p(8), p(25), self.plan, "s", DIM)
        t(W - pad, p(20), "✕", "h", DIM, "ne")
        hits.append((W - pad - p(22), p(12), W - pad + p(6), p(40), "close"))
        t(pad, p(48), f"updated {datetime.now():%H:%M} · ← → switch tabs · right-click dock to refresh", "xs", FAINT)

        y, sh = p(72), p(32)
        ov.card(pad, y, cw, sh, sh / 2, fill=(*WHITE, 22), outline=(*WHITE, 30))
        sw = cw / len(TABS)
        for i, name in enumerate(TABS):
            sx = pad + i * sw
            if i == self.tab:
                ov.card(sx + p(3), y + p(3), sw - p(6), sh - p(6), (sh - p(6)) / 2,
                        fill=(*WHITE, 58), outline=(*WHITE, 80))
            t(sx + sw / 2, y + sh / 2, name, "bb" if i == self.tab else "s", FG if i == self.tab else DIM, "center")
            hits.append((sx, y, sx + sw, y + sh, i))
        y += sh + p(18)

        getattr(self, "tab_" + TABS[self.tab].lower())(ov, t, pad, y, cw, H - pad)
        self.panel.content(ov.done(), T)

    def section(self, t, pad, y, cw, title, right=""):
        t(pad, y, title.upper(), "cap", FAINT)
        if right:
            t(pad + cw, y, right, "xs", FAINT, "ne")
        return y + self.px(20)

    def para(self, t, x, y, text, font, width, col=DIM):
        lines = self.wrap(text, font, width)
        t(x, y, "\n".join(lines), font, col)
        return y + len(lines) * self.f[font].metrics("linespace")

    def tab_overview(self, ov, t, pad, y, cw, bottom):
        p, L, s = self.px, self.limits, self.summary
        tw, th = (cw - p(10)) / 2, p(170)
        for i, (kind, title) in enumerate((("session", "Session · 5h"), ("weekly_all", "Weekly"))):
            lim = find_limit(L, kind)
            pct = lim["percent"] if lim else 0
            x = pad + i * (tw + p(10))
            ov.card(x, y, tw, th, p(20))
            cx, cy = x + tw / 2, y + p(62)
            ov.ring(cx, cy, p(44), p(8), pct, level(pct))
            t(cx, cy, f"{pct}%" if lim else "–", "big", FG, "center")
            t(cx, y + p(122), title, "bb", FG, "n")
            t(cx, y + p(142), f"resets in {until(lim['resets_at'])}" if lim else "loading…", "s", DIM, "n")
        y += th + p(12)
        wp = weekly_pace(find_limit(L, "weekly_all"))
        rows = [r for r in ((L or {}).get("seven_day_breakdown") or {}).get("rows") or [] if r["percent"]]
        info = " · ".join(filter(None, [wp and f"pace {wp[0]:.0f}% of even burn",
                                        ", ".join(f"{r['display_name']} {r['percent']}%" for r in rows)]))
        if info:
            y = self.para(t, pad, y, info, "s", cw)
        if self.err:
            y = self.para(t, pad, y + p(4), self.err[:140], "s", cw, SEV["high"])
        y = self.section(t, pad, y + p(16), cw, "Live now", "context of 1M")
        if not s:
            t(pad, y, "Scanning transcripts…", "s", DIM)
            return
        if not s["live"]:
            t(pad, y, "No session active in the last 15 minutes", "s", DIM)
        for name, a in s["live"]:
            if y + p(34) > bottom:
                break
            ctx = a["peak_ctx"]
            col = SEV["high"] if ctx > 300_000 else SEV["warn"] if ctx > 150_000 else SEV["good"]
            ov.dot(pad + p(4), y + p(8), p(3.5), col)
            t(pad + p(14), y, name[:30], "b")
            t(pad + cw, y + p(1), f"{fmt(ctx)} · {a['turns']} turns", "s", DIM, "ne")
            ov.bar(pad, y + p(22), cw, p(4), ctx / 1_000_000, col)
            y += p(36)

    def tab_projects(self, ov, t, pad, y, cw, bottom):
        p, s = self.px, self.summary
        if not s:
            t(pad, y, "Scanning transcripts…", "s", DIM)
            return
        tips, plug, mix = advice(s, self.limits, self.rtk)
        y = self.section(t, pad, y, cw, "Top projects", "share of 7 days")
        top = s["proj"][:7]
        total = sum(v for _, v in s["proj"]) or 1
        for name, v in top:
            t(pad, y, name[:34], "b")
            t(pad + cw, y + p(1), f"{v / total:.0%}", "bb", FG, "ne")
            ov.bar(pad, y + p(22), cw, p(4), v / (top[0][1] or 1), ACC)
            y += p(36)
        y = self.section(t, pad, y + p(10), cw, "Where tokens go", "weighted")
        x, h, gap = pad, p(10), p(3)
        for k, v in mix.items():
            w = cw * v
            if w > gap + h:
                ov.card(x, y, w - gap, h, h / 2, fill=rgba(MIX[k]), outline=None)
            x += w
        y += h + p(12)
        for i, (k, v) in enumerate(mix.items()):
            lx, ly = pad + (i % 2) * cw / 2, y + (i // 2) * p(20)
            ov.dot(lx + p(4), ly + p(8), p(4), MIX[k])
            t(lx + p(14), ly, k, "s", DIM)
            t(lx + cw / 2 - p(10), ly, f"{v:.0%}", "bb", FG, "ne")
        y += p(48)
        tot = s["tot"]
        self.para(t, pad, y, f"Subagents {s['sub_w'] / (tot[5] or 1):.0%} of usage. Top tools: "
                  + ", ".join(f"{n} {c}" for n, c in s["tools"][:5]), "s", cw, FAINT)

    def tab_tips(self, ov, t, pad, y, cw, bottom):
        p, s = self.px, self.summary
        if not s:
            t(pad, y, "Scanning transcripts…", "s", DIM)
            return
        tips = advice(s, self.limits, self.rtk)[0]
        ls = self.f["s"].metrics("linespace")
        for i, (sev, title, body) in enumerate(tips):
            lines = self.wrap(body, "s", cw - p(42))
            h = p(36) + len(lines) * ls
            if y + h > bottom:
                t(pad, y, f"+{len(tips) - i} more", "s", FAINT)
                break
            ov.card(pad, y, cw, h, p(16))
            ov.dot(pad + p(17), y + p(20), p(4.5), SEV[sev])
            t(pad + p(32), y + p(11), title, "bb")
            t(pad + p(32), y + p(30), "\n".join(lines), "s", DIM)
            y += h + p(8)

    def tab_plugins(self, ov, t, pad, y, cw, bottom):
        p, s = self.px, self.summary
        if not s:
            t(pad, y, "Scanning transcripts…", "s", DIM)
            return
        plug = advice(s, self.limits, self.rtk)[1]
        rtk = plug["rtk"]
        stats = [(f"{plug['out']:.0%}", "of usage is\noutput"), (f"~{plug['save']:.0%}", "max saving\nat a 50% cut"),
                 (rtk[0] if rtk else "–", f"RTK saved\n{rtk[1]:.0f}% of output" if rtk else "RTK\nnot found")]
        tw, th = (cw - p(16)) / 3, p(90)
        for i, (val, cap) in enumerate(stats):
            x = pad + i * (tw + p(8))
            ov.card(x, y, tw, th, p(16))
            t(x + p(12), y + p(12), val, "num", FG)
            t(x + p(12), y + p(44), cap, "xs", DIM)
        y += th + p(16)
        y = self.para(t, pad, y, plug["verdict"], "bb", cw, FG) + p(14)
        for name, what, sev in (("Caveman", "Shorter prose from Claude. Cuts output tokens only.", "ok"),
                                ("Ponytail", "Less code written. Also cuts output tokens only.", "ok"),
                                ("RTK", "Filters shell output before Claude reads it. Cuts context, "
                                        "which is where most of your usage goes.", "good")):
            lines = self.wrap(what, "s", cw - p(42))
            h = p(36) + len(lines) * self.f["s"].metrics("linespace")
            ov.card(pad, y, cw, h, p(16))
            ov.dot(pad + p(17), y + p(20), p(4.5), SEV[sev])
            t(pad + p(32), y + p(11), name, "bb")
            t(pad + p(32), y + p(30), "\n".join(lines), "s", DIM)
            y += h + p(8)


def check():
    """Self-check: real transcripts through advice, plus the glass compositor on a synthetic backdrop."""
    c = scan({})
    assert c, "no transcripts found"
    s = summarize(c)
    assert s["tot"][5] > 0 and s["proj"], s
    lim = {"limits": [{"kind": "weekly_all", "percent": 50,
                       "resets_at": (datetime.now(timezone.utc) + timedelta(days=3.5)).isoformat()}]}
    pace, _ = weekly_pace(find_limit(lim, "weekly_all"))
    assert 99 < pace < 101, pace
    tips, plug, mix = advice(s, lim, ("1M", 50.0))
    assert abs(sum(mix.values()) - 1) < 1e-6 and plug["rtk"] and all(len(t) == 3 for t in tips)
    assert until(None) == "" and fmt(1500) == "1.5k" and level(95) == SEV["high"]

    layers = glass_layers(dock_mask(58, 196, 22, 16), 1)
    w, h = layers["mask"].size
    big = Image.new("RGB", (w + 40, h + 40), (200, 120, 40))
    img = glass_image(big, (20, 20), layers, Ov(w, h).done(), 8)
    assert img.size == (w, h)
    assert img.getpixel((0, 0)) == KEY, "outside the shape must be the transparent key"
    assert img.getpixel((w - 1, 8)) != KEY, "top flare must hug the screen edge"
    assert img.getpixel((w // 2, h // 2)) not in (KEY, (200, 120, 40)), "inside must be tinted glass"
    for name, v in s["proj"][:5]:
        print(f"{name:30} {fmt(v)}")
    for t in tips:
        print("-", t[1], "|", t[2])
    print("ok")


if __name__ == "__main__":
    if "--check" in sys.argv:
        check()
    else:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        App().root.mainloop()
