# START HERE

`claude-sessions` is a local, zero-dependency web dashboard for browsing and
resuming Claude Code sessions. Phase: **working v1**, just extracted into its own
repo.

## What this is

Claude Code persists every session as JSONL under `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`.
This project reads those files and serves a swanky single-page dashboard:
sessions grouped by project directory, with stats (started, last active, message
counts, model, git branch, output tokens, size) and a one-click **resume**
command per session. It's a single Python file using only the standard library —
run it, open the browser, done.

It was built during a disk-full incident: the user had 10+ live sessions that
needed to survive a reboot, and Claude Code's on-disk session history made the
working set recoverable. The dashboard is the durable, live version of that
recovery console.

## Current state

- `claude-status.py` — the whole thing: a stdlib HTTP server + an embedded
  single-page app (HTML/CSS/JS in one Python string). **Works.** Verified serving
  ~270 historical sessions; HTTP 200, `/api/sessions` returns parsed JSON.
- Public repo at `github.com:demitri/claude-sessions` (origin); history starts
  at the 2026-07-03 initial commit. Originally built at `~/claude-status/`
  (now deleted).

## Where things are

- `claude-status.py` — server + dashboard (see `AI/dashboard.md` for internals).
- `README.md` — user-facing usage.
- `AI/dashboard.md` — implementation details: session-file parsing, data model,
  the embedded SPA, gotchas.
- `AI/remote.md` — design (not yet built) for aggregating sessions from other
  servers via an SSH-pipe `--emit` role, hub merge, and adaptive polling.
- `AI/transcript.md` — the per-session history/reader page (**built 2026-07-02**):
  separate linkable `/session?id=[&agent=]` route, full conversation, search
  (scope toggle), distinct user/assistant turns, prompt-jump navigator,
  collapsible tool/thinking, lazy sub-agent expansion + sub-agents panel.
  Design doc + verified JSONL format facts + implementation notes.
- `AI/TODO.md` — open ideas / next steps.
- `tests/test_done.py` — isolated stdlib tests for the `--done` CLI / flags
  invariant (17, run under a throwaway `$HOME`): `python3 tests/test_done.py`.

## Conventions a fresh session would otherwise violate

- **Prefer stdlib.** The "just run it" (no `pip install`) property is the point,
  so stdlib is the default and a new dependency has a high bar — use one only when
  it clearly earns its place, but don't bend over backwards to avoid it.
- **Defensive parsing.** `~/.claude/projects/*.jsonl` is an undocumented,
  unversioned Claude Code internal — skip malformed *lines* (never whole files),
  fall back when fields are missing. This is the sanctioned exception to the
  user's global "never silently skip" rule, because the format is external.
- The page and the data API live in **one file** on purpose. Don't split into a
  framework/build step without a reason — it would break the zero-dependency,
  single-file value.
