# TODO

Open ideas (none blocking; v1 works):

- [ ] **Remote sessions** — aggregate other servers into the dashboard via an
      SSH-pipe `--emit` role + hub merge, with activity-adaptive polling.
      **Design in `AI/remote.md`** — review-complete (codex ×6, sonnet ×4, opus
      ×3, all dry); not built. Two must-verify-before-code items are recorded in
      the doc: (1) what a dropped SSH does to the remote `claude` process + how
      `claude --resume` behaves against a live session (drives the conditional
      reopen), and (2) `REMOTE_TIMEOUT` vs. worst-case cold parse.
- [ ] **Transcript viewer follow-ups** (viewer itself shipped — see Done):
      virtualization/`content-visibility` if the largest sessions lag (user
      chose eager render for v1); append-on-poll live refresh (v1 is a
      load-time snapshot + manual ↻); `include_media=off` escape hatch only if
      a pathological session appears.
- [ ] **squad integration** — optional inter-session messaging surface
      (transcript / tasks / roster pages over squad's SQLite bus, agent↔session
      transcript links, phase-2 browser participation). **Silent absence: zero
      UI change when no `.squad/` workspace exists.** Design in `AI/squad.md`
      — written 2026-07-05, needs review rounds (codex → sonnet → opus) before
      build. Product stance: claude-sessions is the product; squad is a feature.
- [ ] **Copy-all-resume** button — one click to copy every visible (filtered)
      session's resume command, for fast post-reboot restoration.
- [ ] **Context-window column** — `ctx_tokens` is already parsed; surface it
      (e.g. latest context size, maybe as a % of the model's window).
- [ ] **Auto-start on login** — a LaunchAgent plist so the dashboard is always up
      at `127.0.0.1:7878`.
- [ ] Consider a `--restore-sheet` mode that writes the markdown restore sheet
      (the original one-off that seeded this project).

## Maybe pile (deferred, not now)

- [ ] _(none right now)_

## Done

- [x] **Purge warnings** (2026-07-07) — surface sessions about to age out of the
      `cleanupPeriodDays` retention window. `cleanup_period_days()` reads the
      effective value (managed > user settings, default 20); `collect()` sets
      `expires_ts = mtime + window` per session. A subtle `#purgenote` line under
      the "Updated" header counts sessions `<48h` from purge (only the count
      tinted; hint in tooltip); each row `<24h` out gets a `.purgewarn` line
      (past-due → "purged on next Claude Code start"). Skips `done`. Verified
      against an aged-mtime fixture (7.2h → warn, −9.6h → past-due, fresh → none)
      + `node --check`. Supersedes the old "Expires-in column" idea.
- [x] **Public-facing polish** (2026-07-07) — rewrote `README.md` for an outside
      audience (hook, feature list, quick-start clone, privacy note), added
      `LICENSE` (MIT), and embedded `docs/screenshot.png` near the top. The
      screenshot is generated from **fabricated** data (no private repos) by
      `tools/make_fixture.py`, which builds an isolated `$HOME` of invented
      sessions, runs `--once`, and injects open-state + RAM into the inlined
      JSON (no live process / no real RAM) → `demo.html`; re-run it to refresh
      the shot when the UI changes.
- [x] First git commit + remote (2026-07-03) — public repo at
      `github.com:demitri/claude-sessions`.
- [x] **Session transcript viewer** (2026-07-02) — `/session?id=[&agent=]` page:
      full history (incl. content-bearing `system` markers), prompt-jump
      sidebar, search with scope toggle (default prompts+assistant), collapsed
      tool/thinking folds + expand-all, lazy per-`Agent`-call sub-agent
      expansion, sub-agents index panel, per-row "view" link (new tab,
      suppressed in `--once`), gzip `/api/session`, shlex-quoted `resume_cmd`,
      exact-path routing. Design + format facts + implementation notes in
      `AI/transcript.md`.
- [x] **In-session "mark done"** — `claude-status.py --done` (no arg = current
      session via `$CLAUDE_CODE_SESSION_ID`; or a full id / statusline last-4) +
      a `/done` user slash command (`~/.claude/commands/done.md`). Server now
      reloads `flags.json` on mtime (`refresh_flags()` under `_flags_lock` in
      `collect()` + before each POST; `save_flags` records its own write) so
      external marks show live without clobber. Tested: `tests/test_done.py`
      (17/17, isolated under a throwaway $HOME; `python3 tests/test_done.py`).
- [x] Extracted from `~/claude-status/` into its own repo `$GH/claude-sessions`.
- [x] v1 dashboard: scan, stats, sort, filter, group-by-project, resume-copy.
- [x] Fix last-active (UTC via `calendar.timegm`), human-readable token counts
      (K/M/B/T + commas), 24h dates.
- [x] Filter out non-conversation sidecars (zero-turn `ai-title`/`bridge-session`)
      and headless/SDK runs (`entrypoint:"sdk-cli"`).
- [x] Project shortcut chips above the filter box (click a project name to add;
      chip quick-filters, `✕` removes; persisted in `localStorage`). No row
      reordering. Label toggles Shortcuts ⇄ All-projects (by last active).
- [x] Rolling **24h** recency chip (replaces calendar "Today" — survives midnight).
- [x] Toned-down git-branch styling (smaller, own line); friendly status-dot tooltips.
- [x] Two-row entries: compact data row + full-width prompt sub-row (name chip +
      prompt); dropped the narrow "Session" column and the visible session hash.
- [x] Open vs closed: detect live sessions via `~/.claude/sessions/<pid>.json`
      (alive PID). Leading dot = open-state (green / green-pulse busy / grey
      closed); recency moved to a colour-coded "Last active"; "● Open" is an
      independent toggle (ANDs with the time chip); Open count card; denser cards;
      24h "updated" subtitle (de-duped the session count).
- [x] Reboot-survival workflow: ⚑ reopen-after-restart flag per session
      (server-side `~/.config/claude-sessions/flags.json`, `POST /api/flag`),
      "⚑ Flagged" filter toggle, flagged-row highlight. Per-session RAM (RSS via
      batched `ps`) column + "ram·open" stat.
- [x] Tokens to 1 decimal; "updated {d} {Mon} {hh:mm}" 24h subtitle.
- [x] Flag under the dot (first column, centred/aligned); RAM rounds MB /
      switches to GB >999MB; visible accent sort arrow.
- [x] Stats: single inline strip (`#cards2`) replaces the card grid; flagged
      count updates live on toggle; "Copy flagged" button removed.
- [x] Second mark ✕ "done" (finished/dismissed) under the ⚑ flag; done sessions
      hidden by default, "✕ Done" toggle restores them (dimmed). Generalised
      `flags.json` to `{id:{flag,done}}` + `POST {id,kind,value}` (legacy migrate).
