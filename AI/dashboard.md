# dashboard internals

Everything lives in `claude-status.py`. Two halves:

1. **Backend (Python, stdlib):** scans `~/.claude/projects/*/*.jsonl`, parses each
   session, serves JSON + the page via `ThreadingHTTPServer`.
2. **Frontend (embedded SPA):** the `PAGE` string — one HTML doc with inline CSS
   and vanilla JS. No build step, no framework.

## Session file format (`~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`)

Undocumented Claude Code internal. One JSON object per line. Fields used:

- `cwd` — working directory of the session (authoritative; the directory name is
  an encoded form of this, but we read `cwd` from the records, not the dirname).
- `gitBranch` — git branch at session time.
- `version` — Claude Code version.
- `timestamp` — ISO 8601 **UTC** (trailing `Z`) per record; first = started,
  last = updated. Falls back to file mtime if absent. Parsed with
  `calendar.timegm` (treats the struct as UTC) — `time.mktime` would skew it by
  the local UTC offset and push "last active" into the future.
- `type` — `"user"` / `"assistant"` (we count both; others ignored).
- `message.content` — string or list of parts (`{type:"text", text:...}`); used
  for the preview and the "named this session" title.
- `message.model` — assistant model id.
- `message.usage` — `output_tokens` (summed → "Out tok"), and
  `input_tokens` + `cache_read_input_tokens` + `cache_creation_input_tokens`
  (latest → `ctx_tokens`, captured but not yet shown in the table).

Parsing is per-line defensive: a bad line is skipped, never the whole file.
Results are cached by `(mtime, size)` so unchanged files aren't reparsed.

### Open vs closed sessions

A session is **open** iff a live Claude process owns it. `~/.claude/sessions/`
holds one `<pid>.json` per running process — `{pid, sessionId, cwd, status
("idle"/"busy"), kind, name, …}`. `open_sessions()` reads them and keeps those
whose `pid` is still alive (`pid_alive` via `os.kill(pid, 0)`), so a crashed
process's stale file is ignored. `collect()` then sets `s["open"]` and
`s["live_status"]` **per request** — these are process-derived, so they are NOT
stored in `parse_session`'s `(mtime,size)` cache (the file doesn't change when a
session is opened/closed). `lsof` is *not* usable here: Claude appends to the
`.jsonl` and closes it rather than holding it open.

`<pid>.json` also gives **per-session RAM**: `open_sessions()` batches one `ps -o
pid=,rss= -p …` call and attaches `rss_kb` to each open session (closed → 0).
RSS over-counts shared pages, so totals are an upper-ish bound on reclaimable
memory. Surfaced as a "RAM" column and, summed across all loaded sessions, as
the header RAM chip (below) — this is entirely client-side arithmetic over
`rss_kb`, no separate backend endpoint.

### Header RAM chip

`#ramchip` (header row, left of "Group by project") shows total RSS across all
loaded Claude sessions (open only — closed sessions contribute 0), e.g. "1.7 GB
Claude RAM". `ramChip(DATA)` (in `load()`, same 30s cadence as everything else)
sums `rss_kb` and tints the chip's *background* at two thresholds —
`RAM_WARN_KB`/`RAM_CRIT_KB`, currently 3GB/6GB (`.ramchip.warn`/`.ramchip.critical`;
critical also pulses via the same `@keyframes` pattern as the busy-session dot).

This is deliberately **not** system-wide RAM or OS memory pressure — the point
is "are my own Claude sessions using a lot of memory," a number the OS's own
memory-pressure indicator can't tell you. The 3GB/6GB thresholds are a starting
guess (picked with no strong evidence, per the user 2026-07-03), not derived
from anything — expect to retune `RAM_WARN_KB`/`RAM_CRIT_KB` (single constants,
top of the `<script>` block) if they don't match real usage patterns.

The refresh button lives next to the session `#count` (in `.sub`) as a small
icon-only button (`.copy.refresh-sm`), moved out of the header chips row to
make room for the RAM chip.

### Per-session marks (flag / done)

Two persistent marks, each in the sub-row beneath the related main-row control:
⚑ in the first column (under the open-state dot), ✕ in the last column (under the
`⧉ resume` button), with the prompt cell spanning `COLS.length-2` between them:

- **⚑ flag** = "reopen after restart" — the reboot-survival list. Flagged rows
  get an amber tint + accent and stay flagged after the session closes.
  "⚑ Flagged" filter toggle shows only these.
- **✕ done** = "finished/dismissed". **Done sessions are filtered out by
  default**; the "✕ Done" toggle (`showDone`) brings them back, shown dimmed with
  a red accent. (Marking done while hidden simply makes the row vanish.) Note
  `done` now carries two intents — "work completed successfully" (via `/done`)
  and the original "abandoned/dismissed" — collapsed into one hide-mark; the
  dashboard can't distinguish them. Fine for a personal tool; if an "outcome"
  distinction is ever wanted, that's where it'd go (a new mark kind, not a
  reinterpretation of `done`).

Both key on session id, so they persist after the session closes. Storage is
**server-side JSON** (`~/.config/claude-sessions/flags.json`,
`{sessionId: {"flag": ts, "done": ts}}` — key present = set) so marks survive a
reboot and are shared across browsers. Written atomically (`os.replace`) under
`_flags_lock` via `POST /api/flag` (`{id, kind, value}`, `kind ∈ MARK_KINDS`);
legacy bare `{ts}` entries load as a flag. `collect()` sets `s["flagged"]` and
`s["done"]` per request. (`localStorage` was rejected: per-browser and wipeable.)
The static `--once` snapshot has no server, so toggles there fail gracefully.

**`flags.json` is a shared source of truth (mtime-reloaded).** `FLAGS` is held in
memory, but it is re-synced from disk whenever the file's mtime moves —
`refresh_flags()` (one `stat`, reload only on change) is called under `_flags_lock`
at the start of `collect()` and before each `POST /api/flag` mutation, and
`save_flags()` records its own write's mtime so the server never reloads its own
change. This lets *external* writers share the store with the UI without a
restart or a clobber (before this, an outside write was invisible and the next UI
toggle would overwrite it). The one external writer today is the `--done` CLI
(below); the guard also covers a hand edit or a second dashboard process.

**In-session "mark done" (`--done` + `/done`).** `python3 claude-status.py --done`
sets the `done` mark from the CLI and exits (no server). With no argument it marks
the *current* session via `$CLAUDE_CODE_SESSION_ID` (verified 2026-07-01: set in
every Claude Code shell, and its value **equals the transcript filename stem**
`<session-id>.jsonl`; a sub-agent's Bash inherits the *parent* session id, so
`--done` from a sub-agent marks the parent — the desirable behavior). The no-arg
path **warns (not fails)** if no local transcript matches the id — a brand-new
session may not be flushed yet, or a non-interactive session's id isn't a file —
and still records the mark. Or pass a full id / trailing
fragment (`--done 6789`, the statusline's last-4 — a leading `#`, as the
statusline prints, is tolerated), resolved by `resolve_session_id()` (suffix
match; errors loudly on no/ambiguous match rather than guessing).

**Invariant: a session is never both `done` and `flag`** (they're opposites —
"nothing-pending" vs "reopen-later"). Setting either mark clears the other,
enforced on **every** write path: the `--done` CLI (clears `flag`), the
`POST /api/flag` handler (`marks.pop("done" if kind=="flag" else "flag")`), and
the dashboard's optimistic client (`toggleFlag` clears the opposite mark, guards
against rapid same-row re-clicks with an in-flight set, and rolls back *both*
marks if the POST fails). `load_flags` also **collapses any legacy both-set row**
to the more-recently-set mark (strictly later timestamp wins; a **tie or an
unparseable timestamp keeps the row visible** — drops `done`, keeps `flag`) so a
session with pending work is never silently hidden. (This deliberately favours
visibility over codex's suggested "done wins", per the no-silent-hide rule.) The
`~/.claude/commands/done.md` slash command marks the **current** session only
(no argument — it relies on `$CLAUDE_CODE_SESSION_ID`); the optional session
id/last-4 lives on the raw CLI, for marking a session done from a plain terminal
outside any session.

Concurrency: the dashboard process and the `--done` CLI are independent writers,
so the read-modify-write (`refresh_flags` → mutate → `save_flags`) is wrapped in
a **cross-process `flock`** (`flags_write_lock()`, on a sidecar `.lock` file) to
prevent lost updates, and `save_flags` writes a **unique temp file** (`mkstemp`)
before the atomic `os.replace` so two writers can't clobber a shared temp. Reads
(`collect()`) don't take the file lock — `os.replace` guarantees a reader always
sees a whole file — but they snapshot `FLAGS` under `_flags_lock` so a concurrent
POST can't mutate it mid-scan. A running dashboard reflects an external `--done`
on its next scan via the mtime reload. Remote caveat (see `AI/remote.md`): run
inside a session on a *remote* box, `--done` writes that box's own `flags.json`,
invisible to a hub until flag-sync is built.

### Sidecar files (filtered out)

Not every `.jsonl` is a conversation. ~28% of the top-level files are metadata
sidecars with **zero** user/assistant turns — overwhelmingly `ai-title` (an
orphaned auto-generated title, no `cwd` → project would show "?"), plus the odd
`bridge-session`. `collect()` drops any session with `user_msgs + asst_msgs == 0`
so these never reach the dashboard.

`collect()` *also* drops headless/SDK runs (`entrypoint:"sdk-cli"`, e.g.
`claude -p`) — these have real turns and a `cwd`, but aren't interactive
sessions you'd resume. `entrypoint` is captured in `parse_session` (the
session-init record carries it; `"cli"` = interactive, `"sdk-cli"` = headless).

### Retention

Claude Code prunes transcripts older than `cleanupPeriodDays` (**default 20**,
per the [settings docs](https://code.claude.com/docs/en/settings)) on startup —
so with the default the corpus is a rolling ~20-day window and untouched sessions
age out on their own. It's a knob, though: this dev's machine sets it to `99999`
(effectively never), so **don't assume the window is bounded** — read the actual
value from settings.

**Purge warnings.** `cleanup_period_days()` resolves the effective value —
managed (enterprise) `managed-settings.json` wins over `~/.claude/settings.json`,
else the default 20. Per-project setting overrides are **not** merged (documented
approximation: the real value is almost always user-level, and per-project would
mean a settings read per session dir per scan). `collect()` computes it once and
sets `expires_ts = mtime + cleanupPeriodDays·86400` on each session (using file
**mtime** — the authoritative input to Claude Code's own age-based prune, not the
last-record timestamp), plus a top-level `cleanup_period_days` in the response.
The client surfaces this **quietly** (a rolling window means it's near-always
present, so it's informational, not alarming): `purgeNote()` writes a second
`.sub` line right under the "Updated …" header — dim text with only the **count**
tinted amber (`#purgenote .pn`), counting sessions `<48h` from purge
(`PURGE_WARN_H`); the resume-to-reset hint lives in its `title` tooltip. Each row
`<24h` out (`PURGE_CRIT_H`) additionally gets a `.purgewarn` line under its prompt
(past-due → a "will be purged on next Claude Code start" message). Both skip
`done` sessions. With `cleanupPeriodDays` huge (e.g. 99999), `expires_ts` is far
future and nothing shows — correct by construction.

## Per-session data model (`parse_session` → JSON)

`id, cwd, project, title, preview, started_ts, updated_ts, msgs, user_msgs,
asst_msgs, model, branch, version, entrypoint, size_bytes, mtime, out_tokens,
ctx_tokens, resume` — plus `open`, `live_status`, `rss_kb`, `flagged`, `done`,
and `expires_ts`, injected per request by `collect()` (see "Open vs closed
sessions", "Per-session marks", and "Purge warnings").

- `project` — short name via `project_short()`: strips to the repo name, and
  keeps one parent for grouped families (`thehighlighter/…`, `trillianverse/…`).
  **If new repo families are added under `$GH/<family>/<repo>`, extend the
  hardcoded family list in `project_short()`.**
- `preview` — first *real* user message (leading `<system-reminder>` blocks and
  command wrappers are stripped/skipped).
- `title` — extracted from a `named this session "X"` system-reminder, if present.
- `resume` — `cd "<cwd>" && claude --resume <id>`.

## Frontend

- **Stats** are a single thin inline strip (`.statbar` `#cards2`, `cards2()`) —
  bold value + dim label, divider-separated, one line tall (replaced the old card
  grid, which wrapped). `cards2()` is re-run on flag toggle so the flagged count
  stays live. RAM is formatted by `ram(kb)` — rounded MB (no decimals; it's an
  estimate), switching to 1-decimal GB above ~999 MB. Tokens use `ktok` at 1 dp.
- The leading column stacks the **open-state dot** (main row) and the **⚑ flag**
  (sub-row), both centred (`.dotcell{text-align:center}`) so they share one axis.
- Active sort header shows an accent-coloured `▾/▴` (`th .ar`).
- Unnamed rows show a dim `#<last4-of-id>` matcher (`.sidtail`) at the end of the
  prompt sub-row, to pair a terminal with its dashboard entry. It mirrors the
  global statusline (`~/.claude/statusline-command.sh`), which prints `#<last4>`
  of `session_id` and omits it when `session_name` is set — so named rows
  (identified by their name chip) drop the matcher on both sides.

- **Recency** time chips (mutually exclusive): Live ≤15m, Active ≤2h, 24h, All.
  Default = **24h** (`statusF='day'`, a rolling `now-86400` window — deliberately
  *not* a calendar day, so work across midnight stays visible).
- **`● Open`** is a *separate, independent* toggle (`#openchip`, `openOnly`) that
  ANDs with the recency chip — so "24h + Open" = open sessions active in the last
  day. It is not part of the `#status` radio group.
- The **left-column dot is open-state**, not recency: green = open (`op-open`),
  green **pulsing** = open & busy (`op-busy`), grey = closed (`op-closed`);
  tooltip shows `Open · idle/busy` or `Closed`.
- **Recency moved to the "Last active" column**, coloured by `recencyClass()`:
  green (`r-live`, ≤2h), amber (`r-recent`, ≤24h), dim (`r-idle`, >24h).
- **Each entry is a two-row `<tbody class="entry">`** (`rowHtml`): the main row
  holds the compact columns; a sub-row below carries the session name chip
  (`.subname`, in the Project column — empty status cell first — so it left-aligns
  under the project label) and the full-width prompt preview (`.subprompt`,
  `colspan=COLS.length-2`). The preview is clamped to **2 lines** via CSS
  `-webkit-line-clamp:2` (fills the lines at any width, ellipsis, reflows on
  resize); the server keeps up to 400 chars of `preview` so there's enough text
  to fill both lines. The old narrow "Session" column was removed — it
  wrapped to ~4 lines and left the other columns mostly empty. Entries are kept
  distinct by bordering the sub-row (not the main row) and hovering the whole
  `tbody.entry` as a unit. The full session id is **not** shown (it's embedded in
  the `⧉ resume` command); `s.id` is still searchable.
- Sorting is client-side (`sortKey`/`sortDir`); click a header to toggle. Tables
  use multiple `<tbody>` elements (one per entry), so no single wrapping `<tbody>`.
- Group-by-project renders per-project `<table>` sections with token subtotals.
- **Shortcut chips.** Click a project name (table cell or group header) to add
  it as a chip above the filter box (`#favs`, `renderFavs()`); the chips do **not**
  reorder rows. Clicking a chip body quick-filters to that project (toggles
  `#projsel`; the active chip gets `.on`); the chip's `✕` removes it. State lives
  in `localStorage['cs_favs']` (a JSON array of project names) via
  `loadFavs`/`saveFavs`, wrapped in try/catch so the `--once` static `file://`
  snapshot degrades gracefully.
  The bar's **label is a toggle** (`#favmode`, `bindFavMode()`, persisted in
  `localStorage['cs_favmode']`): "Shortcuts" shows the saved favourites (with
  `✕`); "All projects" lists every project as a chip ordered by last active
  (`allProjectsByLastActive()`), favourites marked with `.star`. `'all'` mode
  reads `DATA`, so `renderFavs()` is called inside `load()` after the fetch.
- `⧉ resume` uses `navigator.clipboard.writeText`.
- Auto-refresh every 30 s (`setInterval(load, 30000)`); the page fetches
  `api/sessions` (relative path, so the static `--once` snapshot also works after
  inlining the data).

## Endpoints

Routes are dispatched on the **exact** `urlsplit(path).path` — never
`startswith` (`"/api/sessions?…".startswith("/api/session")` is also true, so
the prefix idiom would swallow the sessions poll).

- `GET /` → the dashboard page (any unknown path also falls back to it).
- `GET /api/sessions` → `{generated, sessions:[…]}` (fresh scan each call).
- `GET /session?id=<id>[&agent=<agentId>]` → the transcript page
  (`TRANSCRIPT_PAGE`, second embedded SPA — see `AI/transcript.md`).
- `GET /api/session?id=<id>[&agent=<agentId>]` → full parsed transcript JSON,
  gzip-encoded when the client accepts it; 404 unknown id, 409 ambiguous
  fragment, 400 missing id / non-hex `agent`. Cached in `_TRANSCRIPT_CACHE`
  (same `(mtime,size)` pattern as `_CACHE`, deliberately a separate dict —
  same key space, different value shape).
- `POST /api/flag` ← `{id, kind, value}` (`kind ∈ {"flag","done"}`) → toggles a
  per-session mark in `flags.json`; returns `{ok, kind, value}`.
- `GET /api/search?q=<query>[&scope=default|deep][&project=<short>]` → full-text
  corpus search (`search_corpus`, gzip). Two-stage: a token-AND raw-byte prefilter
  (a superset — see `_query_needles`) skips files without parsing; only hits are
  parsed for snippets. Returns `{results:[{session, hit_count, hits:[{turn_index,
  role, ts, before, match, after, agent?}]}], hit_count, scanned, matched, errors,
  matched_sessions, truncated, query, scope}`. Sub-agent hits carry `agent=<id>`
  and group under the parent session; snippets deep-link to `/session?id=…#t<idx>`.
  Empty `q` → empty results (never a full dump). Design + invariants: `AI/search.md`.

The per-row "view" link (next to `⧉ resume`) opens `/session?id=` in a new tab;
it is suppressed in the `--once` static snapshot via the `const STATIC=false;`
build-time replace (no server → no route). The resume command is built by
`resume_cmd()` (shlex-quoted cwd) — the one shared builder for both pages.

## `--once` static mode

`write_static()` inlines a `collect()` snapshot into the page by replacing the
`fetch('api/sessions…')` line with `let j=<json>;`, and flips
`const STATIC=false;` → `true` (suppresses per-row "view" links — a `file://`
snapshot has no `/session` route), writing `index.html`. Keep the
`async function load(){` and `const STATIC=false;` lines in `PAGE`
byte-identical to the replace targets in `write_static()`, or inlining
silently no-ops.
