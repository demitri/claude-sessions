# claude-sessions

A local dashboard for your Claude Code sessions. Scans `~/.claude/projects/` and
serves a sortable, filterable web page showing your sessions grouped by project
directory, with per-session stats and one-click resume commands.

Runs on the Python 3 standard library — no install step required (tested on
3.13; any recent Python 3 should work). Third-party packages are allowed where
one clearly earns its place, but stdlib stays the default so "just run it"
keeps working.

## Run

```bash
python3 claude-status.py            # serve on http://127.0.0.1:7878 and open a browser
python3 claude-status.py --port 9000
python3 claude-status.py --no-open  # don't auto-open the browser
python3 claude-status.py --once     # write a static index.html snapshot and exit
python3 claude-status.py --done     # mark the current session "done" and exit
```

An example **`/done` slash command** ships in `commands/done.md` — copy it to
`~/.claude/commands/done.md` and set the path to your checkout (see the install
note at the top of the file). It marks the **current** session complete so it
drops out of the dashboard's default view — type `/done` then `/exit` to close
out a finished session in one gesture. It reads `$CLAUDE_CODE_SESSION_ID`, so it
takes no argument. (Setting done also
clears any "reopen" flag — done means nothing's pending.) The raw CLI also accepts
an optional session id or the statusline's last-4 — `python3 claude-status.py
--done 6789` — for marking a session done from a plain terminal, outside any
session. The running dashboard picks the mark up on its next refresh (it reloads
`flags.json` when the file changes).

The server rescans `~/.claude/projects/` on every request (results are cached per
file by mtime+size), and the page auto-refreshes every 30 s.

## What it shows

- **Summary cards** — total sessions, Live (≤15m), Active (≤2h), projects,
  messages, output tokens, on-disk size.
- **Sortable table** — click any column header; click again to reverse.
- **Filters** — free-text search (project / title / message / branch / id /
  model), a project dropdown, and status chips (All · Live · Active · Today,
  defaulting to Today).
- **Group-by-project** toggle — per-project sections with counts and token
  subtotals.
- **Per-session stats** — start time, last-active (relative, colour-coded by
  recency), user/assistant message split, output tokens, model, git branch, file
  size, RAM (for open sessions), and the first real user message as a preview.
- **Open vs closed** — the leading dot shows live state (green open · pulsing
  busy · grey closed), detected from the running Claude processes; an "Open"
  toggle and count filter to live sessions.
- **Reboot survival** — ⚑ flag the sessions you're not done with, then after a
  restart filter to "Flagged" and resume them. ✕ mark sessions you're finished
  with as *done* (hidden by default; "✕ Done" reveals them). Both marks persist
  server-side across reboots and browsers.
- **⧉ resume** — copies `cd "<dir>" && claude --resume <id>` to the clipboard.

## Transcript reader

Every session row has a **view** link that opens the full conversation on its
own linkable page (`/session?id=<id>`):

- User and assistant turns rendered distinctly, with tool calls and thinking
  blocks collapsed behind one-line summaries — expand only what you care about.
- **Search** across the transcript, with a scope toggle (prompts only vs
  everything).
- **Prompt-jump navigator** — a sidebar of your prompts for skipping straight
  to any point in the conversation.
- **Sub-agents panel** — sessions that spawned sub-agents list them for lazy
  expansion, and each sub-agent transcript is linkable too
  (`/session?id=<id>&agent=<agentId>`).

## Why it exists

Born from a disk-full incident with 10+ open Claude sessions that needed to
survive a reboot. Claude Code persists every session to `~/.claude/projects/*/*.jsonl`,
so the working set can be reconstructed after a restart — this dashboard turns
that on-disk state into a live console for finding and resuming sessions.

See `AI/START_HERE.md` for orientation and `AI/dashboard.md` for implementation
details.
