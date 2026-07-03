# remote sessions (design)

**Status: design, not built.** This is the reviewable spec for aggregating
Claude Code sessions from other servers into one dashboard. Nothing here is
implemented yet — red-pen it before code moves. (Round 1 of review applied:
codex.)

## Goal

Today the dashboard shows only *this* machine's sessions (`~/.claude/projects`
+ live-process detection on the local box). The goal is to see sessions from a
few other named servers you work on constantly, in the same dashboard, and —
critically — to **reopen a server-side session** after your local end drops (a
closed terminal / dropped SSH breaks the session on your side, but the session
itself lives on the server as a resumable `.jsonl`).

Non-goals: a fleet manager for dozens of dynamic hosts; a central store of all
history; running anything heavyweight on the remotes.

## The seam that makes this cheap

`collect()` already does *all* the machine-local work — parse `.jsonl`, detect
live processes, read RSS — and everything downstream (`/api/sessions`, the SPA)
just consumes its JSON. So "remote sessions" reduces to: **run `collect()` on
each server, tag each session with a `host`, merge the lists in one hub.**

Three things are inherently machine-local and *must* be computed where the
session lives — the hub cannot derive them remotely:

- **Liveness** — `open_sessions()` uses `os.kill` + reads `~/.claude/sessions/*.json`.
- **RAM** — `ps -o rss` against live PIDs.
- **`resume`** — `cd "<dir>" && claude --resume <id>` only works on that box.

This rules out the naive "rsync every remote's `~/.claude/projects` to the hub
and parse centrally" approach: it loses open/busy state and RAM, and its resume
commands point at directories that don't exist locally. **The parser has to run
on the remote.**

## Transport: SSH-pipe `--emit` (open / read / close)

The hub polls each host with a short-lived, self-contained SSH exec that
streams the script over stdin:

```python
subprocess.run(
    ["ssh", "-C",                            # -C: gzip; measured 165KB→36KB payload
     "-o", "BatchMode=yes", "-o", "NumberOfPasswordPrompts=0",
     "-o", "ConnectTimeout=8",
     *shlex.split(host["ssh"]),          # options BEFORE destination
     host.get("python", "python3"), "-", "--emit"],   # after destination = remote command
    input=SCRIPT_SOURCE, capture_output=True, text=True, timeout=REMOTE_TIMEOUT)
```

- **Nothing is deployed on the remote** and there is no version skew — the
  remote runs the *hub's own* source, read from `__file__` once at startup.
- No ports are opened; reuses your existing SSH auth.
- **Never `shell=True`.** The `ssh` target may contain a user@host and options;
  it is `shlex.split` into argv, and `--emit` output is parsed as JSON. See
  "Command construction & quoting".
- **SSH hardening is mandatory, not optional** (this is the defence against your
  flaky connections *and* against a silent hang): `BatchMode=yes` +
  `NumberOfPasswordPrompts=0` make a host that would prompt for a
  password/passphrase fail fast instead of blocking the poll; `ConnectTimeout`
  bounds the TCP/handshake wait; the subprocess `timeout` bounds the whole poll.
  A first-connect unknown-host-key prompt also fails fast under `BatchMode` —
  surfaced as a host error telling you to `ssh` in once manually to accept the
  key.

Why this transport (decision, given flaky SSH): each poll is independent —
open, read stdout, close. A connection that dies mid-poll costs **one stale
cycle** for that host and self-heals on the next tick; there is no long-lived
tunnel to babysit. A remote-HTTP-over-tunnel design would break on the same
flakiness and stay broken until re-established — strictly worse here. (The
file-sync design is rejected above.)

### Remote parse cost — the one-shot cache problem

`parse_session`'s `_CACHE` is an **in-process** dict. A one-shot
`python3 - --emit` starts cold every poll, so it **reparses every remote
`.jsonl` on every fetch** — the existing `(mtime,size)` cache does not survive
across processes. For a ~30-day corpus of a few hundred sessions this is
seconds of remote CPU per poll, which the adaptive polling below mostly hides,
but it must not be hand-waved. Two mitigations, both in scope:

- **On-disk parse cache for `--emit`** (required for v1): persist the
  **parse-layer fields only** to `~/.cache/claude-sessions/parse-cache.json` on
  the remote, keyed by `path → (mtime, size, parsed)`. **Caveat, must fix
  first:** today `parse_session` caches the `result` dict *by reference*
  ([claude-status.py:229](../claude-status.py)) and `collect()` then mutates
  that same object in place with transient fields — `open`, `live_status`,
  `rss_kb`, `flagged`, `done` ([claude-status.py:339](../claude-status.py)). So
  the in-memory cached dict already accumulates non-parse state (benign today
  since it's overwritten each request, but it would poison a serialized cache).
  Fix as part of this work: have `parse_session` **return a copy** (or have
  `collect()` copy before stamping) so the cached object holds only parse-layer
  fields, and serialize *that* — never the per-request `open`/`rss_kb`/flag
  fields. The file also carries a **cache-format version** tag: the hub streams
  its own parser to the remote (no *code* skew), but the remote's *disk* cache
  outlives hub versions, so a newer parse-layer model must invalidate an old
  cache. On a version mismatch (or unreadable version) the whole cache is
  ignored and rebuilt, so an unchanged JSONL never serves a stale shape. A cold `--emit` loads it, reuses unchanged
  files, reparses only what changed, and rewrites it. Bounded by the same
  rolling retention window as the corpus; prune entries whose file is gone.
  **Failure-safety is required, not optional** — the cache is a performance
  aid and must never turn into a host failure: load is best-effort (a
  missing/corrupt/partially-written file → treat as empty and rebuild),
  malformed individual entries are ignored, writes go through a temp file +
  `os.replace` (same atomic pattern as `save_flags`) so a killed or concurrent
  `--emit` can't leave a torn file, and **any** cache read/write error is
  swallowed *for the cache only* (fall back to live parse) — never propagated
  out of `--emit`. (This is a sanctioned local performance cache, not the
  session data itself, so degrading to a full reparse is the correct fallback.)
  **Cold-parse-vs-timeout trap (must design around, not assume away):** the
  self-heal claim ("one stale cycle, then recovers") holds *only if the parse
  finishes and writes the cache*. A cold `--emit` reparses the whole corpus; if
  that cold parse exceeds `REMOTE_TIMEOUT`, the hub kills the subprocess before
  it writes the cache — so the next poll is *also* cold and *also* times out,
  **forever**, and a healthy host shows permanently red. Two aggravators: the
  corpus size is external and unbounded up to the ~30-day retention window (the
  hub doesn't control it), and a **hub-version bump invalidates every remote's
  disk cache at once** (cache-format version change above) → a *synchronized
  cold-parse storm* across all hosts on the first post-upgrade poll. Mitigations
  (v1 must adopt at least the first): **(a)** write the cache *incrementally* —
  flush every N files (temp+`os.replace`) so a killed cold parse still makes
  forward progress and the next poll is warmer, converging instead of looping;
  **(b)** size `REMOTE_TIMEOUT` against worst-case *cold* parse, not steady
  state; **(c)** consider a longer first-contact timeout, or emit a partial
  result + "still warming" host status rather than a hard timeout. This makes
  the "Numbers — right values?" open question load-bearing for `REMOTE_TIMEOUT`
  specifically.
- **Persistent per-server dashboard** (for servers you keep it running on
  anyway — see roles): its in-memory `_CACHE` stays warm *for requests to that
  running process*. Note a separate `python3 - --emit` subprocess **cannot**
  share that memory — only polling the persistent process's `/api/sessions`
  (over a port-forward) is warm. That is the rejected-for-v1 HTTP transport, so
  it's an *optional future* per-host mode (`"mode": "http"` in the host config),
  not the v1 path. For v1 the on-disk cache above is what makes SSH-piped
  `--emit` cheap regardless of whether a persistent dashboard is also running.

Cost otherwise: one `ssh` + `python3` per host per poll — trivial for a few
hosts. Polls run **one thread per host** with the timeout above so a slow/dead
host never stalls the page.

Each poll also **streams the whole ~36 KB script over stdin** (that's the price
of the zero-deploy, no-version-skew property). This is deliberate and accepted:
36 KB is negligible bandwidth even on a poor link, and the flaky-SSH risk this
design guards against is *connection setup/teardown reliability*, not payload
size — which the open/read/close + retention model already handles. If a host is
ever bandwidth-constrained enough for this to matter, the escape hatch is the
optional persistent per-server dashboard (deploy the script once, poll its API)
— explicitly a future mode, not v1.

### Command construction & quoting

Two shell layers must be quoted correctly or a `cwd`/id with a space, quote, or
`$` breaks the command (or worse). Rules:

- **Polling** never goes through a shell (argv vector above) — no quoting needed
  there.
- **Copyable resume strings** are shell text the *user* pastes, so they must be
  quoted with `shlex.quote`:
  - Local (today, [claude-status.py:207](../claude-status.py)) interpolates
    `cwd` raw into `cd "%s" && …`; migrate it to
    `f'cd {shlex.quote(cwd)} && claude --resume {shlex.quote(sid)}'` for
    correctness on odd paths.
  - Remote wraps that inner command as a single argument to `ssh -t`. The
    `ssh` target may itself be multi-token (e.g. `-p 2222 user@host`), so it is
    **`shlex.split` then each token quoted individually** — never quoted as one
    word (that would collapse `-p 2222 user@host` into a single bogus
    destination):

    ```python
    dest = " ".join(shlex.quote(t) for t in shlex.split(host_ssh))
    remote = f"ssh -t {dest} {shlex.quote(inner)}"
    ```

    so the inner `cd … && claude --resume …` survives one layer of shell
    unwrapping on the remote intact.

Note this is a **copy-to-clipboard** workflow (browser security — the page can't
launch a terminal), not a literal one-click launch. The value is that the exact,
correct reconnect command is one paste away.

## Roles of the one file

Same `claude-status.py`, three roles — **no new software to maintain**:

1. **`--emit`** — `print(json.dumps(collect()))` and exit. Used by the hub's
   SSH pipe. Its `resume` strings are the remote's own local form; the hub
   rewrites them (see merge). Unlike the local server, `--emit` is **soft on a
   missing `~/.claude/projects`**: it prints the normal minimal shape
   `{"generated": <now>, "sessions": []}` (a host with no sessions is *ok*, not
   a failed host), rather than exiting non-zero the way `main()` does today for
   local UI startup.
2. **Hub** (your laptop) — normal server mode, plus it reads a hosts config and
   merges each host's `--emit` output into the local view.
3. **Per-server dashboard** (optional, for servers you live in) — the *same*
   script run persistently on the remote so you can browse it directly over a
   quick `ssh -L` port-forward and flag/reopen sessions locally on that box when
   your laptop end has dropped. Surviving login is ~10 lines of
   systemd/launchd — this generalizes the existing "Auto-start on login" TODO
   from the laptop to those hosts. A sample unit ships with the implementation.

**Remote Python requirement:** `collect()` uses `subprocess.run(...,
capture_output=True, text=True)`, which is **Python 3.7+**. So "the remote needs
python3" means 3.7 or newer on its `PATH`, or a per-host `python` key in the
hosts config pointing at a suitable interpreter (see Hosts config).

## Hosts config

`~/.config/claude-sessions/hosts.json` — a small static list (a few named
hosts):

```json
[
  {"name": "bistromath", "ssh": "bistromath"},
  {"name": "gpu-box",    "ssh": "demitri@gpu.example.net", "resume_wrapper": "tmux new -As claude-{id} {cmd}"}
]
```

- `name` — short label shown in the Host column / filter and used in the
  composite session key (see identity). Validated at load: must match
  `[A-Za-z0-9_.-]+` (so it can't contain the `:` composite-key delimiter), must
  be unique **including the implicit `local`**, and **may not be `local`**
  (reserved for this machine). Any violation errors loudly at startup.
- `ssh` — whatever you'd type after `ssh ` (an alias from `~/.ssh/config` is
  ideal).
- `python` *(optional, per host)* — path to a Python ≥ 3.7 interpreter on the
  remote when it isn't the default `python3` on `PATH` (e.g. `/usr/bin/python3`
  or a pyenv shim). The hub substitutes it for `python3` in the poll argv.
  Omitted → `python3`.
- `resume_wrapper` *(optional)* — a **template** that wraps the resume command
  so a *re*-dropped SSH doesn't kill the resumed session again (directly serves
  the "my connections close unexpectedly" goal). It **must contain the `{cmd}`
  placeholder**; the hub substitutes a shell-quoted `claude --resume <id>` there
  (a prefix contract is wrong — `tmux new -As claude` prefixed would yield
  `tmux new -As claude claude --resume …`). A wrapper string lacking `{cmd}` is
  rejected loudly at config load. Two more placeholders are exposed so the
  wrapper can be **per-session** — essential for `tmux`/`screen`, where a static
  session name makes every resume attach to the *same* session instead of
  running the requested one: `{id}` (full session UUID) and `{idtail}` (last 4
  of the id, matching the dashboard's `#tail` matcher). Prefer `{id}` for
  uniqueness — example: `"tmux new -As claude-{id} {cmd}"` — so each session
  gets its own tmux and reconnecting with the same id re-attaches to the right
  one. `{idtail}` is available for shorter, human-readable names but **can
  collide** across enough sessions (only 4 hex chars), so use it only when you
  accept that risk. Omitted → plain resume.

**Absent-config behavior** (the two cases, disambiguated):

- **No hosts file, or a valid-but-empty list → local only, silently.** This is
  the expected default for the common single-machine user; a warning here would
  be noise. The data shape is unchanged (always-on invariant below — one host,
  `local`).
- **A hosts file that exists but is malformed/unparseable → local only, with a
  loud one-line warning** naming the file and the parse error. This is a
  configuration mistake, not the default, so it must not be a silent skip.

## Merge semantics

**Always-on invariant (no dual mode).** There is a *single* code path and a
*single* API/flag shape whether or not any remotes are configured. Local
sessions are **always** stamped `host: "local"`, `key: "local:<id>"`,
`stale: false`; `collect_all()` **always** runs (with just `local` when no hosts
exist); `/api/sessions` **always** returns the extended shape
(`sessions` + `hosts:[{name:"local", …}]`); `POST /api/flag` **always** takes
`key` and **400s a bare-`id` body** (the bare-`id` → `local:<id>` mapping is a
one-time *on-disk* migration in `load_flags`, never a live request path). The
only difference
with zero remotes is cosmetic — the UI **hides** the Host column/filter and the
host-status strip when the merged set contains only `local`. This avoids two
frontend/API modes and an unclear fallback.

- **Remote `--emit` output is untrusted input to the hub.** Even though the
  threat model is "your own boxes," the hub parses remote stdout and threads
  remote-provided `cwd`/`id`/`preview` into shell strings (resume) and the
  rendered page — so it applies the same posture already used for the local
  session format: defensive JSON parsing (skip malformed, never crash the
  merge), `shlex.quote` on every interpolation into a command, and `esc()` on
  every interpolation into HTML. This is a one-line policy statement, not new
  machinery — the existing `esc()`/quoting already cover it — but it must be
  stated so it isn't quietly dropped.
- Local sessions get `host = "local"`; their `resume` is unchanged (modulo the
  quoting fix).
- Remote sessions get `host = <name>`, and their `resume` is **rewritten by the
  hub** (only the hub knows the origin host) to reconnect over SSH, honoring
  `resume_wrapper` when set. The inner command is `cd <cwd> && <resumecmd>`,
  where `resumecmd` is `claude --resume <id>` — or, if `resume_wrapper` is set,
  that wrapper with `{cmd}` replaced by a shell-quoted `claude --resume <id>`:

  ```
  # no wrapper:
  ssh -t <ssh tokens…> 'cd <cwd> && claude --resume <id>'
  # resume_wrapper = "tmux new -As claude-{id} {cmd}":
  ssh -t <ssh tokens…> 'cd <cwd> && tmux new -As claude-<id> '\''claude --resume <id>'\'''
  ```

  (all interpolations `shlex.quote`d per "Command construction & quoting").

- Timestamps are already UTC epoch (`calendar.timegm`), host-independent — they
  merge and sort cleanly with no timezone work.
- `open`, `live_status`, `rss_kb` come from the **remote's own** `collect()`
  (its process states), so remote liveness/RAM are accurate *when fresh* (see
  staleness).
- Merged list is re-sorted by `updated_ts`, exactly as today.

### Reopen semantics: conditional on remote liveness (not one uniform command)

The feature's headline goal is reopening a *dropped* server session — one whose
local end died leaving a resumable `.jsonl`. But `claude --resume <id>` is only
correct when the server-side process is actually **gone**. The hub already knows
which it is: the remote's own `collect()` reports `open`/`live_status`. So the
resume affordance must branch on it, rather than emitting one uniform
`claude --resume` for every remote row:

The branch keys on the **raw last-known `open`**, deliberately **not**
`effective_open` (see the explicit carve-out in the staleness section). This is a
stated exception to the "use `effective_open` everywhere" rule, and it matters:

- **Closed remote session** (`open` false, fresh) — the assumed
  dropped-and-killed case: offer the `ssh -t … 'cd <cwd> && claude --resume
  <id>'` reconnect above. This is the reopen path.
- **Still-open remote session** (`open` true, fresh): a plain `claude --resume
  <id>` would spawn a *second* process against a live session and conflict. So
  the hub does **not** offer a bare resume here. Options (pick at build): (a)
  suppress/grey the resume affordance for open rows with a "already live on
  <host>" tooltip; (b) if — and only if — `resume_wrapper` is a *reattach* form
  (`tmux attach`/`tmux new -A`) **and** the session was originally launched under
  that same wrapper, offer an attach. The dashboard cannot retroactively impose
  a tmux naming convention on a session it did not start, so (a) is the safe
  default and (b) is opt-in via `resume_wrapper`. **Caveat on (b):** the hub
  cannot verify the session was actually launched under the wrapper — if you opt
  into (b) but an open session was started *without* it, the attach form
  (`tmux new -A -s claude-<id> …`) creates a *fresh, conflicting* session,
  exactly the harm (a) avoids. Opting into (b) means accepting that you launch
  the relevant sessions under the wrapper.
- **Stale remote session** (host unreachable, last-good served): liveness is
  **unknown** — the poll that would tell you `open` is the one that failed. Do
  **not** confidently offer a bare resume (it would double-attach if the session
  is in fact still live), and you can't reach the host to run it anyway. Treat
  the reopen affordance as unavailable/greyed with a "host unreachable" note
  until a successful poll resolves the true state. This is precisely why the
  branch must read raw `open` *and* be stale-aware, not just `effective_open`
  (which would force stale→closed→"offer bare resume", the unsafe reading).

**Load-bearing external assumption to pin (per "known-quantities-never-guessed"):**
this design assumes a dropped SSH/terminal *kills* the local `claude` process,
leaving a jsonl-only, safely-resumable session. That is the premise the whole
reopen path rests on, and it is `claude --resume`'s behavior against a live vs.
dead session — an external quantity. **Verify empirically before implementing**
(does a SIGHUP'd session leave `open` false on the remote? does `--resume`
against a live session error, or double-attach?), and let the observed behavior,
not this assumption, drive the branch above. The "is `tmux` the right resilience
answer" open question *is* this question — it is load-bearing, not a free knob.

### Identity: composite `host:id` key

Session ids are UUIDs, so collisions are unlikely — but the flag store, the
client's `DATA.find(x => x.id === id)`, and stale-cache bookkeeping all key on
`id` today, and a wrong-host match is a real bug class once two hosts are in
play (mirrored home dirs, copied transcripts). So each session carries **both**
`host` and `id`, and everything that must be unique across the merged set keys
on the **composite `host + ":" + id`** (a `key` field). Flags, client lookups,
and stale marking all use `key`; `id` remains the raw UUID for the resume
command and for search.

### Flags are centralized on the hub (authoritative)

Flags (`flag` / `done`) live only in the **hub's** `flags.json`, now keyed by
the **composite `key`** (`host:id`), and are (re)applied by the hub to *every*
session — local and remote — overriding whatever flag state a remote's `--emit`
reported. So flagging a remote session from the laptop works, and the hub is the
single source of truth for the aggregate view. `POST /api/flag` takes `key`.

**The frontend must send `key`, not `id` — this is not optional.** The
bare-`id` acceptance is *only* a one-time migration for pre-existing entries in
`flags.json` on disk (read as `local:<id>`); it is **not** a live fallback for
the running UI. If the current buttons are left as-is
([claude-status.py:593,595](../claude-status.py) emit `data-id`;
`toggleFlag`/`DATA.find(x=>x.id===id)` at
[claude-status.py:640-641](../claude-status.py); the POST body `{id,kind,value}`
at [claude-status.py:645](../claude-status.py)), then flagging a *remote*
session would send a bare UUID and — under the required server rule below — get
a **400**, so remote flagging is visibly broken rather than silently wrong. (Had
the server instead mapped bare `id` → `local:<id>` at request time, it would
write a *phantom* `local:<uuid>` entry — not session-hijacking, but a silent
mis-write — which is exactly why the request path rejects bare `id`.) Either way
the stated "flagging a remote session from the laptop works" goal requires the
frontend to send `key`. So the frontend changes are **mandatory** and enumerated in
Touch Points: buttons carry `data-key`, `toggleFlag(key,kind)` looks up by
`DATA.find(x => x.key === key)`, and the POST body is `{key, kind, value}`. The
single, unambiguous server rule: **`POST /api/flag` requires `key` and returns
400 on a bare-`id` body** — it never silently maps `id` → `local:<id>` at
request time. The only place bare `id` is tolerated is `load_flags`, which
rewrites *pre-existing on-disk* entries to `local:<id>` at startup. So a stray
or stale client that sends a bare UUID fails loudly (400) instead of writing a
phantom flag — consistent with no-silent-misattribution.

Accepted v1 divergence (conscious product call, not an oversight): a per-server
dashboard keeps its *own* `flags.json`, so a flag set on the hub is not
reflected in that server's standalone dashboard (and vice-versa). Note this
divergence bites *precisely* the reopen scenario this feature targets — if you
flag a session on the per-server dashboard at the moment your laptop drops, that
flag is invisible on the hub once you're back, with no cross-reference in either
UI. The deferral is nonetheless defensible because **the core loop works without
the per-server role at all**: flag centrally on the hub + hub-rewritten resume
covers reopen end-to-end; the per-server dashboard is a convenience for
browsing/flagging *on the box* during a disconnect, not a dependency. Syncing
them (hub pushes flag writes to the remote over SSH) is a deliberate
**follow-up**, not v1. See "Open questions."

### Unreachable host — loud, never silent, never a fake session

A host's poll can fail (SSH error, timeout, non-zero exit, unparseable output).
This must be **loud** (global no-silent-skip rule) but **must not be encoded as
a fake session row** — that would pollute stats, filters, sorting, and grouping.
Instead the API grows a separate `hosts` block, and the UI renders a host-status
strip from it.

### API schema (`GET /api/sessions`)

Extended from today's `{generated, sessions}` to:

```jsonc
{
  "generated": 1751000000,
  "sessions": [ /* …, each now with host, key, and a `stale` bool */ ],
  "hosts": [
    {"name": "local",      "ok": true,  "stale": false, "age_s": 0,   "error": null},
    {"name": "bistromath", "ok": true,  "stale": false, "age_s": 3,   "error": null},
    {"name": "gpu-box",    "ok": false, "stale": true,  "age_s": 210, "error": "ConnectTimeout"}
  ]
}
```

- `hosts[]` drives a host-status strip (a badge per host: green ok, grey stale,
  red error-with-reason). A host that has never succeeded shows red with no
  sessions; a host that *had* data but is now failing shows its last-good
  sessions **marked `stale`** plus a red badge.
- The frontend reads `hosts` in addition to `sessions`; today it only reads
  `generated`/`sessions`, so this is an additive change.

### Stale data must not masquerade as live

When a host's last-good sessions are shown after a failed poll, each is flagged
`stale: true`. Staleness must gate **every** live-state derivation, not just the
aggregate cards, or a stale `open: true` will still render a green/busy dot and
pass the `Open` filter. The rule is a single derived predicate used everywhere:

```
effective_open = !s.stale && s.open
```

- The **open-state dot** on a stale row shows an "unknown/stale" state (a
  distinct greyed marker), never green/pulsing, regardless of the cached `open`.
- The **`Open` filter** and any live/busy logic test `effective_open`, not
  `open`.
- **Aggregate counts** in `cards2()` (open, live, RAM · open) sum on
  `effective_open` / non-stale `rss_kb` — an unreachable host must not inflate
  them with old process state.
- The row is also **rendered visually distinct** (dimmed + a stale marker).
- (Historical counts like total sessions / on-disk size may still include stale
  rows, labeled.)

**One deliberate exception to "everywhere":** the *reopen affordance* (see
"Reopen semantics") must **not** be derived from `effective_open`. `effective_open`
collapses stale→closed, which is the right call for *display* (don't show a green
dot you can't trust) but the *wrong* call for *action*: treating a stale session
as closed would offer a bare `claude --resume` that double-attaches if it's
actually still live. So the reopen branch reads raw `open` and treats `stale`
as its own third state (affordance unavailable). Everything *display*-related
still uses `effective_open`; only the resume *action* is carved out. This carve-
out is stated in both places on purpose so neither rule silently overrides the
other.

## Adaptive polling (client-driven)

Fixed 30 s polling is replaced by a **self-rescheduling timer whose delay is a
function of idle time** — this is the real throttle on remote SSH cost.

- **Active** (interacted within the last 10 s): poll every **10 s**.
- **Visible but idle**: exponential decay, ×2 per step — 10 → 20 → 40 → 80 →
  160 → **300 s ceiling**.
- **Tab hidden**: **hard pause** — `clearTimeout`, schedule nothing, zero
  remote SSH while the tab isn't in front of you. (Decision: full pause, not a
  slow heartbeat.)
- **On focus / `visibilitychange`→visible**: immediate `load()`, then restart
  the timer at the 10 s active cadence and re-decay from there.
- **Any interaction resets to active**: `click`, `keydown`, `input` (search),
  `scroll`, throttled `mousemove`, `focus`. The first three = "actively using";
  mousemove/focus = "sitting there"; nothing firing = "walked away" → decays.
  Existing buttons currently call `render()`, not `load()`; they'll route
  through the activity bump too, so any button press also kicks a fresh remote
  fetch, as requested.

Sketch:

```js
let lastActivity = Date.now(), timer = null, loadInFlight = false;
const ACTIVE = 10000, CEILING = 300000;
function nextDelay(){
  const idle = Date.now() - lastActivity;
  if (idle < ACTIVE) return ACTIVE;
  const steps = Math.floor(Math.log2(idle / ACTIVE)) + 1;   // ×2 per step
  return Math.min(ACTIVE * 2 ** steps, CEILING);
}
function schedule(){ clearTimeout(timer); if (document.hidden) return;   // hard pause
  timer = setTimeout(async () => { await load(); schedule(); }, nextDelay()); }
async function load(){ if (loadInFlight) return; loadInFlight = true;    // drop overlaps
  try { /* fetch + render */ } finally { loadInFlight = false; } }
function bump(){ const wasIdle = Date.now() - lastActivity > ACTIVE;
  lastActivity = Date.now(); if (wasIdle) load(); schedule(); }
// On regaining visibility, reset lastActivity FIRST — else nextDelay() measures
// against the stale pre-hidden timestamp and the next tick lands at the ceiling
// instead of the promised 10s active cadence. Note this loads UNCONDITIONALLY
// (not via bump(), which only loads if wasIdle) — spec wants an immediate
// refresh on every focus, even a brief hide.
document.addEventListener('visibilitychange', () => {
  if (document.hidden) { clearTimeout(timer); return; }   // hard pause
  lastActivity = Date.now(); load(); schedule();          // back → active cadence
});
```

### Server-side coalescing (cache + in-flight suppression)

"Every interaction refreshes" plus a `ThreadingHTTPServer` (multiple tabs,
overlapping requests) could fire a burst of SSH fan-outs.

The per-host cache entry has **two independent lifetimes** that must not be
conflated (they were, in an earlier draft, which left the stale fallback with
nothing to fall back to). One entry per host: `{payload, ok, fetched_at}`.

- **Freshness / rate-limit (the ~5 s number)** governs only *whether to attempt
  a fresh poll*: if the last **successful** fetch is younger than ~5 s, serve
  the cached payload without re-SSHing. This is a coalescing throttle, **not an
  eviction TTL** — nothing is deleted when 5 s elapses; it just becomes eligible
  for a refresh on the next request.
- **Last-known-good retention** is indefinite: a host's last *successful*
  `payload` is **kept until replaced by a newer successful poll**, however long
  that takes. On a **failed** poll the entry is retained, `ok` flips false, and
  its sessions are served **marked `stale`** with `age_s = now − fetched_at` (of
  the last good fetch). **`fetched_at` is a hub-side receipt timestamp** (when
  the hub got the payload), *not* the remote's self-reported `generated` epoch —
  using the remote's clock here would reintroduce the cross-host skew the design
  otherwise avoids. So `age_s` is computed entirely against the hub clock. This is what powers the `hosts[]` example where a host
  210 s stale still shows its last-good sessions. Only if there has *never* been
  a successful poll is there no payload (host shows red, zero sessions).
- **In-flight suppression**: a per-host lock/future so concurrent requests
  *share one* running SSH process instead of each spawning their own; late
  callers await the same result. (The frontend `loadInFlight` guard above is the
  client-side complement.)

Local file scanning stays uncached-cheap, as today. This per-host cache is
distinct from the per-file `(mtime,size)` parse cache (unchanged) and from the
new on-disk `--emit` cache (a *remote* concern).

## Frontend — host-aware everywhere

With multiple servers, project identity alone collapses (`claude-sessions` on
two hosts would merge visually). So host is threaded through the UI:

- **Host column** + a host filter chip.
- **Search haystack** includes `host`.
- **Grouping / shortcut chips / project dropdown** key on `host / project`, not
  bare project — or display the host as a prefix/badge so same-named repos on
  different hosts stay distinct.
- **Host-status strip** from `hosts[]` (see API): per-host ok/stale/error badge.
- Stale sessions dimmed + marked; excluded from live/RAM cards.

## Implementation order

1. **Data path + the flag-key contract (one atomic increment)** — `--emit`
   (soft-on-missing, on-disk parse cache); hosts-config load (+ unique-name
   validation); hub merge (host stamp, composite `key`, resume rewrite w/
   quoting + `resume_wrapper` + open-conditional affordance, flag reapply);
   remote fetch (per-host thread + SSH hardening + timeout + freshness cache +
   in-flight suppression); extended API (`sessions` w/ `host`/`key`/`stale`,
   `hosts[]`). **Critically, the composite-`key` flag path is a single contract
   that spans server AND frontend and must land here, together:** the moment the
   server requires `key` and 400s a bare-`id` body, the *existing* frontend
   (which sends `{id,…}` with `data-id` buttons) starts getting 400s on **every**
   flag toggle, local included — so `data-key`, `toggleFlag(key,kind)`,
   `DATA.find(x=>x.key===key)`, and the `{key,…}` POST body ship in this step,
   not in step 3. Deferring them would regress working local flagging mid-plan.
   **The loudness/staleness *rendering* contract is likewise atomic with the
   data that feeds it** — same "an increment must be internally coherent" logic
   as the flag path. Step 1 emits `stale` + `hosts[]`, so step 1 must also ship
   what makes them loud: the **host-status strip** (dead/stale badge), **stale
   row styling**, and **`effective_open` + `cards2()` aggregate exclusion** — plus
   the **frontend half of the open-conditional resume** (suppress/grey the copy
   button for open/stale rows; the current `rowHtml` always emits the copy button
   at [claude-status.py:590](../claude-status.py), so a backend that merely nulls
   `resume` would ship a copy-nothing button). Without these in step 1, a failed
   host is *silently* wrong — `cards2()` still sums raw `open`/`rss_kb`
   ([claude-status.py:686-690](../claude-status.py)), stale rows show a live dot,
   no error badge — violating both step 1's own "shows loud" goal and the global
   no-silent-skip rule.
   Goal: remote sessions appear, a dead host **shows loud** (badge + stale
   styling + de-inflated cards), the reopen affordance is liveness-correct, and
   flagging (local + remote) works — a genuinely working, reviewable increment.
2. **Adaptive polling** — activity tracking, decay, hard-pause-on-hidden,
   refresh-on-focus, `loadInFlight`; route existing controls through the bump.
   (Separable from remote — coupled only via cost; see the sequencing note
   below. Server-side coalescing in step 1 is the actual cost backstop, so this
   step can slip without blocking remote value.)
3. **UI host-awareness (multi-host disambiguation)** — Host column/filter,
   host-in-search, host-aware grouping/chips. *Not* cosmetic: without it, two
   same-named repos on different hosts are visually indistinguishable in the
   rows, search can't filter by host, and grouping merges them (the "identity
   collapses" problem from the Frontend section). It's deferrable *after* core
   value ships, but it's the difference between a usable and an ambiguous
   multi-host view. (The flag-path migration AND the loudness/staleness rendering
   + conditional-resume suppression are *not* here — they moved to step 1 as part
   of coherent, non-regressing increments.)
4. **Docs + ops** — keep this file current; ship a sample launchd/systemd unit
   for the per-server dashboards; document the `python3 ≥ 3.7`-on-remote
   requirement and the one-time `ssh` host-key acceptance.

**Sequencing note (thinner-v1 option).** Step 2 (adaptive polling) is the
riskiest single frontend change and is coupled to remote *only by cost* — and
step 1's server-side coalescing (~5 s freshness) is already a sufficient cost
backstop for "a few hosts." So a leaner first ship is viable: **step 1 alone +
a fixed longer poll interval** (e.g. bump the existing 30 s up) already delivers
the core value — remote visibility, loud dead-host status, and liveness-correct
reopen (step 1 now carries the loudness/staleness rendering). **Honest tradeoff:**
step-1-alone ships that value but with an *ambiguous multi-host view* — same-named
repos on different hosts aren't disambiguated in the rows, and you can't
search/group by host — until step 3 adds the host column/filter/grouping. So the
thinner v1 is "remote sessions visible and reopenable, host disambiguation to
follow," not "feature-complete minus polish." Step 2 (adaptive polling) is a
genuinely clean follow-up (server coalescing is the cost backstop). Decide the
sequencing knowing step 3 is real multi-host usability, not cosmetics. Adaptive polling stays in scope (the user specifically wants the
activity-decay + hard-pause-on-hidden behavior); this is purely about *order*,
letting remote value ship without waiting on the scheduler rewrite. Recommended
unless you'd rather do it all at once.

## Touch points in `claude-status.py`

- `main()` / argparse — add `--emit`. Also **soften the missing-local-projects
  guard**: today `main()` exits before serving if `~/.claude/projects` is
  absent; under the always-on `collect_all()` invariant (and especially with
  remotes configured), server mode should instead treat a missing local
  projects dir as *zero local sessions* and still serve, so the hub isn't dead
  just because this machine has no local sessions.
- New: hosts-config loader (+ unique-name check); on-disk parse cache load/save
  for `--emit`; `collect_remote(host)` (SSH argv exec → parse → stamp host +
  `key` → rewrite resume → strip remote flags → mark fresh); `collect_all()`
  merging local + remotes + reapplying hub flags + assembling `hosts[]`; a
  per-host freshness cache + lock/future for coalescing.
- `collect()` — unchanged for local; `--emit` prints it verbatim.
- `parse_session` / `_CACHE` — return a copy so `collect()`'s transient
  stamping (`open`/`live_status`/`rss_kb`/`flagged`/`done`) no longer mutates the
  cached object; then add the disk-persistence path (parse-layer fields only)
  used by `--emit`.
- resume construction — `shlex.quote` both layers (local + remote).
- `FLAGS` / `load_flags` / `save_flags` / `POST /api/flag` — key on composite
  `key`. `load_flags` migrates *pre-existing on-disk* bare-`id` entries →
  `local:<id>` at startup; the `POST` endpoint **requires `key` and 400s a
  bare-`id` body** (no request-time mapping).
- **`mark_done_cli` (the `--done` CLI) is a third *live* flag writer** and must
  be migrated too. Today it does `FLAGS[sid] = marks` with a **bare** `sid`
  ([claude-status.py](../claude-status.py)); post-remote it must construct the
  **composite key** directly (`local:<id>` on the machine it runs on — the writer
  is always "local" to itself), *not* rely on the startup bare→`local` migration.
  Otherwise `--done` writes a bare-`id` entry at runtime while `collect_all()`
  looks up `local:<id>`, so the mark silently fails to apply until the next hub
  restart. (This is the un-enumerated mechanical half of the accepted per-server
  flag divergence — surfaced once `--done` exists as a live write path.)
- `Handler.do_GET` `/api/sessions` — always call `collect_all()` and return the
  extended shape (per the always-on invariant; `hosts[]` is just `["local"]`
  with no remotes).
- `PAGE` — adaptive scheduler + activity listeners + `loadInFlight`; Host column
  in `COLS`/`rowHtml`; host in search/grouping/chips; host-status strip; stale
  styling; `cards2()` excludes stale from live/RAM. **`rowHtml` conditional
  resume:** suppress/grey the `⧉ resume` copy button when a row is open or stale
  (today [claude-status.py:590](../claude-status.py) *always* emits it, so a
  backend that only nulls `resume` ships a copy-nothing button) — the frontend
  half of the open-conditional reopen affordance, ships in step 1. **Flag path
  must move to `key`:** flag buttons carry `data-key` (not `data-id`),
  `toggleFlag(key,kind)` resolves via `DATA.find(x => x.key === key)`, and the
  POST body is `{key, kind, value}` — otherwise remote flag POSTs fail with 400
  (see Flags). **Cross-design (transcript viewer):** if `AI/transcript.md`'s
  per-row "view" link has shipped, it must be **suppressed on `host != "local"`
  rows** (a remote session's transcript isn't on the hub → 404); whichever of the
  two features ships second owns wiring this.
- `write_static()` (`--once`) — **local-only by default.** Since the whole
  premise is flaky/slow SSH, a "write index.html and exit" command must not risk
  blocking for `N × ConnectTimeout` on unreachable hosts. Remotes are included
  only with an explicit opt-in (`--once --remote`), and even then under a single
  bounded overall deadline so it can't hang indefinitely. Either way the
  snapshot is frozen — adaptive polling / live remote refresh don't apply, and a
  host that timed out during the opt-in poll is written as an error entry in
  `hosts[]`, not silently omitted.

## Must-verify-before-code (promoted from "open questions" — these are
## load-bearing, not free knobs)

- **Reopen semantics against a live session** — *partially resolved (user
  workflow, 2026-07-01).* The user runs `claude` interactively with **no
  tmux/nohup**, so a dropped SSH SIGHUPs the foreground process and `claude`
  exits: the session becomes **closed**, only the `.jsonl` survives, and
  `claude --resume <id>` reconstructs from it — i.e. the "closed → resume" branch
  is the live case for this setup, and the dangerous "`--resume` against a
  *still-live* session" sub-case **cannot arise** (a drop leaves nothing live to
  double-attach to). The "still-open → offer attach, not resume" branch only
  becomes reachable **if the user adopts tmux** (drop detaches instead of
  killing; reopen = `tmux attach` via the `resume_wrapper` reattach form). tmux
  is under consideration but has a real cost for this user (scrollback moves into
  tmux's buffer, away from the native terminal). Still worth a one-off empirical
  confirmation that `claude` exits on SIGHUP and that `--resume` errors (not
  corrupts) if ever run against a live session, but the design is safe either
  way because open rows are offered attach, never a bare resume.
- **`REMOTE_TIMEOUT` vs. worst-case *cold* parse** — *measured 2026-07-01 on the
  local corpus:* 278 sessions / 309 MB cold-parses in **~2.0 s** (warm/cache-hit
  **~1 ms**; per-file avg 7 ms; largest single file 18 MB → 47 ms). `--emit` JSON
  payload = **165 KB** for 195 shown sessions (**36 KB** gzipped, so `ssh -C`
  makes the download trivial). Cold parse is seconds not minutes, so an overall
  `REMOTE_TIMEOUT` ~30 s comfortably covers a 3–4× slower remote; the on-disk
  cache is enormously effective (2000 ms → 1 ms). Original concern retained:
  must be sized so a
  first/cold `--emit` on a full ~30-day corpus can finish and write its cache;
  otherwise the cold-parse-vs-timeout trap makes a healthy host permanently red
  (see the parse-cache section). Pairs with adopting incremental cache writes.

## Open questions (genuine product/ops choices)

- **Flag sync** hub↔per-server: v1 leaves them separate (composite key makes a
  future sync unambiguous), and the divergence hits the reopen scenario (see
  Flags). Worth building the SSH write-back now, or defer?
- **v1 sequencing**: ship the thinner v1 (remote + reopen + longer fixed poll)
  first and add adaptive polling as a follow-up, or do it all at once? (See the
  sequencing note under Implementation order.)
- **Other numbers**: active cadence 10 s, ceiling 5 min, coalesce/freshness
  ~5 s, `ConnectTimeout` 8 s — right values? (`REMOTE_TIMEOUT` is promoted
  above, since it's correctness- not preference-shaped.)
- **`--emit` exposure ack**: it prints only session *metadata* (cwd, branch,
  previews, ids) — the same data already on the local dashboard — over your own
  SSH. No new secret surface, but previews contain prompt text; conscious ack.
