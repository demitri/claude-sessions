# transcript-on-disk search (design — not built)

**Status:** designed 2026-07-07, not built. Ready to implement in a fresh
session. This is a full-text search *across every transcript on disk*, distinct
from the dashboard's existing metadata filter (which only matches the fields
already loaded: project / title / preview / branch / id / model). Here you grep
the actual conversation content and get back matching sessions with **metadata
highlights + local snippet context**, each snippet deep-linked to the matching
turn in the transcript reader.

## Why it fits (feasibility — already assessed)

- **Corpus is greppable.** ~827 MB across ~1804 `.jsonl` files (that count
  includes sub-agent + sidecar files; ~270 are real interactive sessions).
  Largest single transcript ~25 MB; base64 blobs are rare. A per-query scan is a
  ~1 s operation cold, faster warm (OS page cache). **No index / DB needed for
  v1.** Corpus is bounded by `cleanupPeriodDays` retention, so it doesn't grow
  unbounded (caveat: the user can set the window huge — this machine is `99999`).
- **Deep-linking to a matched turn is nearly free.** Turn nodes already get a
  stable id `d.id='t'+t.i` (`turnNode`, in `TRANSCRIPT_PAGE`), where `t.i` is a
  0-based index over surviving turns. The reader already scrolls to nodes and
  flashes them (`flash()`, `scrollIntoView`). Only missing piece: honor
  `location.hash` on load to jump to `#t<idx>` (see checklist step 5).
- **The scope model already exists.** The transcript viewer's in-page search has
  a toggle *"Also search tool input/output, thinking and system markers"*
  (`class="scope"` in `TRANSCRIPT_PAGE`). Corpus search should mirror it: default
  = user + assistant text; opt-in "deep" = tool i/o, thinking, system markers.
- **Plumbing to reuse:** `parse_transcript(path)` (typed turns), `_part`/`_parts`
  (typed content parts), `_TRANSCRIPT_CACHE` (keyed `(mtime, size, sub_sig)`),
  `_json(obj, gz=True)` (gzipped responses), exact-path routing in `do_GET`.

## Backend

New endpoint, exact-path dispatched in `do_GET` (alongside `/api/sessions`,
`/api/session`, `/api/flag`):

    GET /api/search?q=<query>&scope=<default|deep>  →  {results:[...], truncated, scanned, matched}

**Two-stage scan (this is the crux):**

1. **Raw-byte prefilter — a *correct superset*, no parsing.** For each `.jsonl`,
   read the raw bytes and test for the **JSON-encoded form** of the query:
   `needle = json.dumps(q)[1:-1]` (strips the surrounding quotes). This matches
   how text actually sits escaped in the file — `"` → `\"`, backslashes, and
   non-ASCII `\uXXXX` — so it never misses a real match the way a naive raw
   substring would. Case-insensitivity: lowercase both sides (or scan twice);
   keep it simple in v1. Files with no raw hit are skipped without ever being
   JSON-parsed. **This is the no-silent-skip linchpin — comment it and add a
   test** (a query containing a quote / non-ASCII char must still find its match).
2. **Parse only the hit files** with `parse_transcript()` and walk the typed
   turns/parts to produce accurate snippets + the turn index. Match against the
   *parsed text* (not raw bytes) so offsets and context are clean.

**Scope:** default searches user + assistant text parts. `scope=deep` also
searches tool_use input, tool_result output, thinking, and system markers.
Base64 image/document data is **excluded by construction** (you search parsed
text parts, not the `data:` blobs) — state this explicitly in a comment, don't
let it be a silent side effect.

**Result model** (per matching session):

    { session: { id, project, title, updated_ts, model, branch, out_tokens,
                 open, resume },          # metadata highlights (reuse parse_session fields)
      hit_count: N,
      hits: [ { turn_index, role, ts, before, match, after } ] }   # capped ~5/session

- `before`/`after` = ~120 chars of context each side of the match, whitespace
  collapsed (reuse the `_clean_preview` idea). `match` = the matched span.
- **Cap per session (~5 hits), but never silently.** Return `hit_count` (total)
  so the UI can say "3 more matches" — a silent top-N truncation reads as
  "that's all of them." Same for a global file/result cap if you add one: return
  `truncated`/`scanned`/`matched` counts and surface them.
- **Rank** by recency (default; matches the dashboard) or `hit_count`.

**Coverage:** include sub-agent transcripts — they hold real content. (They're
the `<agentId>.jsonl` files under the per-session sub-agent dirs that
`parse_transcript` already knows about via `sub_sig`.)

**Latency:** ~1 s cold on ~800 MB, faster warm. v1 = synchronous + a spinner. If
it bites later: a per-file searchable-text cache keyed `(mtime, size)` (same
pattern as `_CACHE`/`_TRANSCRIPT_CACHE`), or streamed/chunked results. Don't
build the cache pre-emptively.

## Frontend

- **Search-mode toggle** on the existing filter box (`#q`): **meta** (today's
  instant client-side filter — keep as default) vs **transcripts** (Enter runs
  `/api/search`). Don't make transcript search fire on every keystroke.
- **Results view** replaces the table with session cards — reuse the existing
  metadata row — each followed by its snippet list: match wrapped in `<mark>`,
  context each side, and a link to `/session?id=<id>#t<turn_index>` (for a
  sub-agent hit, `&agent=<agentId>#t<idx>`). Show "+N more matches" when capped.
- **Deep-link handler in `TRANSCRIPT_PAGE`:** on load, if `location.hash` is
  `#t<idx>`, scroll to that turn node and `flash()` it (reuse the existing
  jump/flash machinery). This is the only change to the transcript page itself.

## No-silent-skip invariants (call these out in review)

1. The JSON-encoded-query prefilter must be a **guaranteed superset** — test it
   with quote/backslash/non-ASCII queries. If ever uncertain, fall back to
   full-parse scan rather than risk a miss.
2. Base64 exclusion is an **explicit** decision, commented — not a side effect.
3. Any cap (per-session hits, total files/results) is **surfaced** in the
   response and shown in the UI — never a silent truncation.

## Implementation checklist (suggested order)

1. Backend `search(q, scope)` helper: glob `PROJECTS/*/*.jsonl` (+ sub-agent
   files), raw-byte prefilter, parse hits, build result model. Unit-test the
   prefilter superset property.
2. Wire `elif u.path == "/api/search":` in `do_GET` → `self._json(search(...), gz=True)`.
   Validate/escape `q`; empty `q` → empty results (not a full dump).
3. Frontend mode toggle + results renderer (new render path; don't disturb the
   metadata filter).
4. Snippet → transcript deep-link URLs (`#t<idx>`, `&agent=` for sub-agents).
5. `TRANSCRIPT_PAGE`: `location.hash` → scroll + `flash` on load.
6. Tests + a silent-skip review pass (this touches a parser/scanner — mandatory).

## Key code references (grep the symbol — line numbers drift)

- Routing: `def do_GET` → the `u.path == "/api/sessions"` / `/api/session` /
  `/api/flag` chain; `_json(obj, gz=...)`.
- Transcript parse: `def parse_transcript`, `_part`, `_parts`, `_TRANSCRIPT_CACHE`
  (key includes `sub_sig` for sub-agent dirs).
- Turn anchor + jump: `d.id='t'+t.i` (`turnNode`), `function flash`,
  `scrollIntoView`; scope toggle `class="scope"` — all in `TRANSCRIPT_PAGE`.
- Metadata for result cards: `parse_session` fields, `project_short`, `resume_cmd`.
- Corpus glob: `glob.glob(os.path.join(PROJECTS, "*", "*.jsonl"))`.

## Open decisions (pick during build)

- Case sensitivity (v1: case-insensitive substring). Regex support = later.
- Whether the mode toggle is a chip or a segmented control next to `#q`.
- Whether "transcripts" mode also keeps the project/recency filters ANDed in
  (probably yes — filter the searched set first, then scan).
