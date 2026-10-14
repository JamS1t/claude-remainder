# claude-remainder

An always-on Windows sidebar that keeps your Claude Code usage in view: session and weekly limits, live sessions, which projects eat the most tokens, and suggestions drawn from your own transcripts.

## What it shows

- **Limits**: 5-hour session and weekly utilization with reset countdowns, from the same endpoint `/usage` reads.
- **Live sessions**: sessions active in the last 15 minutes and their context size (amber above 150k, red above 300k).
- **Top projects (7 days)**: each project's share of weighted usage.
- **Where tokens go**: cache reads vs cache writes vs output vs fresh input, subagent share, most-used tools.
- **Suggestions**: weekly pacing, oversized sessions, baseline context per fresh session, model mix, tool habits.
- **Caveman / Ponytail / RTK**: how much output-trimming plugins can save at most, given your output share, plus `rtk gain` totals.

Token weights mirror API price ratios (cache read 0.1x, cache write 1.25x/2x, output 5x). Anthropic doesn't publish plan limits in tokens, so the project shares rank projects against each other rather than measure exact cost.

## Requirements

- Windows 10/11
- Python 3.10+ with Tkinter (the standard installer includes it)
- Claude Code logged in with a Claude subscription (`~/.claude/.credentials.json`)
- Optional: [rtk](https://github.com/rtk-ai/rtk) on PATH for the RTK savings line

No third-party Python packages.

## Run

```
pythonw sidebar.py
```

The first scan of `~/.claude/projects` takes a few seconds to a minute; later scans only re-read transcripts that changed (cached in `.scan-cache.json`, git-ignored).

Self-check against your real transcripts, no UI:

```
python sidebar.py --check
```

### Start at login

Create a shortcut in `shell:startup` pointing to `pythonw.exe` with the full path to `sidebar.py` as its argument.

## Privacy

Everything runs locally. The only network call is to `api.anthropic.com/api/oauth/usage`, made with your own Claude Code OAuth token, which is read from disk at request time and never stored or logged. Transcripts never leave your machine.
