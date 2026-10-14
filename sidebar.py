"""Claude usage sidebar: docked to the left screen edge, always visible.

Sources:
  - https://api.anthropic.com/api/oauth/usage (session/weekly limits, your own OAuth token)
  - ~/.claude/projects/**/*.jsonl transcripts (tokens per project, tools, context size)
  - `rtk gain` (RTK savings)
Run: pythonw sidebar.py   (self-check: python sidebar.py --check)
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path.home() / ".claude"
PROJECTS = HOME / "projects"
CACHE = Path(__file__).with_name(".scan-cache.json")
WIDTH, COLLAPSED = 320, 26
USAGE_EVERY, SCAN_EVERY = 120, 60  # seconds

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


def advice(s, limits, rtk):
    tips = []
    weekly = next((l for l in (limits or {}).get("limits", []) if l["kind"] == "weekly_all"), None)
    session = next((l for l in (limits or {}).get("limits", []) if l["kind"] == "session"), None)
    if weekly and weekly.get("resets_at"):
        left = (datetime.fromisoformat(weekly["resets_at"]) - datetime.now(timezone.utc)).total_seconds()
        elapsed = max(0.01, 1 - left / (7 * 86400))
        pct = weekly["percent"]
        pace = pct / elapsed
        if pace > 105:
            tips.append(("warn", f"Weekly pace {pace:.0f}%: you'd hit the cap ~{(100 - pct) / (pct / elapsed / 7):.1f}d "
                                 "before reset. Push heavy runs (agents, renders) past the reset."))
        elif pct < 80 and left < 2 * 86400:
            tips.append(("good", f"{100 - pct}% of the week unused, resets in {until(weekly['resets_at'])}. "
                                 "Spend it now: queue big refactors, reviews, research."))
        else:
            tips.append(("ok", f"Weekly pace {pace:.0f}% of budget. On track."))
    if session and session["percent"] >= 80:
        tips.append(("warn", f"Session window {session['percent']}%. Wrap up or switch to Sonnet; resets in {until(session['resets_at'])}."))

    tot = s["tot"]
    wsum = tot[5] or 1
    part = {k: tot[i] * W[k] for i, k in enumerate(("in", "cw5", "cw1h", "cr", "out"))}
    psum = sum(part.values()) or 1
    cr_share, out_share = part["cr"] / psum, part["out"] / psum
    cw_share = (part["cw5"] + part["cw1h"]) / psum
    if cr_share + cw_share > 0.6:
        tips.append(("warn", f"Context re-reads + cache writes = {(cr_share + cw_share):.0%} of weighted usage. "
                             "Long sessions are the cost: /clear between unrelated tasks, /compact at milestones."))
    fat = sorted((a["peak_ctx"], n) for n, a in s["sessions"] if a["peak_ctx"] > 300_000)
    if fat:
        tips.append(("warn", f"{len(fat)} session(s) grew past 300k context this week "
                             f"(biggest: {fat[-1][1]}, {fmt(fat[-1][0])}). Every turn re-reads all of it."))
    if s["base_ctx"] > 40_000:
        tips.append(("warn", f"Fresh session starts at ~{fmt(s['base_ctx'])} tokens before you type "
                             "(system prompt, CLAUDE.md, skills list, MCP tools, hook output). Disable unused "
                             "plugins/MCP servers per project: biggest fixed cost in Claude Code here."))
    if s["sub_w"] / wsum > 0.25:
        tips.append(("ok", f"Subagents = {s['sub_w'] / wsum:.0%} of usage. Fine for parallel work; "
                           "for single lookups, Grep/Read directly is cheaper."))
    opus = sum(v for m, v in s["models"].items() if "opus" in m or "fable" in m)
    if opus / (sum(s["models"].values()) or 1) > 0.9:
        tips.append(("ok", "~All usage on Opus/Fable. /model sonnet for mechanical edits, renames, docs."))
    tools = dict(s["tools"])
    if tools.get("Read", 0) > 3 * max(1, tools.get("Grep", 0)):
        tips.append(("ok", "Read calls far outnumber Grep. Grep first, then Read with offset/limit."))

    # caveman/ponytail only shrink output tokens; that share is their ceiling
    verdict = (f"Output = {out_share:.0%} of weighted usage. Caveman trims prose and ponytail trims code, "
               f"both inside that slice: a 50% cut saves ~{out_share / 2:.0%} total. "
               + ("Worth keeping, but context size matters more." if out_share < 0.3 else
                  "Real win here: output is a big slice."))
    if rtk:
        verdict += f"  RTK: saved {rtk[0]} tokens ({rtk[1]:.0f}% of command output). Keep."
    return tips, verdict, {"cache read": cr_share, "cache write": cw_share, "output": out_share,
                           "fresh input": part["in"] / psum}


# ---------- Windows appbar: reserve the left strip so maximized windows don't cover it ----------

class APPBARDATA(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uCallbackMessage", wt.UINT),
                ("uEdge", wt.UINT), ("rc", wt.RECT), ("lParam", wt.LPARAM)]


ABM_NEW, ABM_REMOVE, ABM_QUERYPOS, ABM_SETPOS, ABE_LEFT = 0, 1, 2, 3, 0


class AppBar:
    def __init__(self, hwnd):
        self.abd = APPBARDATA(ctypes.sizeof(APPBARDATA), hwnd, 0x8000 + 77, ABE_LEFT)
        self.shell = ctypes.windll.shell32.SHAppBarMessage
        self.shell(ABM_NEW, ctypes.byref(self.abd))

    def dock(self, width):
        u = ctypes.windll.user32
        a = self.abd
        a.rc = wt.RECT(0, 0, width, u.GetSystemMetrics(1))
        self.shell(ABM_QUERYPOS, ctypes.byref(a))
        a.rc.right = a.rc.left + width
        self.shell(ABM_SETPOS, ctypes.byref(a))
        return a.rc.left, a.rc.top, a.rc.right - a.rc.left, a.rc.bottom - a.rc.top

    def remove(self):
        self.shell(ABM_REMOVE, ctypes.byref(self.abd))


# ---------- UI ----------

BG, CARD, FG, DIM, ACC = "#16161a", "#202027", "#e8e6e3", "#8a8894", "#d97757"
SEV = {"warn": "#e5a54b", "good": "#6cc183", "ok": "#7aa2d6", "high": "#e06c6c"}
FONT = "Segoe UI"


class Sidebar:
    def __init__(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=BG)
        self.collapsed = False
        self.cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
        self.limits = self.rtk = self.summary = None
        self.err = ""
        self.root.update_idletasks()
        self.bar = AppBar(int(self.root.wm_frame(), 16))
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

        top = tk.Frame(self.root, bg=BG)
        top.pack(fill="x")
        self.toggle_btn = tk.Label(top, text="◀", bg=BG, fg=DIM, font=(FONT, 11), cursor="hand2")
        self.toggle_btn.pack(side="right", padx=6)
        self.toggle_btn.bind("<Button-1>", lambda e: self.toggle())
        close = tk.Label(top, text="✕", bg=BG, fg=DIM, font=(FONT, 10), cursor="hand2")
        close.pack(side="right")
        close.bind("<Button-1>", lambda e: self.quit())
        self.title = tk.Label(top, text="CLAUDE", bg=BG, fg=ACC, font=(FONT, 11, "bold"))
        self.title.pack(side="left", padx=10, pady=6)

        self.canvas = tk.Canvas(self.root, bg=BG, highlightthickness=0, width=WIDTH)
        self.canvas.pack(fill="both", expand=True)
        self.body = tk.Frame(self.canvas, bg=BG)
        self.canvas.create_window(0, 0, window=self.body, anchor="nw", width=WIDTH)
        self.body.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.root.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(-e.delta // 120, "units"))

        self.place()
        self.render()
        self.loop_limits()
        self.loop_scan()
        self.tick()

    def place(self):
        x, y, w, h = self.bar.dock(COLLAPSED if self.collapsed else WIDTH)
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def toggle(self):
        self.collapsed = not self.collapsed
        self.toggle_btn.config(text="▶" if self.collapsed else "◀")
        for w in (self.title, self.canvas):
            w.pack_forget()
        if not self.collapsed:
            self.title.pack(side="left", padx=10, pady=6)
            self.canvas.pack(fill="both", expand=True)
        self.place()

    def quit(self):
        self.bar.remove()
        self.root.destroy()

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
        self.render()

    def loop_limits(self):
        def got(r):
            self.limits, self.err = r, ""
            self.render()
        self.bg(fetch_limits, got)
        self.bg(fetch_rtk, lambda r: (setattr(self, "rtk", r), self.render()))
        self.root.after(USAGE_EVERY * 1000, self.loop_limits)

    def loop_scan(self):
        def work():
            c = scan(self.cache)
            CACHE.write_text(json.dumps(c))
            return c
        def got(c):
            self.cache, self.summary = c, summarize(c)
            self.render()
        self.bg(work, got)
        self.root.after(SCAN_EVERY * 1000, self.loop_scan)

    def tick(self):  # keep reset countdowns fresh without refetching
        self.render()
        self.root.after(30_000, self.tick)

    # widgets
    def head(self, text):
        tk.Label(self.body, text=text.upper(), bg=BG, fg=DIM, font=(FONT, 8, "bold"),
                 anchor="w").pack(fill="x", padx=12, pady=(12, 3))

    def text(self, t, fg=FG, size=9, parent=None, **kw):
        tk.Label(parent or self.body, text=t, bg=(CARD if parent else BG), fg=fg, font=(FONT, size),
                 anchor="w", justify="left", wraplength=WIDTH - 40, **kw).pack(fill="x", padx=12, pady=1)

    def meter(self, label, pct, right=""):
        f = tk.Frame(self.body, bg=BG)
        f.pack(fill="x", padx=12, pady=3)
        tk.Label(f, text=label, bg=BG, fg=FG, font=(FONT, 9)).pack(side="left")
        tk.Label(f, text=right, bg=BG, fg=DIM, font=(FONT, 8)).pack(side="right")
        c = tk.Canvas(self.body, height=6, bg=CARD, highlightthickness=0, width=WIDTH - 24)
        c.pack(padx=12, anchor="w")
        col = SEV["high"] if pct >= 90 else SEV["warn"] if pct >= 70 else SEV["good"]
        c.create_rectangle(0, 0, (WIDTH - 24) * min(pct, 100) / 100, 6, fill=col, width=0)

    def render(self):
        if self.collapsed:
            return
        for w in self.body.winfo_children():
            w.destroy()
        s, L = self.summary, self.limits

        self.head("Limits")
        if L:
            for l in L.get("limits", []):
                if l["kind"] == "weekly_scoped" and not l["percent"]:
                    continue
                name = {"session": "Session (5h)", "weekly_all": "Weekly"}.get(l["kind"]) or \
                    f"Weekly · {((l.get('scope') or {}).get('model') or {}).get('display_name', 'scoped')}"
                self.meter(f"{name}  {l['percent']}%", l["percent"], f"resets {until(l['resets_at'])}")
            rows = (L.get("seven_day_breakdown") or {}).get("rows") or []
            if rows:
                self.text("  ·  ".join(f"{r['display_name']} {r['percent']}%" for r in rows if r["percent"]),
                          fg=DIM, size=8)
        else:
            self.text("loading…" if not self.err else "unavailable", fg=DIM)
        if self.err:
            self.text(self.err[:120], fg=SEV["high"], size=8)

        if not s:
            self.head("Usage")
            self.text("scanning transcripts (first run takes a minute)…", fg=DIM)
            return

        self.head("Live sessions")
        for name, a in s["live"][:5] or []:
            ctx = a["peak_ctx"]
            self.text(f"● {name}   ctx {fmt(ctx)}   {a['turns']} turns",
                      fg=SEV["high"] if ctx > 300_000 else SEV["warn"] if ctx > 150_000 else FG)
        if not s["live"]:
            self.text("none active in last 15 min", fg=DIM)

        self.head("Top projects · 7 days")
        top = s["proj"][:7]
        big = top[0][1] if top else 1
        total = sum(v for _, v in s["proj"]) or 1
        for name, v in top:
            f = tk.Frame(self.body, bg=BG)
            f.pack(fill="x", padx=12, pady=1)
            tk.Label(f, text=name[:26], bg=BG, fg=FG, font=(FONT, 9)).pack(side="left")
            tk.Label(f, text=f"{v / total:.0%}", bg=BG, fg=DIM, font=(FONT, 8)).pack(side="right")
            c = tk.Canvas(self.body, height=3, bg=BG, highlightthickness=0, width=WIDTH - 24)
            c.pack(padx=12, anchor="w")
            c.create_rectangle(0, 0, (WIDTH - 24) * v / big, 3, fill=ACC, width=0)

        tips, verdict, parts = advice(s, L, self.rtk)
        self.head("Where tokens go · 7 days")
        self.text("   ".join(f"{k} {v:.0%}" for k, v in parts.items()), size=8)
        t = s["tot"]
        self.text(f"raw: {fmt(t[3])} cache-read · {fmt(t[1] + t[2])} cache-write · {fmt(t[4])} out · "
                  f"subagents {s['sub_w'] / (t[5] or 1):.0%}", fg=DIM, size=8)
        self.text("tools: " + ", ".join(f"{n} {c}" for n, c in s["tools"][:6]), fg=DIM, size=8)

        self.head("Suggestions")
        for sev, tip in tips:
            card = tk.Frame(self.body, bg=CARD, highlightthickness=0)
            card.pack(fill="x", padx=12, pady=3)
            tk.Frame(card, bg=SEV[sev], width=3).pack(side="left", fill="y")
            tk.Label(card, text=tip, bg=CARD, fg=FG, font=(FONT, 9), anchor="w", justify="left",
                     wraplength=WIDTH - 50).pack(fill="x", padx=8, pady=6)

        self.head("Caveman / Ponytail / RTK")
        self.text(verdict, size=9)
        self.text(f"updated {datetime.now():%H:%M}", fg=DIM, size=7)


def check():
    """Self-check against real transcripts: parse, summarize, advise without UI."""
    c = scan({})
    assert c, "no transcripts found"
    s = summarize(c)
    assert s["tot"][5] > 0 and s["proj"], s
    tips, verdict, parts = advice(s, None, ("1M", 50.0))
    assert abs(sum(parts.values()) - 1) < 1e-6 and "RTK" in verdict
    assert until(None) == "" and fmt(1500) == "1.5k"
    for name, v in s["proj"][:5]:
        print(f"{name:30} {fmt(v)}")
    print(*(t for _, t in tips), verdict, sep="\n- ")
    print("ok")


if __name__ == "__main__":
    if "--check" in sys.argv:
        check()
    else:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        Sidebar().root.mainloop()
