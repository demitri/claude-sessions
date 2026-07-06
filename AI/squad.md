# squad integration (design — not yet built)

Optional integration with [squad](https://github.com/mco-org/squad) (mco-org,
MIT, Rust, v0.7.6), a multi-agent messaging CLI: Claude/codex/gemini sessions
join a shared SQLite bus (`<workspace>/.squad/messages.db`) as *named agents*
and exchange messages and structured tasks. This doc designs the dashboard's
squad surface: view/search the inter-session transcript, task lifecycles, and
(phase 2) let the user participate from the browser.

Validated groundwork (2026-07-05, two-session live trial — see
`AI/session-ipc-research.md` §7): raw SQLite reads/writes are first-class
squad participants; delivery is a 500ms poll of the DB by
`squad receive --wait`; schema is 3 tables.

## Product stance

claude-sessions is the product; squad support is an **optional feature**,
invisible unless squad data exists. squad-ui is *not* a separate product (a
standalone viewer would rebuild this dashboard's skeleton and could never join
messages to session transcripts). The module boundary below keeps later
extraction cheap if outside demand appears.

## Hard requirements (user, 2026-07-05)

1. **Silent absence.** With no squad workspace present, the UI is byte-for-byte
   unchanged — no tab, no empty panel, no "squad not found" message.
   *Sanctioned exception to the global no-silent-skip rule* (like defensive
   JSONL parsing): this is a deliberate feature gate on **data presence**, not
   error suppression. The line is drawn precisely: `.squad/` absent → silently
   off; `.squad/` present but unreadable/unexpected → **loud** (see Schema
   guard). Never conflate the two.
2. **Stdlib only** — `sqlite3` is stdlib (FTS5 included). No new dependencies.
3. **Strictly read-only against squad's DB** in phase 1. Never touch the
   `read` flag (it is inbox-consumption state owned by recipients); never
   create tables in squad's file (our FTS/index state, if any, lives in our own
   sidecar under `~/.config/claude-sessions/`).

## Workspace discovery

No config. Mirror squad's own walk-up discovery, driven by data the dashboard
already has: for each session `cwd` in `collect()`, walk parent directories to
filesystem root looking for `.squad/messages.db`; dedupe the hits. This finds
exactly the buses those sessions can see, and aggregates multiple workspaces
natively.

**Workspace convention (user, 2026-07-05):** the canonical bus lives at
`$REPO/.squad/` — the directory *above* `$GH`, containing all repository
hosts (`~/Documents/Repositories` on Macs: GitHub/GitLab/BitBucket/…; on the
user's Linux boxes `$REPO` = `$GH` = `~/repositories`). Every repo is beneath
it, so walk-up discovery reaches the shared bus from any session in any repo.
The discovery mechanism above stays generic — the convention is where to
`squad init`, not a hardcoded path. Cache the per-cwd walk result
(cwd → workspace-or-None) with a TTL; a `--squad-db <path>` CLI override adds
an explicit workspace (repeatable) for edge cases (e.g. a bus none of the
scanned sessions' cwds sit under).

Multiple workspaces: agent ids are only unique per workspace, so everything is
keyed `(workspace, agent_id)`; the UI shows a workspace label (shortened like
`project_short()`) when >1 workspace is live.

## Reading the DB

- Open with URI: `sqlite3.connect("file:...?mode=ro", uri=True)`. The DB is
  WAL; concurrent readers are safe and never block agents.
- **Cache gotcha:** under WAL, writes land in `messages.db-wal` — the main
  file's `(mtime,size)` may not move until checkpoint. Cache key must combine
  main + `-wal` stat (`-wal` may not exist; treat missing as `(0,0)`).
- Timestamps are Unix epoch seconds (integers). Message `content` is free
  text; render as text, never HTML.

### Schema (squad 0.7.6 — the "protocol" is these tables)

- `agents(id, role, joined_at, session_token, last_seen, status
  'active'|'archived', archived_at, client_type, protocol_version)`
- `messages(id, from_agent, to_agent, content, created_at, read,
  kind 'note'|'task_assigned', task_id, reply_to)`
- `tasks(id uuid, title, body, created_by, assigned_to, status
  'queued'|'acked'|'completed', lease_owner, lease_expires_at,
  result_summary, created_at, updated_at, completed_at)`

`@all` broadcasts are sender-side fan-out: N rows with identical
`(from_agent, content, created_at)` — collapse them into one logical message
in the UI with a recipient list.

### Schema guard (fail loud, the right way)

squad is young (3 months, 14 releases); the schema is undocumented but has
been strictly additive so far. On first open per workspace, verify via
`PRAGMA table_info` that every column we *read* exists. Missing column /
unreadable DB → the workspace is shown in the UI as an **error state** (a
visible "squad workspace at X: unexpected schema (squad upgrade?)" banner on
the squad page and a warning glyph in the nav), and logged to stderr. Extra
unknown columns are fine (additive evolution). This is the loud side of
requirement 1's line.

## Backend surface

New module-boundary file section in `claude-status.py` (or a second file
`squad_data.py` imported unconditionally — import never fails, it's stdlib;
*data* presence is the gate). All squad code talks only to squad DBs + the
mapping sidecar; zero imports from JSONL-scanning internals. The join point is
`collect()` (annotating sessions) and the Handler routes.

Routes (exact-path dispatch, as everywhere):

- `GET /api/squad` → `{workspaces:[{path, error?, agents:[…], tasks:[…]}],
  messages:[…]}` with `?since=<epoch>` for incremental fetch. Omitted/empty
  when no workspace is discovered (and the page JS renders nothing).
- `GET /squad` → the squad page (third embedded SPA string, same pattern as
  `TRANSCRIPT_PAGE`). Direct navigation with no workspaces shows a plain
  "no squad workspaces found" body (reachable only by typing the URL — no
  link ever points here in that state, preserving silent absence).

Dashboard page changes (all conditional on `/api/sessions` reporting
`squad:true`, so zero DOM impact otherwise):

- A nav link/chip to `/squad`, rendered only when squad data exists.
- Per-session **agent chip** on rows whose session is a mapped squad agent
  (needs the mapping sidecar, below): small `@scout`-style chip in the name
  sub-row; unread-inbox count as a subtle badge (computed from `messages
  WHERE to_agent=… AND read=0` — read-only).

## The squad page

Three views over one fetch (tabs or stacked sections; decide at build time):

1. **Transcript** — chronological messages, broadcast-collapsed, threaded by
   `reply_to`/`task_id` (indent replies; task messages show a state chip).
   Filters: agent, workspace, kind, free text. Sender/recipient names link to
   the mapped session's `/session?id=…` transcript when a mapping exists —
   *this is the integration's unique value* (message ↔ what the session was
   actually doing).
2. **Tasks** — lifecycle board (queued / acked+lease / completed), showing
   `result_summary`, lease expiry countdown on acked tasks, creator→assignee.
   This also fixes squad's own gap: completion is not push-delivered to the
   creator; the dashboard is where completions become visible.
3. **Agents** — roster: id, role, client, active/archived, last_seen
   staleness (mirror `squad agents`' staleness notion), mapped session link.

Search across message content: v1 = client-side substring over the fetched
window (consistent with the dashboard's filter box). FTS5 sidecar index only
if/when volume demands it (own DB under `~/.config/claude-sessions/`, never
in squad's file).

Refresh: same 30s poll as the dashboard (`?since=` keeps it cheap). SSE is a
later nicety, not v1.

## Agent ↔ session mapping

Squad's `agents` table has no Claude session id, and nothing infers it safely
(cwd/process correlation is guessing — banned). Make it explicit at join
time: a wrapper/slash-command (ours, e.g. `commands/squad-join.md`, or a line
in the user's `.squad/roles/*.md` join instructions) records

    <workspace>/.squad/claude-sessions-map.json
    { "<agent_id>": {"session_id": "<$CLAUDE_CODE_SESSION_ID>",
                      "joined_at": <epoch>} }

written with the same flock + mkstemp + `os.replace` pattern as `flags.json`
(multiple sessions join concurrently). It lives in the workspace (the mapping
is per-bus and should die with the bus), is additive to squad's dir but never
touches squad's DB. Missing/partial mapping degrades gracefully: names render
without links (silent — absence of optional enrichment, not an error).
Non-Claude agents (codex/gemini) simply have no mapping; their `client_type`
shows in the roster.

## Phase 2 — participation (write path)

A send box on the squad page: `POST /api/squad/send {workspace, to, content,
reply_to?}` INSERTs into `messages` as agent `demitri` (registering the agent
row on first send, status active; id configurable via `--squad-as`). Raw
INSERT is validated first-class (trial above); alternatively shell out to
`squad send` if the binary exists — decide at build time, but the INSERT path
keeps the no-binary-required property. Writes are the one exception to
requirement 3 and are scoped to: INSERT into `messages`, INSERT/heartbeat own
row in `agents`. Still never UPDATE other rows.

## Phase 3 — mobile: PWA + Pushover deep links (decided 2026-07-05, post-v1)

**Decision:** the mobile reply channel is the squad page itself, mobile-first
as a PWA, with Pushover as the doorbell — *not* a chat platform. Explicitly
not v1.0; build after phases 1–2 prove out.

- Squad page gets a manifest + mobile-first layout (add-to-home-screen,
  thread view usable on a phone). Reply = the phase-2 send box.
- Server pushes via the user's `pushover` script (globally allowlisted) on
  `@demitri` mentions and task completions, **with `-u <deep link>`** to the
  specific thread (`/squad?ws=…&thread=…`) — tap → reply box. One tap from
  notification to participation; the "Pushover can't reply" problem is solved
  by making the notification a door, not the conversation.
- Opt-in via CLI flag, off by default (per the user's explicit-request-only
  notification policy). Requires the dashboard to be reachable from the phone
  (user's homelab/VPN concern, not this project's).
- Considered and set aside: self-hosted ntfy (iOS instant delivery relays
  through ntfy.sh upstream — partial cloud dependency), Gotify (Android-only),
  Matrix/XMPP/Delta Chat bridge bots (real chat-app UX; **the fallback if the
  PWA experiment proves insufficient** — squad stays the bus, chat is only the
  human edge, ~100-line bridge bot).

## Risks / open questions

- **Schema drift** is the standing risk (young project, no stability
  promise). The guard converts drift into a visible error, not wrong data.
  Pin expectations to squad 0.7.6 columns; revisit on squad upgrades.
- Message volume: unbounded `messages` growth → `?since=` + LIMIT window;
  squad has no retention story yet.
- `--once` static snapshot: squad panel inlines like sessions do, or is
  simply omitted — decide at build time (snapshot has no POST anyway).
- Naming: dashboard nav label ("Squad"? "Comms"?) — user's call at build.

## Status

- 2026-07-05: design written (this doc). Not reviewed, not built. Per review
  policy: needs codex → sonnet (→ opus if findings) rounds to dry before
  implementation.
- Dev fixture: `~/tmp/.squad/` holds the validation trial's real data (agents
  incl. a raw-SQL `webapp` participant, a broadcast, a full
  queued→acked→completed task lifecycle) — ready-made test workspace for
  building v1. The canonical (empty until first join) bus is `$REPO/.squad/`.
