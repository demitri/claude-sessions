# claude-sessions

A local, dependency-free dashboard for Claude Code sessions. See `README.md` for usage.

## Agent rules

- Read `AI/START_HERE.md` first — mandatory session-start orientation.
- Keep `AI/` files current proactively — update `START_HERE.md`, `TODO.md`, and
  topic files whenever work changes project state (phase, layout, decisions,
  dependencies, scope), without being asked.
- `AI/START_HERE.md` stays concise — it is a table of contents; detail goes in
  topic-specific `AI/` files.

## Project specifics

- Prefer the Python standard library. The "no install needed, just run it"
  property is a core design goal, so stdlib is the default and the bar for a new
  dependency is high — reach for one only when it genuinely earns its place, and
  don't contort the code to avoid one. (Note: number/time formatting in the
  dashboard happens client-side in the page's JavaScript, so Python libs like
  `humanize` can't reach it.)
- The session-file format (`~/.claude/projects/*/*.jsonl`) is an undocumented
  Claude Code internal. Parsing is defensive by design: unknown fields are
  ignored, malformed lines are skipped per-line, never the whole file. When a
  field is missing, fall back (e.g. file mtime for timestamps) rather than crash
  — this is the one place where "skip the bad line" is correct, because the
  format is external and unversioned. Document any new field reliance in
  `AI/dashboard.md`.
