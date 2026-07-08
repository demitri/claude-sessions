#!/usr/bin/env python3
"""Isolated tests for transcript-on-disk full-text search (AI/search.md).

Sets $HOME to a throwaway dir BEFORE importing claude-status.py, builds a small
crafted corpus, and exercises the no-silent-skip invariants: the raw-byte
prefilter must be a *guaranteed superset* (quote / backslash / non-ASCII /
cross-part matches must still be found), base64 blobs must be excluded by
construction, scope must gate tool/thinking/system text, and every cap must be
surfaced (hit_count, truncated). Stdlib only. Run:  python3 tests/test_search.py
"""
import os, json, tempfile, importlib.util

HOME = tempfile.mkdtemp(prefix="cs-test-search-")
os.environ["HOME"] = HOME
os.environ.pop("CLAUDE_CODE_SESSION_ID", None)

PROJ = os.path.join(HOME, ".claude", "projects")
os.makedirs(os.path.join(PROJ, "proj-a"))
os.makedirs(os.path.join(PROJ, "proj-b"))

TS = "2026-06-01T10:00:00Z"


def uid(tail):
    return "%08d-aaaa-bbbb-cccc-%012d" % (tail, tail)


def write_session(project, sid, turns, cwd="/work/thing"):
    """turns: list of raw record dicts (already shaped). Adds cwd/timestamp."""
    path = os.path.join(PROJ, project, sid + ".jsonl")
    with open(path, "w") as fh:
        for t in turns:
            rec = dict(t)
            rec.setdefault("cwd", cwd)
            rec.setdefault("timestamp", TS)
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return path


def user(text):
    return {"type": "user", "message": {"content": text}}


def user_parts(parts):
    return {"type": "user", "message": {"content": parts}}


def asst(parts):
    return {"type": "assistant", "message": {"role": "assistant",
            "model": "claude-x", "content": parts, "usage": {"output_tokens": 3}}}


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


def run(q, scope="default", project=None):
    return cs.search_corpus(q, cs.SearchScope.parse(scope), project)


def sessions_for(q, **kw):
    return {r["session"]["id"] for r in run(q, **kw)["results"]}


# --- A. _query_needles: encodings are the in-file JSON form, lowercased -------
print("=== A. prefilter needles (the superset linchpin) ===")
check("plain ascii -> literal", cs._query_needles("Hello") == [b"hello"])
check("quote -> escaped \\\"", cs._query_needles('say "hi"') == [b"say", b'\\"hi\\"'])
check("backslash -> escaped \\\\", cs._query_needles(r"C:\dir") == [rb"c:\\dir"])
check("multiword -> AND of tokens", cs._query_needles("foo bar") == [b"foo", b"bar"])
check("non-ascii -> None (parse-all fallback)", cs._query_needles("café") is None)

# --- B. superset: matches that would trip a naive raw scan -------------------
print("=== B. superset property (no silent miss) ===")
# cross-part: two text parts join to 'hello world' in parsed text; the raw file
# never contains that literal substring (it spans two JSON string values).
write_session("proj-a", uid(1), [asst([{"type": "text", "text": "hello"},
                                        {"type": "text", "text": "world"}])])
raw = open(os.path.join(PROJ, "proj-a", uid(1) + ".jsonl"), "rb").read()
check("cross-part literal absent from raw bytes", b"hello world" not in raw)
check("cross-part match still found", uid(1) in sessions_for("hello world"))
# quoted text: stored escaped as \" in the file
write_session("proj-a", uid(2), [user('please say "hi there" now')])
check("quoted-string query found", uid(2) in sessions_for('say "hi there"'))
# backslash path: stored as C:\\logs in the file
write_session("proj-a", uid(3), [user(r"open C:\logs\app now")])
check("backslash query found", uid(3) in sessions_for(r"c:\logs"))
# non-ascii, mixed case: parse-all fallback + Unicode case-fold in stage 2
write_session("proj-a", uid(4), [user("le café est ouvert")])
check("non-ascii case-insensitive found", uid(4) in sessions_for("CAFÉ"))

# --- C. scope gates tool/thinking/system text -------------------------------
print("=== C. scope (default vs deep) ===")
write_session("proj-b", uid(10), [
    asst([{"type": "text", "text": "visible answer"},
          {"type": "thinking", "thinking": "zzsecretthought"},
          {"type": "tool_use", "id": "tu1", "name": "Bash",
           "input": {"command": "zztoolarg run"}}]),
    {"type": "system", "subtype": "note", "content": "zzsystemmarker here",
     "timestamp": TS, "cwd": "/work/thing"},
])
check("default finds assistant text", uid(10) in sessions_for("visible answer"))
check("default MISSES thinking", uid(10) not in sessions_for("zzsecretthought"))
check("default MISSES tool input", uid(10) not in sessions_for("zztoolarg"))
check("default MISSES system marker", uid(10) not in sessions_for("zzsystemmarker"))
check("deep finds thinking", uid(10) in sessions_for("zzsecretthought", scope="deep"))
check("deep finds tool input", uid(10) in sessions_for("zztoolarg", scope="deep"))
check("deep finds tool name", uid(10) in sessions_for("Bash", scope="deep"))
check("deep finds system marker", uid(10) in sessions_for("zzsystemmarker", scope="deep"))

# --- D. base64 blobs excluded by construction -------------------------------
print("=== D. base64 exclusion (explicit, even in deep scope) ===")
write_session("proj-b", uid(11), [user_parts([
    {"type": "text", "text": "an image follows"},
    {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                  "data": "AAAAzzblobneedlezz9999"}}])])
check("blob needle NOT matched (default)", uid(11) not in sessions_for("zzblobneedle"))
check("blob needle NOT matched (deep)", uid(11) not in sessions_for("zzblobneedle", scope="deep"))
check("surrounding text still matched", uid(11) in sessions_for("image follows"))

# --- E. per-session cap surfaced via hit_count; turn_index is accurate -------
print("=== E. caps are surfaced, never silent ===")
many = [asst([{"type": "text", "text": "needle zz%02d here" % i}]) for i in range(7)]
write_session("proj-b", uid(12), many)
res = run("needle")
row = next(r for r in res["results"] if r["session"]["id"] == uid(12))
check("hit_count reports the TRUE total (7)", row["hit_count"] == 7)
check("hits list capped at 5", len(row["hits"]) == 5)
check("turn_index matches dense turn order", [h["turn_index"] for h in row["hits"]] == [0, 1, 2, 3, 4])
check("snippet keeps source casing in match", row["hits"][0]["match"] == "needle")

# --- F. sub-agent transcripts searched and grouped under the parent ----------
print("=== F. sub-agent coverage + deep-link agent id ===")
parent = uid(20)
write_session("proj-a", parent, [user("kick off the agent")])
subdir = os.path.join(PROJ, "proj-a", parent, "subagents")
os.makedirs(subdir)
AID = "a1b2c3d4e5f6"
with open(os.path.join(subdir, "agent-%s.jsonl" % AID), "w") as fh:
    fh.write(json.dumps(asst([{"type": "text", "text": "zzsubagentfinding"}]) |
                        {"cwd": "/work/thing", "timestamp": TS}) + "\n")
res = run("zzsubagentfinding")
prow = next((r for r in res["results"] if r["session"]["id"] == parent), None)
check("sub-agent hit grouped under parent session", prow is not None)
check("hit carries the agent id for deep-linking", prow and prow["hits"][0].get("agent") == AID)

# --- G. empty query & project filter ----------------------------------------
print("=== G. empty query + project filter ===")
e = run("")
check("empty query -> no results, nothing scanned", e["results"] == [] and e["scanned"] == 0)
pa = run("needle", project="proj-b")   # uid(12) is in proj-b
check("project filter restricts the set", all(r["session"]["project"] == "proj-b"
                                              for r in pa["results"]))
r_all = sessions_for("the")
r_b = sessions_for("the", project="proj-a")
check("project filter is a strict subset", r_b <= r_all and r_b != r_all or not r_all)

# --- H. skips are surfaced (unreadable file / unlinkable sub-agent id) -------
print("=== H. errors surfaced, never silent ===")
# non-hex sub-agent id: content can't be deep-linked, so it's counted, not dropped
badparent = uid(30)
write_session("proj-b", badparent, [user("parent with a weird agent")])
bsub = os.path.join(PROJ, "proj-b", badparent, "subagents")
os.makedirs(bsub)
with open(os.path.join(bsub, "agent-NOTHEXZZ.jsonl"), "w") as fh:
    fh.write(json.dumps(asst([{"type": "text", "text": "zzhidden"}]) |
                        {"cwd": "/w", "timestamp": TS}) + "\n")
res = run("zzhidden")
check("non-hex sub-agent counted in errors", res["errors"] >= 1)
check("non-hex sub-agent yields no (broken) result", badparent not in {r["session"]["id"] for r in res["results"]})

if os.geteuid() != 0:   # root bypasses file perms; skip there
    unreadable = write_session("proj-b", uid(31), [user("zzunreadable content")])
    os.chmod(unreadable, 0)
    res = run("zzunreadable")
    check("unreadable file counted in errors", res["errors"] >= 1)
    check("unreadable file not returned as a hit", uid(31) not in {r["session"]["id"] for r in res["results"]})
    os.chmod(unreadable, 0o644)
else:
    print("  SKIP unreadable-file check (running as root)")

# parent metadata unreadable *after* hits are found (e.g. pruned mid-scan while
# sub-agent files still match): the found hits must not vanish uncounted.
write_session("proj-a", uid(32), [asst([{"type": "text", "text": "zzorphanmatch"}])])
_real_ps = cs.parse_session
cs.parse_session = lambda p: None            # simulate the metadata read failing
try:
    res = run("zzorphanmatch")
finally:
    cs.parse_session = _real_ps
check("hits found but metadata gone -> dropped, and counted", res["errors"] >= 1)
check("no half-built result emitted", res["results"] == [])

print("\n%d passed, %d failed" % (ok, fail))
raise SystemExit(1 if fail else 0)
