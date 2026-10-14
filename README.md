# claude-remainder

An always-on, liquid-glass dock on the right edge of your Windows screen that keeps your Claude Code usage in view: session and weekly limits, live sessions, which projects eat the most tokens, and suggestions drawn from your own transcripts.

## Using it

- **Dock**: a small glass tab hugging the right screen edge with two rings, the 5-hour session and the weekly limit.
- **Hover a ring**: a glass pill slides out with the exact percentage, reset countdown and weekly pace.
- **Chevron button**: opens the full panel with four tabs (Overview, Projects, Tips, Plugins). Arrow keys switch tabs, Esc closes.
- **Drag** the dock up or down the edge. **Right-click** it to refresh or quit.

The glass is real: each window is excluded from screen capture, so the app grabs what is behind it, blurs and tints it, and adds rim light and sheen. It refreshes when the screen behind changes.

## What the panel shows

- **Limits**: 5-hour session and weekly utilization with reset countdowns, from the same endpoint `/usage` reads.
- **Live sessions**: sessions active in the last 15 minutes and their context size (amber above 150k, red above 300k).
- **Top projects (7 days)**: each project's share of weighted usage.
- **Where tokens go**: cache reads vs cache writes vs output vs fresh input, subagent share, most-used tools.
- **Suggestions**: weekly pacing, oversized sessions, baseline context per fresh session, model mix, tool habits.
- **Caveman / Ponytail / RTK**: how much output-trimming plugins can save at most, given your output share, plus `rtk gain` totals.

Token weights mirror API price ratios (cache read 0.1x, cache write 1.25x/2x, output 5x). Anthropic doesn't publish plan limits in tokens, so the project shares rank projects against each other rather than measure exact cost.

## Requirements

- Windows 10 (2004+) or 11
- Python 3.10+ with Tkinter (the standard installer includes it)
- Claude Code logged in with a Claude subscription (`~/.claude/.credentials.json`)
- [Pillow](https://pypi.org/project/pillow/) (`pip install pillow`) for the glass rendering
- Optional: [rtk](https://github.com/rtk-ai/rtk) on PATH for the RTK savings line

## Run

```
pythonw sidebar.py
```

The first scan of `~/.claude/projects` takes a few seconds to a minute; later scans only re-read transcripts that changed (cached in `.scan-cache.json`, git-ignored).

Self-check against your real transcripts and the glass compositor, no UI:

```
python sidebar.py --check
```

### Start at login

Create a shortcut in `shell:startup` pointing to `pythonw.exe` with the full path to `sidebar.py` as its argument.

Set `CLAUDE_DOCK_DEBUG=1` to make the windows visible to screenshots (the glass backdrop then freezes, since it would otherwise capture itself).

## Privacy

Everything runs locally. The only network call is to `api.anthropic.com/api/oauth/usage`, made with your own Claude Code OAuth token, which is read from disk at request time and never stored or logged. Transcripts and the screen grabs used for the glass effect never leave your machine.
