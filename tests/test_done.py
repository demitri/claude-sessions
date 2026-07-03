#!/usr/bin/env python3
"""Isolated tests for the `--done` CLI, refresh_flags, and the done/flag invariant.

Sets $HOME to a throwaway dir BEFORE importing claude-status.py, so FLAGS_PATH /
PROJECTS resolve under the sandbox and the real ~/.config and ~/.claude are never
touched. Stdlib only. Run:  python3 tests/test_done.py   (exit 0 = all passed).
"""
import os, json, tempfile, importlib.util, time, glob, shutil

HOME = tempfile.mkdtemp(prefix="cs-test-home-")
os.environ["HOME"] = HOME
os.environ.pop("CLAUDE_CODE_SESSION_ID", None)

# fake corpus: two projects, three sessions
PROJ = os.path.join(HOME, ".claude", "projects")
os.makedirs(os.path.join(PROJ, "proj-a"))
os.makedirs(os.path.join(PROJ, "proj-b"))
SIDS = ["11111111-aaaa-bbbb-cccc-000000001234",
        "22222222-dddd-eeee-ffff-000000005678",
        "33333333-9999-8888-7777-000000009abc"]
line = json.dumps({"type": "user", "cwd": "/x", "message": {"content": "hi"},
                   "timestamp": "2026-06-01T10:00:00Z"}) + "\n"
open(os.path.join(PROJ, "proj-a", SIDS[0] + ".jsonl"), "w").write(line)
open(os.path.join(PROJ, "proj-a", SIDS[1] + ".jsonl"), "w").write(line)
open(os.path.join(PROJ, "proj-b", SIDS[2] + ".jsonl"), "w").write(line)

# import claude-status.py resolved relative to this test file (works in any clone)
SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "claude-status.py")
spec = importlib.util.spec_from_file_location("cs", SRC)
cs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cs)

ok = 0
fail = 0
def check(name, cond):
    global ok, fail
    print(("  PASS " if cond else "  FAIL ") + name)
    ok += cond; fail += (not cond)

print("=== A. no-arg via $CLAUDE_CODE_SESSION_ID ===")
os.environ["CLAUDE_CODE_SESSION_ID"] = SIDS[0]
cs.mark_done_cli(None)
d = json.load(open(cs.FLAGS_PATH))
check("current session marked done", d.get(SIDS[0], {}).get("done") is not None)
check("only that session marked", set(d) == {SIDS[0]})

print("=== B. resolve by last-4 (tail) ===")
cs.mark_done_cli("1234")  # tail of SIDS[0]
check("tail 1234 -> SIDS[0]", SIDS[0] in json.load(open(cs.FLAGS_PATH)))
cs.mark_done_cli(SIDS[2])  # full id
check("full id -> SIDS[2]", SIDS[2] in json.load(open(cs.FLAGS_PATH)))

print("=== C. error paths ===")
try:
    cs.resolve_session_id("nomatch-zzzz"); check("no-match raises", False)
except SystemExit as e:
    check("no-match raises with message", "No local session" in str(e))
# ambiguous: create two sessions sharing the SAME last-4 suffix "beef"
open(os.path.join(PROJ, "proj-a", "aaaaaaaa-1111-2222-3333-00000000beef.jsonl"), "w").write(line)
open(os.path.join(PROJ, "proj-b", "bbbbbbbb-4444-5555-6666-00000000beef.jsonl"), "w").write(line)
try:
    cs.resolve_session_id("beef"); check("ambiguous raises", False)
except SystemExit as e:
    check("ambiguous raises with list", "Ambiguous" in str(e))
os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
try:
    cs.mark_done_cli(None); check("no-id raises", False)
except SystemExit as e:
    check("no-id raises with message", "No session id" in str(e))

print("=== F. leading '#' tolerated + done clears a reopen flag ===")
os.environ["CLAUDE_CODE_SESSION_ID"] = SIDS[1]
with cs.flags_write_lock():           # pre-seed a reopen flag on SIDS[1]
    cs.refresh_flags(); m = cs.FLAGS.get(SIDS[1], {}); m["flag"] = 1.0
    cs.FLAGS[SIDS[1]] = m; cs.save_flags(cs.FLAGS)
cs.mark_done_cli("#5678")             # '#'+tail of SIDS[1]
d = json.load(open(cs.FLAGS_PATH))
check("'#5678' resolved to SIDS[1] and marked done", d.get(SIDS[1], {}).get("done") is not None)
check("done cleared the stale reopen flag", "flag" not in d.get(SIDS[1], {}))
os.environ.pop("CLAUDE_CODE_SESSION_ID", None)

print("=== G. empty-string token falls back to env (the quoted \"$ARGUMENTS\" case) ===")
os.environ["CLAUDE_CODE_SESSION_ID"] = SIDS[2]
cs.mark_done_cli("")                  # what `--done ""` produces
check("empty token used current session", SIDS[2] in json.load(open(cs.FLAGS_PATH)))
os.environ.pop("CLAUDE_CODE_SESSION_ID", None)

print("=== D. refresh_flags picks up an external write (mtime reload) ===")
# simulate the running server: it has FLAGS in memory; another process writes.
cs.FLAGS = {}; cs._flags_mtime = None
with cs._flags_lock:
    cs.refresh_flags()  # loads current disk state
before = dict(cs.FLAGS)
# external writer adds a brand-new entry directly to the file
ext = dict(before); ext["44444444-0000-0000-0000-00000000ffff"] = {"flag": 1.0}
tmp = cs.FLAGS_PATH + ".ext"
json.dump(ext, open(tmp, "w"))
os.replace(tmp, cs.FLAGS_PATH)
os.utime(cs.FLAGS_PATH, (time.time() + 5, time.time() + 5))  # ensure mtime differs
with cs._flags_lock:
    cs.refresh_flags()
check("external entry now visible in FLAGS", "44444444-0000-0000-0000-00000000ffff" in cs.FLAGS)
check("prior entries retained", all(k in cs.FLAGS for k in before))

print("=== E. save_flags updates _flags_mtime (no needless reload) ===")
with cs._flags_lock:
    cs.refresh_flags()
m1 = cs._flags_mtime
with cs._flags_lock:
    cs.FLAGS["55555555-1111-1111-1111-111111111111"] = {"done": 2.0}
    cs.save_flags(cs.FLAGS)
m2 = cs._flags_mtime
check("_flags_mtime advanced after save", m2 is not None and m2 != m1)
with cs._flags_lock:
    cs.refresh_flags()  # should be a no-op (our own write already recorded)
check("no reload needed after own save (mtime unchanged)", cs._flags_mtime == m2)

print("=== H. load_flags collapses legacy both-set rows (never both) ===")
both = {
  "later-done-0000-0000-0000-000000000000": {"flag": 100.0, "done": 200.0},  # done newer
  "later-flag-0000-0000-0000-000000000000": {"flag": 300.0, "done": 150.0},  # flag newer
  "bad-ts-0000-0000-0000-0000-000000000000": {"flag": "x", "done": "y"},     # unparseable
}
tmp = cs.FLAGS_PATH + ".seed"
json.dump(both, open(tmp, "w")); os.replace(tmp, cs.FLAGS_PATH)
r = cs.load_flags()
check("done newer -> keep done only", r["later-done-0000-0000-0000-000000000000"] == {"done": 200.0})
check("flag newer -> keep flag only", r["later-flag-0000-0000-0000-000000000000"] == {"flag": 300.0})
check("bad ts -> keep flag (visible, drop done)", set(r["bad-ts-0000-0000-0000-0000-000000000000"]) == {"flag"})

print("\n%d passed, %d failed" % (ok, fail))
shutil.rmtree(HOME, ignore_errors=True)
raise SystemExit(1 if fail else 0)
