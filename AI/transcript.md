# session transcript viewer (design → built)

**Status: built 2026-07-02** (backend + both page changes in `claude-status.py`;
verified end-to-end against the real corpus — see "Implementation notes" at the
bottom for deltas discovered during the build). Originally a reviewable spec. **[decided]** = user
decision; **[verified]** = checked against the real corpus; unknowns are in
"Open questions". Review applied: codex r1 (corrected the sidechain model +
format facts), sonnet (dry, 4 rounds: agentId validation, route collision,
addressing, nested parsing), opus altitude (**cut the payload-trimming
subsystem** in favour of whole-file-gzipped + client virtualization; surfaced
`system`-subtype content and the remote "view" gap); codex r2 (re-verified the
sidechain model holds; corrected the system-subtype skip list to a
content-driven rule, softened the Agent↔agentId exact-match claim, added the
`fallback` part type, sub-agent meta/resume split, `shlex.quote` for resume,
tightened the XSS spec). All r2 findings corpus-verified before applying.

## Goal

The dashboard lets you *find and resume* sessions but not *read* them. Add a
viewer: from a dashboard row, open the full conversation of that session, with
search, visually distinct user-vs-assistant turns, and a fast way to jump
between the human prompts. Style is deliberately minimal for v1 ("style later");
function first.

## Verified JSONL format (grounding — do not re-guess)

Undocumented Claude Code internal; treat as external + defensively parsed.
Enumerated across the largest local transcripts.

Top-level `type` values: `assistant`, `user` (the conversation) plus many
metadata types — `last-prompt`, `permission-mode`, `mode`, `attachment`,
`queue-operation`, `ai-title`, `agent-name`, `bridge-session`, `custom-title`,
`system`, `file-history-snapshot`, `agent-color`. The viewer consumes the
`user`/`assistant` conversation turns, and — this is the correction from the
altitude review — **`system` records are NOT uniformly skippable**: a
"full history" reader that dropped them all would **silently lose real content**.
`system` has a `subtype` **[verified]**, and some carry reader-visible content:

The rule is **content-driven, not a subtype whitelist** (codex r2 correction —
the earlier draft listed `scheduled_task_fire`/`informational`/`bridge_status`
as skippable metadata, but the corpus now shows **all three carry reader-visible
`content`**, as does `model_refusal_fallback`; subtype lists rot as Claude Code
evolves, a content check doesn't):

- **Render any `system` record with a top-level `content` field** as a distinct,
  clearly-labelled, collapsed-by-default marker block (not styled as a
  user/assistant turn), whatever its subtype. Corpus today **[verified
  2026-07-02]**: `away_summary` (852/852 have content), `local_command`
  (171/171), `scheduled_task_fire` (20/20), `informational` (15/15),
  `compact_boundary` (6/6 — a context-compaction divider; omitting it makes the
  conversation jump inexplicably), `bridge_status` (5/5),
  `model_refusal_fallback` (1/1).
- **`api_error` (77) has NO top-level `content`** — its text lives in
  `error.formatted` / `error.message`. Special-case it: render a visible error
  marker from those fields.
- **Skip only content-less metadata:** `turn_duration` (2644, 0 content),
  `agents_killed` (1, 0 content), and the other non-`{user,assistant}` types
  above (`attachment`, `mode`, etc.), consistent with `collect()`'s sidecar
  filtering. An unknown content-less `system` subtype is skipped too — the
  content check is exactly what makes that safe.

`message.role` is only `user`/`assistant`. `message.content` is a **list**
(~46k) or a **str** (~2k, all on **user** turns — human prompts; assistant
content was list in every sampled case, but accept str defensively). Content
parts and key sets **[verified]**:

| part `type`   | keys                                          | render as |
|---------------|-----------------------------------------------|-----------|
| `text`        | `text`, `type`                                | prose (escaped) |
| `thinking`    | `thinking`, `signature`, `type`               | collapsed "thinking" (field is `thinking`, **not** `text`) |
| `tool_use`    | `id`, `name`, `input`, `caller`, `type`       | collapsed tool call (name + escaped JSON input) |
| `tool_result` | `content`, `type`, `tool_use_id`, *`is_error`?*| collapsed output, linked to its `tool_use` via `tool_use_id`; red **only when `is_error is True`** |
| `image`       | `source`, `type`                              | `source = {type:"base64", media_type, data}` |
| `document`    | `source`, `type` (same source shape)          | a PDF/file paste; render a labelled stub (see media) |
| `fallback`    | `from`, `to`, `type`                          | model-switch marker (`{from:{model}, to:{model}}`, assistant turn, 1 instance **[verified]**); render as a small labelled marker ("model fallback: X → Y") |

**`is_error` is optional** **[verified]** — ~11.5k of ~21k `tool_result` parts
omit it. Parser MUST default absent → `false` and only style an error when
`is_error is True`; never assume the key exists.

`tool_result.content` is a **str** (~13.6k) or a **list** of parts (~1.1k, e.g.
text/image). Top-level keys also present: `isSidechain` (**always `false`** in
top-level files — see Sub-agents), `parentUuid`, `toolUseResult` (mirrors the
tool result, and carries `agentId` on `Agent` calls), `isMeta`, `level`.

### The critical distinction: not every `user` turn is a human prompt

**[verified]** Of `user` turns: **str content (~2051) = typed human prompts**;
list content `(tool_result,)` (~14739) = **tool outputs**, `(text,)` (~66) =
prompts sent as a text part, `(image,)` (~1) = a pasted image (and `(document,)`
also occurs — this enumeration is illustrative, not exhaustive). So **~86% of
`user` entries are tool-result deliveries, not prompts.** The prompt navigator
MUST anchor on real human prompts only, or it lists thousands of "prompts". The
classification *rule* below (contains text/image/document, no tool_result)
already covers every case, exhaustively.

A dedicated **`extract_human_prompt(turn)`** helper decides this — do **not**
reuse `_first_text()`/preview logic literally (it ignores `image`/`document`
parts, so an image-only or document-only human input would be misclassified as
"not a prompt"). Rule: a turn is a human prompt iff `type=="user"` AND its
content is a str, OR a list that contains a `text`/`image`/`document` part and
**no** `tool_result` part; after stripping leading `<system-reminder>` /
command-wrapper blocks, a text-only prompt with no remaining human text is not a
prompt (pure wrapper), but an image/document-only prompt still counts (labelled
"[image]" / "[document]" in the nav).

**`is_prompt=false` scopes the *navigator* only — never the transcript.** A turn
with `is_prompt:false` (a tool-result delivery, or a wrapper-only turn) is
*omitted from the prompt nav list* but **still rendered in `turns` and on the
page**. It must never be dropped from the history — that would silently lose
content (e.g. a slash-command wrapper turn), violating the no-silent-skip rule.
`is_prompt` is a nav flag, not a visibility filter.

Note: a sub-agent transcript's *first* `user` turn (str content) is the spawning
agent's task instructions, not literally human-typed; the rule above will mark it
`is_prompt:true`. That's harmless (it *is* the prompt of that sub-conversation) —
flagged here so it isn't later "fixed" as a misclassification.

## Sub-agents (sidechains) — corrected model **[verified]**

The earlier draft was wrong: sidechains are **not** interleaved in the top-level
file. **Every** top-level conversation turn is `isSidechain:false` (47,430/47,430
sampled). Instead, each sub-agent's transcript is a **separate nested file**:

```
~/.claude/projects/<encoded-cwd>/<session-id>/subagents/agent-<agentId>.jsonl
```

(1,300+ such files locally). The spawning tool is the **`Agent`** tool (not
`Task`); its tool-result turn carries **`toolUseResult.agentId`**. The match is
**near- but not exact** (codex r2 correction, corpus-verified 2026-07-02: 1,196
`Agent` tool_use parts vs 1,193 `agentId`s — a few calls never get a result,
e.g. killed/interrupted agents). Two consequences: (a) the "sub-agent transcript
unavailable" stub is a *live* path, not belt-and-suspenders; (b) the co-located
`agent-<id>.meta.json` carries **`toolUseId`**, so a resultless `Agent` call can
still be linked by indexing meta files by `toolUseId` — the parser SHOULD use
this as the fallback linkage before showing the stub. **`agentId` is bare
lowercase hex** (e.g.
`a3307f68596da09bf`), and the filename is **`"agent-" + agentId + ".jsonl"`** —
i.e. `agentId` is the filename stem *minus* the `agent-` prefix, not the whole
stem. This distinction is load-bearing for the query-param validation below.
This is the linkage.

Consequences:
- `collect()` / `_session_path()` glob only `PROJECTS/*/*.jsonl` (one level), so
  they never see nested subagent files — correct for the dashboard, and the
  viewer must scan the nested path explicitly for sub-agent transcripts.
- **v1 = main transcript + sub-agent inventory (lazy).** Render the main thread
  (the top-level file). Each `Agent` `tool_use` shows a collapsed "sub-agent"
  block; expanding it **lazy-loads** that sub-agent's nested file via a separate
  request (`?agent=<agentId>`), so the main payload never carries sub-agent
  bodies. If the `agentId` linkage is missing for some call, show a visible
  "sub-agent transcript unavailable" stub — **never silently omit it**.
- Sub-agent transcripts have the same shape and are parsed by the same
  `parse_transcript`.
- **`subagents/` is not a flat list of `agent-*.jsonl`.** Two gotchas
  **[verified]**: (a) each `agent-<id>.jsonl` has a co-located
  **`agent-<id>.meta.json`** — free, already-available metadata to label a
  collapsed sub-agent block with something better than a bare id (e.g.
  "general-purpose: Opus design review"); the v1 lazy loader should read it for
  the block header. **Key sets vary** (codex r2, corpus-verified): `agentType` +
  `description` are always present; `toolUseId` almost always; `spawnDepth` in
  only ~53%; extras like `worktreePath`, `worktreeBranch`, `stoppedByUser`
  appear in a few. Parse with `.get()` throughout — only `agentType`/
  `description` may be relied on for the label. (b) `/loop` /
  `/schedule` background routines create a nested **`subagents/workflows/wf_<id>/`**
  subtree (extra nesting, its own `.meta.json` siblings) whose agents are **not**
  linked from the top-level `Agent` tool_use. v1's direct `?agent=<agentId>`
  lookup is unaffected (it addresses a specific file), but any *future*
  "sub-agents index panel" that globs `subagents/` MUST skip `workflows/` and
  `*.meta.json` — else it lists unlinked workflow agents as if they were this
  session's sub-agents.

## Backend

- **`parse_transcript(path)`** → ordered turns, parsed **defensively per-line**
  (skip a malformed *line*, never the file). Returns:
  ```
  { "meta": {id, project, cwd, title, started_ts, updated_ts,
             msgs, user_msgs, asst_msgs, resume},
    "turns": [ { i, role, ts, is_prompt, parts:[ ... ] } ] }
  ```
  `i` is the **dense, 0-based index over *surviving* turns** (assigned after
  malformed-line skips), so `turns[i].i == i` — a stable client-side anchor for
  the prompt nav and for append-on-poll (fetch turns after the last `i`), *not* a
  source line number.
  `meta` carries the same **`resume`** string as `parse_session`'s result
  so the transcript page's ⧉ resume button reuses it verbatim (no client-side
  duplication of the format string), and the same `user_msgs`/`asst_msgs`
  breakdown the dashboard shows. Two codex-r2 refinements:
  - **Resume construction is centralized and shell-safe**: one helper builds
    `cd <shlex.quote(cwd)> && claude --resume <id>`, used by *both*
    `parse_session` and `parse_transcript` — the current inline
    `cd "%s"` breaks on quotes/`$` in `cwd`.
  - **A sub-agent transcript gets sub-agent meta, not a bogus resume.** When
    parsing `?agent=<agentId>`, `meta` carries `{agent_id, agent_type,
    description, parent_id}` (from the co-located `.meta.json`) and **no
    `resume`** — `claude --resume agent-<id>` is not a real command; the page
    hides the resume button when `meta.resume` is absent.

  Each part is typed and self-describing: `{kind:"text", text}`,
  `{kind:"thinking", text}`, `{kind:"tool_use", id, name, input}`,
  `{kind:"tool_result", tool_use_id, is_error, text|parts}`,
  `{kind:"image"|"document", media_type, data}` (inlined in v1),
  `{kind:"tool_reference", tool_name}` (**[verified]** ~300 real cases, nested in
  tool_result content), `{kind:"fallback", from_model, to_model}`, and — for any
  **future/unknown** part type — `{kind:"unknown", raw_type}` rendered as a
  visible "unrecognized block" (no-silent-skip; a new content type surfaces as a
  known-unknown).
  An **`Agent` `tool_use` part additionally carries a `subagent` object** (codex
  r2): `{agent_id, agent_type, description}` — `agent_id` from
  `toolUseResult.agentId` (or the meta-file `toolUseId` index for resultless
  calls), label fields from the co-located `.meta.json`. `subagent: null` when
  no linkage resolves → the client renders the "unavailable" stub. This is what
  the lazy `?agent=` expansion keys on; without it the client has no id to
  request.
- **A list-form `tool_result.content` is parsed by the *same* part dispatcher.**
  `tool_result.content` is a str or a **list of parts** (verified to contain
  `text`, `image`, and `tool_reference`). Those nested parts run through the same
  typing/`unknown`-fallback/`esc()`-escaping as top-level parts (so a nested
  `image` becomes a `{kind:"image"}` part, `tool_reference` a
  `{kind:"tool_reference"}`, an unrecognized nested type a `{kind:"unknown"}` —
  never dropped, `tool_name`/nested text escaped). The `tool_result` part's
  `parts` field holds these nested parts inline.
- **`GET /api/session?id=<id>[&agent=<agentId>]`** → the object above. Without
  `agent`, the main top-level transcript; with `agent`, that sub-agent's nested
  `subagents/agent-<agentId>.jsonl`.
  - **Route dispatch must not collide with `/api/sessions`.** `Handler.do_GET`
    matches by `self.path.startswith("/api/sessions")`; since
    `"/api/sessions?…".startswith("/api/session")` is **also true**, a naive
    `elif startswith("/api/session")` would swallow the dashboard's 30 s
    sessions-list poll. Match on the **exact path** (`urlsplit(path).path ==
    "/api/session"`) or test the plural route first — call this out so the
    build doesn't reproduce the existing `startswith` idiom into a bug.
  - **ID resolution must NOT call `resolve_session_id()`** — that raises
    `SystemExit` on no/ambiguous match, which would kill the server thread /
    process inside a request handler. Add a **non-raising** resolver (or a
    `raising=False` mode) returning one of {found sid, none, ambiguous}; the
    handler maps none → **404** and ambiguous → **409** (JSON error body).
  - **`agent` param validation:** the query value is the **bare `agentId`**
    (hex), so validate it as `^[0-9a-f]+$` and construct the path as
    `<session-dir>/subagents/agent-<agentId>.jsonl` — do **not** validate against
    `agent-[0-9a-f]+` (that would reject every real request, since the param has
    no `agent-` prefix). Reject anything else (no path traversal).
  - **`id` param validation — equally load-bearing** (sonnet r2, **verified
    exploitable then fixed 2026-07-02**): the `id` token is globbed into a path
    too, so it needs the *same* charset gate as `agent`. `glob.escape` neutralizes
    glob metacharacters but **not** `/` or `..`, so an unvalidated `id` like
    `../../../../tmp/secret` walks out of `PROJECTS` and the endpoint returns any
    readable `*.jsonl` on disk (confirmed HTTP 200 on a planted file). Gate every
    token to the UUID charset `^[0-9a-fA-F-]+$` **inside `_session_path` and at
    the top of `_find_session_id`** (the two functions that touch the filesystem,
    so the guard covers the CLI resolvers too) — `/` and `.` cannot appear, so
    traversal is impossible. Don't rely on `glob.escape` alone; it is not a
    traversal defense.
- **Whole-transcript payload — no trimming subsystem in v1** (altitude cut). `/api/session`
  returns the **entire** parsed top-level transcript (all turns, all parts, media
  inlined), `gzip`-compressed (`Content-Encoding: gzip`). Rationale: this is a
  **localhost** reader of your own files — a ~19 MB text/JSON body gzips hard and
  crosses `127.0.0.1` in milliseconds, so trimming the *wire* buys little; the
  real cost is **DOM rendering**, a *frontend* concern solved by virtualization
  (see Performance), not server-side payload flags. Because there are no flags,
  `parse_transcript` output is **request-independent**, which collapses a pile of
  complexity:
  - **Caching reuses `_CACHE`'s `(mtime, size, result)` *shape* in a *separate*
    dict** (e.g. `_TRANSCRIPT_CACHE`) — flag-blind, overwrite-on-change
    ([claude-status.py](../claude-status.py)); no nested or compound key. It must
    **not** share the literal `_CACHE` dict: a main transcript's path is *also* a
    `parse_session` key, and the two store different value shapes
    (`{meta, turns}` vs the stats dict), so one bare-path dict would serve the
    wrong shape to whichever function ran second.
  - **No part-fetch endpoint, no `include_*` flags, no truncation/stub wire
    contract, no media sub-requests, no dotted-path part addressing.** (These were
    a subsystem built to trim a wire payload that doesn't need trimming on
    localhost+gzip — cut per the altitude review. The whole class of nested-media
    addressing, per-part truncation markers, and flag-stable indices goes with
    it.)
  - Sub-agents stay **lazy** (`?agent=<agentId>`) — that split is about *which
    file to load*, not payload trimming, so it remains (validated `^[0-9a-f]+$`,
    404/409 as above).
  - **Deferred escape hatch** (only if a real session proves pathological, not
    v1): a single `include_media=off` returning `{kind, media_type, bytes}` stubs
    + click-to-load. Noted so it isn't reinvented; the 62 nested ~94 KB base64
    images are rare, gzip well, and virtualization keeps off-screen ones out of
    the DOM anyway.
- **`GET /session?id=<id>`** → serves the transcript page (second embedded
  `PAGE`-style string). **[decided: separate linkable page]** — real
  bookmarkable URL, back button works, isolated from the dashboard SPA.

## Frontend (the transcript page)

- **Escaping / XSS is a hard requirement.** The transcript renders **untrusted**
  content — arbitrary tool output, file contents, prompts, JSON tool inputs,
  pasted media. Tightened per codex r2 (the dashboard's "`esc()` before
  `innerHTML`" idiom alone is too loose for this much hostile text):
  - **Default is DOM construction + `textContent`** for transcript-derived
    strings; `innerHTML` only for trusted page chrome, or with every
    interpolated value escaped for its *context* (text vs attribute — the
    dashboard's `esc()` covers both: it escapes `&<>"`).
  - **No transcript-derived string** in event-handler attributes, `style`, or
    URLs — the only URL built from transcript data is the media `data:` URL
    below, and query values in `?agent=` links are the server-validated hex id.
  - Media is inlined **only** for an allow-list of safe image MIME types
    (`image/png|jpeg|gif|webp`) as a `data:` URL, base64 data validated
    (`^[A-Za-z0-9+/=\s]+$`); anything else (documents, unknown MIME) renders as
    a labelled non-executable stub.
- **Distinct user vs assistant** **[decided]** — different background / indent /
  label; human prompts (`is_prompt`) are the strongest element.
- **Jump between prompts** **[decided: sidebar list + keyboard]** — a sidebar
  lists every human prompt (truncated one-liner + relative time; image/doc
  prompts labelled); click scrolls to it; keyboard prev/next (`[`/`]` or `j`/`k`)
  between prompt anchors. Each prompt turn is an `id` anchor.
- **Search** **[decided]** — client-side filter/highlight with next/prev match
  (`n`/`N`); human+assistant text by default, tool output opt-in via a toggle.
- **Tool / thinking: collapsed by default + a top-of-page global expand/collapse-
  all toggle** **[decided]**. Each `tool_use`/`tool_result`/`thinking`/sub-agent
  block renders folded (one click to expand); the top bar has one control to
  expand/collapse all. Folded, not hidden.
- **Meta bar**: project, title, started/last-active, message counts, a ⧉ resume
  copy button (same command as the dashboard), link back to the dashboard.

## Performance

These sizes are a **monotonically growing** corpus, not fixed ceilings — do not
hard-code them. As of 2026-07-01: top-level file max ~**19 MB**; *recursive* size
(session + nested sub-agent logs) ~**127 MB** (one session had 555
`Agent`-linked sub-agent files). Two levers, and the design deliberately uses the
*client* one:

- **Wire:** the top-level file only (sub-agents lazy via `?agent=`), gzipped.
  ~19 MB of text/JSON gzips to a few MB and moves over `127.0.0.1` in ms — a
  non-issue at localhost, which is why the server-side trimming subsystem was cut.
- **DOM (the real cost):** eagerly rendering thousands of turns is what actually
  hurts, so **client virtualization** (render only turns near the viewport;
  ordered `turns[]` makes this straightforward) is the primary scaling lever.
  Pull it into v1 if the largest sessions lag; otherwise ship eager render first
  and virtualize next — either way it's a self-contained frontend change that
  *also* keeps off-screen media/tool-output out of the DOM for free (subsuming
  what `include_media`/caps would have done). Collapsed-by-default tool/thinking
  blocks already cut initial layout cost.

No content is trimmed or omitted server-side, so there is no truncation/omission
to surface — the whole transcript is present; virtualization only controls *when*
a turn is mounted.

## Dashboard integration

- A per-row **"view"** link (next to `⧉ resume`) → `/session?id=<id>`. Small.
- **"view" is only valid for locally-readable sessions.** It resolves `id`
  against the local `~/.claude/projects` (`_session_path`), so it must be
  **suppressed on rows whose transcript isn't local** — two instances of one
  class:
  - **`--once` static mode:** a `file://` snapshot has no server, so
    `/session?id=` would 404/do nothing. Omit/disable the link in the static
    snapshot (a build-time flag on `rowHtml`).
  - **Remote rows (per `AI/remote.md`):** once remote lands, rows include
    `host != "local"` sessions whose files don't exist on the hub, so "view"
    would 404. **Suppress "view" on `host != "local"` rows** (exactly as `--once`
    does). The richer path — the hub fetching a remote transcript over SSH via
    the `--emit` role — is a real feature, **deferred explicitly**, not v1.
  - Cross-design note: both this design and `remote.md` edit `rowHtml` /
    `do_GET`; **whichever ships second owns wiring this suppression** and should
    be sequenced aware of the other (see `AI/remote.md`).
- **Live-refresh: v1 is a load-time snapshot** (the transcript is fetched once
  when the page opens; a manual "↻" re-fetches). The common case is viewing a
  *live* session still being written, so this is a real decision, not an
  omission: a static snapshot is the simple v1, and append-on-poll (fetch only
  turns after the last `i`) becomes natural once virtualization lands. State it
  so a frozen view isn't mistaken for a bug against the dashboard's 30 s auto-poll
  expectation.
- No change to the existing `/api/sessions` data model.

## Touch points in `claude-status.py`

- New `parse_transcript(path)` (parses `user`/`assistant` turns **and**
  content-bearing `system` records → labelled marker blocks, `api_error` from
  `error.formatted`/`error.message`; same defensive per-line skip),
  `extract_human_prompt(turn)`, and a non-raising id resolver (or
  `resolve_session_id(..., raising=False)`); a helper to locate a sub-agent's
  nested file from `(session_id, agentId)` **and read its co-located
  `agent-<id>.meta.json`** for the collapsed block's label (plus a
  `toolUseId → agentId` meta index for resultless `Agent` calls).
- A shared shell-safe resume builder (`shlex.quote(cwd)`) replacing the inline
  `cd "%s"` in `parse_session`, used by both parsers.
- **Transcript caching reuses `_CACHE`'s `(mtime,size)` pattern in a *separate*
  dict** (`_TRANSCRIPT_CACHE`) — `parse_transcript` output is request-independent
  (no payload flags), so no nested/compound key; but it is **not** the same dict
  as `parse_session`'s `_CACHE`, since a top-level session path keys both and the
  two value shapes differ.
- New `TRANSCRIPT_PAGE` embedded string (second SPA) + its assets/CSS.
- `Handler.do_GET`: route `/session` → `TRANSCRIPT_PAGE`; `/api/session` →
  `parse_transcript` JSON — matched on **exact path** (`urlsplit(path).path ==
  "/api/session"`, not `startswith`, to avoid swallowing `/api/sessions`),
  404 unknown id, 409 ambiguous, `agent=<hex>` for the lazy sub-agent transcript;
  existing routes unchanged. (No part-fetch route — cut with the trimming
  subsystem.)
- `PAGE` (dashboard): per-row "view" link in `rowHtml`, **suppressed in `--once`
  and on `host != "local"` rows**.

## Verified against the corpus (resolved)

- **Sub-agent linkage** **[verified]**: `Agent` tool → `toolUseResult.agentId`
  (bare hex) → `subagents/agent-<agentId>.jsonl`. Keep the "unavailable" stub for
  any call whose `agentId` doesn't resolve to a file.
- **`document` parts** **[verified]**: exist (2 real instances, a PDF paste),
  shape `{type:"document", source:{type:"base64", media_type:"application/pdf",
  data:…}}` — same source shape as `image`. Render as a labelled stub (see media).
- **`image.source`** **[verified]** `{type:"base64", media_type, data}`.
- **No mixed-type content** **[verified]**: across all ~24.7k `user` turns, no
  turn combines `tool_result` with `text`/`image`/`document` — so the
  human-prompt rule's "contains text/image/document AND no tool_result" clause is
  exhaustive and safe.
- **Assistant content is 100% list** (46,637/46,637) — the "accept str
  defensively" default is belt-and-suspenders, not masking a live case.

## Open questions — resolved **[decided 2026-07-02]**

- **Virtualization**: eager render only in v1; accept lag on the largest
  sessions and revisit after real use (user declined `content-visibility` for
  now — keep it as the first lever if lag shows up).
- **Search scope**: a toggle between prompts+assistant and everything
  (tool output is useful, e.g. finding branch names); **default =
  prompts+assistant**.
- **Sub-agents**: both — lazy per-`Agent`-call expansion *and* a session-level
  sub-agents index panel ("I have no way of seeing them today"). The index
  globs `subagents/agent-*.jsonl` one level only (which inherently skips
  `workflows/`), labels from `.meta.json`, and each entry links to the
  sub-agent's own page (`/session?id=<sid>&agent=<agentId>`).
- **"view" link target**: new tab (keeps dashboard filter/scroll state).
- **Media (deferred, not v1)**: v1 inlines media; only if a session proves
  pathological, add `include_media=off` stubs + click-to-load.

## Implementation notes (deltas found during the build, 2026-07-02)

- **Sub-agent storage is FLAT** **[verified]**: no `subagents/*/subagents/`
  dirs exist; `spawnDepth: 2` meta files sit in the *same* flat dir as depth-1
  agents, and 28 sub-agent transcripts contain their own `Agent` calls. So
  `_subagent_dir()` maps a sub-agent file to its *containing* `subagents/` dir
  (not a nested one) — that's what makes recursive inline expansion work.
- **Prompt turns carry a `label`** (cleaned nav one-liner, ≤200 chars,
  `"[image]"`/`"[document]"` for media-only prompts) so the client doesn't
  re-implement wrapper-stripping. Schema addition to the `turns[]` entries.
- **`tool_reference` key is `tool_name`** **[verified]** (298 cases, all
  `{type, tool_name}`).
- Harness-injected turns that are plain text but not human-typed (e.g.
  `<task-notification>…` background-task events) classify as prompts — the
  known-unknown side of the conservative rule; extend `WRAPPER_PREFIXES` only
  with evidence, never speculatively.
- The `?agent=` response reuses the parent's `id` in `meta.id` (so page links
  stay stable) and adds `agent_id`/`agent_type`/`description`; `resume` is `""`
  for sub-agent transcripts (not resumable) and the page hides the button.
- The transcript page verifies cleanly in jsdom (all turns render, folds
  default-collapsed, expand-all, search scopes, XSS probe inert) and
  end-to-end over HTTP against the largest local session (3,766 turns,
  555 linked sub-agents, gzip wire ~540 KB, parse ~0.3 s warm).

## Efficiency + panel refinements (opus escalation round, 2026-07-02)

- **`_subagent_metas` is cached by the `subagents/` dir mtime** (`_METAS_CACHE`).
  Reading it is O(N) in sub-agent count (555 on the biggest session); it has two
  legitimate per-request callers (`parse_transcript` linkage labels +
  `list_subagents` panel labels), so the cache stops the same request scanning
  twice. Safe against staleness because meta *labels* (`agentType`,
  `description`, `toolUseId`) are written once at spawn and never edited in
  place, while create/delete/rename — the only events that change the agent set
  — bump the dir mtime. (This is the same dir-mtime-immutability assumption codex
  accepted for `_TRANSCRIPT_CACHE`'s subagents component.) Measured: cold 12.5 ms
  → warm 0.01 ms.
- **Single-label lookups use `_read_meta(dir, agent_id)`** — a direct one-file
  read — so opening one sub-agent page never scans all 555 metas for one label.
- **A sub-agent view's panel lists ITS OWN children**, derived from that
  transcript's Agent-call linkage (`_linked_subagents`), **not** a dir glob:
  storage is flat, so a sub-agent's children share the dir with unrelated
  siblings — `list_subagents`' glob is correct only for a top-level session
  (where every file in `subagents/` *is* a direct child). Verified: a nested
  sub-agent shows exactly its 3 real children; the top-level session still lists
  all 555.

## Frontend UX iteration (user-directed, 2026-07-02)

Post-build changes to the transcript page from direct user feedback:

- **Human prompts are the loudest element.** Distinct blue-tinted panel
  (`linear-gradient(#182a44,#132135)`, 4px `--accent2` left border, rounded) +
  a **serif** face (`ui-serif, Georgia`) at 16px — deliberately a *different
  font*, not just a size bump, so prompts read as a separate voice from the
  sans-serif assistant/tool text.
- **Per-prompt elevator.** Each prompt turn carries a small ▲/▼ button pair in a
  left gutter that jumps to the previous/next prompt (`stepPrompt`), complementing
  the sidebar list and the `[`/`]`/`j`/`k` keys.
- **Sub-agent transcripts open in a 3rd split pane** (`#apane`, right of the main
  column), **not a new tab and not inline**. Clicking a sub-agent in the left
  panel opens the pane *and* scrolls the main transcript to the `Agent` call site
  (tagged `data-agent-call=<agentId>`), flashing it. Inside the pane, a nested
  `Agent` pushes onto a breadcrumb stack (back/close buttons; `Esc` closes). The
  pane is the lazy-load boundary — one transcript fetched at a time, on demand —
  so the earlier "don't auto-open agent folds" guard is gone (agent folds no
  longer fetch on toggle; the pane does).
- The main column widens (`body.paneopen #main{max-width:none}`) when the pane is
  open; below 820px the pane overlays full-width.

## Markdown rendering + copy buttons (user-directed, 2026-07-02)

- **Assistant text renders as Markdown**; prompts and tool output stay plain.
  `partNode` gets `ctx.md` (set only for assistant turns); a text part then goes
  through **`mdToDom`**, a compact, self-contained Markdown→DOM renderer
  (headings, bold/italic/strike, inline + fenced code, lists, blockquotes, pipe
  tables, hr, links). **No external lib** (the page is CSP-free but must stay
  single-file) and — critically — **no `innerHTML`**: every node is built with
  `createElement`/`createTextNode`, so untrusted transcript text can never be
  parsed as HTML. Link hrefs are scheme-gated (`http(s)`/`mailto`/relative/anchor
  only; `javascript:`/`data:` links degrade to plain text), `target=_blank
  rel=noopener`. XSS-tested (script tags, `onerror`, `javascript:` links all
  inert).
- **Copy buttons** (`⧉ copy`) on every input/output: each prompt, each assistant
  turn (header), and each fold (tool call input, tool result output, thinking,
  system markers, unknown blocks). They copy the **raw source verbatim** —
  Markdown left intact per the request, not the rendered text. In a fold summary
  the button `stopPropagation`/`preventDefault`s so a copy click doesn't toggle
  the fold. A turn/fold with no textual content gets no button.

### Markdown-renderer hardening (sonnet review, 2026-07-02)

The renderer runs on untrusted text, so two DoS classes were closed (both
corpus-independent — garbled/pasted content triggers them, no adversary needed):

- **Blockquote recursion is depth-capped** (`mdToDom(src, depth)`, cap 24). Only
  blockquotes recurse; a single line of N leading `>` was N stack frames →
  `RangeError` at ~3k, which (uncaught) blanked every later turn. Past the cap the
  remainder renders verbatim.
- **Inline parsing is length-capped** (`mdInline`, 2000 chars/line). The inline
  regex is O(n²) on a pathological long single line (100k `[` ≈ 14 s); over the
  cap the line renders as a plain text node. Normal prose lines are well under.
- **Defense in depth:** `mdNode` wraps `mdToDom` in try/catch → plain-text
  fallback; `renderTurns`/`loadAgentInto` isolate each turn in try/catch → a
  failing turn shows a visible `⚠ could not render turn N` marker instead of
  blanking the rest. (Surfacing, not silent-skipping — the raw text stays
  visible.) Verified: 5000 `>` + 100k `[` render in <1 s and the next normal turn
  still renders.
