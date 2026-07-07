# claude-sessions

**A local dashboard for your Claude Code sessions.** Point it at `~/.claude`,
open the page, and see every session grouped by project — with live status,
stats, a full transcript reader, and one-click resume.

No install, no dependencies, no daemon. It's a single Python file that runs on
the standard library. `python3 claude-status.py` and you're looking at it.

<p align="center">
  <img src="docs/screenshot.png" alt="The claude-sessions dashboard: sessions grouped by project with status dots, stats, and resume buttons" width="900">
</p>

## Why

Claude Code persists every session to `~/.claude/projects/*/*.jsonl`, but there's
no built-in way to see them all at once. This project was born from a disk-full
incident with 10+ open sessions that needed to survive a reboot — the on-disk
history made the working set recoverable, and this dashboard turns that state
into a live console for finding, triaging, and resuming your work.

## Features

- **Everything at a glance** — every session grouped by project directory, with
  start time, last-active (colour-coded by recency), message counts, model, git
  branch, output tokens, and on-disk size.
- **Live status** — a coloured dot per session: green for open, pulsing for
  busy, grey for closed — detected from the running Claude processes, not
  guessed from file mtimes.
- **One-click resume** — the ⧉ button copies `cd "<dir>" && claude --resume <id>`
  straight to your clipboard.
- **Transcript reader** — a **view** link opens the full conversation on its own
  linkable page: distinct user/assistant turns, collapsed tool-calls and
  thinking, in-transcript search, a prompt-jump sidebar, and lazy sub-agent
  expansion.
- **Reboot survival** — ⚑ flag the sessions you're not done with, restart, filter
  to "Flagged", and resume them. Marks persist server-side across reboots and
  browsers.
- **Filter & sort** — free-text search (project / title / message / branch / id /
  model), a project dropdown, recency chips, and sortable columns.
- **Memory footprint** — a per-session RAM column and a header chip totalling how
  much memory your open sessions are holding — a number the OS can't tell you.
- **`/done` in a session** — mark the current session complete so it drops out of
  the default view; pairs naturally with `/exit`.

## Requirements

Python 3 with the standard library. That's it — no `pip install`. Tested on
Python 3.13; any recent Python 3 should work. macOS and Linux.

## Quick start

```bash
git clone https://github.com/demitri/claude-sessions.git
cd claude-sessions
python3 claude-status.py
```

This serves the dashboard on <http://127.0.0.1:7878> and opens your browser.

```bash
python3 claude-status.py --port 9000   # serve on a different port
python3 claude-status.py --no-open     # don't auto-open the browser
python3 claude-status.py --once        # write a static index.html snapshot and exit
python3 claude-status.py --done        # mark the current session "done" and exit
```

The server rescans `~/.claude/projects/` on every request (cached per file by
mtime + size), and the page auto-refreshes every 30 seconds.

## The `/done` slash command

An example `/done` command ships in `commands/done.md`. Copy it to
`~/.claude/commands/done.md` and set the checkout path (see the note at the top
of the file). It marks the **current** session complete — reading
`$CLAUDE_CODE_SESSION_ID`, so it takes no argument — dropping it from the
dashboard's default view. Type `/done` then `/exit` to close out a finished
session in one gesture.

The raw CLI also accepts an optional session id or the statusline's last-4
(`python3 claude-status.py --done 6789`) for marking a session done from a plain
terminal, outside any session. A running dashboard picks the mark up on its next
refresh.

## How it works

Everything lives in `claude-status.py` — a stdlib `ThreadingHTTPServer` backend
and a single embedded HTML/CSS/JS page, no build step and no framework. The
backend scans `~/.claude/projects/*/*.jsonl`, parses each session defensively
(the format is an undocumented Claude Code internal, so malformed *lines* are
skipped, never whole files), and serves the data as JSON. Live status comes from
`~/.claude/sessions/<pid>.json`, cross-checked against the running processes.

For a deeper tour, see [`AI/dashboard.md`](AI/dashboard.md); for orientation,
[`AI/START_HERE.md`](AI/START_HERE.md).

## Privacy

Everything runs locally and binds to `127.0.0.1`. Nothing is sent anywhere — the
dashboard only reads the session files Claude Code already wrote to your disk.

## License

[MIT](LICENSE) © Demitri Muna
