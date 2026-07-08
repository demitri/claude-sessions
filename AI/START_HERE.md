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
- `README.md` — public-facing usage (screenshot, features, quick-start). `LICENSE`
  is MIT.
- `docs/screenshot.png` — the README hero image, generated from **fabricated**
  data (no private repos) by `tools/make_fixture.py`: it writes an isolated
  `$HOME` of invented session files, runs `claude-status.py --once` against it
  (reusing the app's own `write_static()`, so the page never drifts from the
  live UI), then **injects** the open-state + RAM fields (`open`/`live_status`/
  `rss_kb`) into the inlined JSON — so the RAM column/chip and green dots render
  with no live process and no real RAM. Output is `$FIXTURE_HOME/demo.html`
  (a plain file:// page — nothing to kill afterward). Re-run to refresh the
  screenshot when the UI changes; see the script's docstring.
- `AI/dashboard.md` — implementation details: session-file parsing, data model,
  the embedded SPA, gotchas.
- `AI/search.md` — design (not yet built, **ready to implement**) for full
  transcript-on-disk search: raw-byte JSON-encoded-query prefilter → parse only
  hit files → snippet results with metadata highlights, deep-linked to the
  matching turn (`#t<idx>`). Distinct from the existing metadata filter.
- `AI/remote.md` — design (not yet built) for aggregating sessions from other
  servers via an SSH-pipe `--emit` role, hub merge, and adaptive polling.
- `AI/session-ipc-research.md` — background research for squad: notification
  paths, inter-session IPC cost model (one model invocation per message),
  ecosystem survey, and the 2026-07-05 two-session squad validation trial.
- `AI/squad.md` — design (not yet built, not yet reviewed) for optional
  [squad](https://github.com/mco-org/squad) integration: view/search the
  inter-session message bus, task lifecycles, agent↔session links. Hard rule:
  **silently absent** when no `.squad/` workspace exists (sanctioned exception
  to no-silent-skip; data-presence gate, not error suppression).
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
