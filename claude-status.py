#!/usr/bin/env python3
"""
claude-status — a local dashboard for your Claude Code sessions.

Scans ~/.claude/projects/ and serves a sortable / filterable web page showing
open sessions grouped by project directory, with stats (started, last active,
message counts, model, git branch, tokens, size) and one-click resume commands.

Usage:
    python3 claude-status.py            # serve on http://127.0.0.1:7878 and open a browser
    python3 claude-status.py --port 9000
    python3 claude-status.py --no-open  # don't auto-open the browser
    python3 claude-status.py --once     # write a static index.html next to this script and exit

Stdlib only. No external dependencies.
"""
import argparse
import calendar
import contextlib
import fcntl
import glob
import gzip
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

PROJECTS = os.path.expanduser("~/.claude/projects")
SESSIONS = os.path.expanduser("~/.claude/sessions")  # <pid>.json per live process
FLAGS_PATH = os.path.expanduser("~/.config/claude-sessions/flags.json")
_flags_lock = threading.Lock()
_flags_mtime = None  # mtime of flags.json at last load; drives refresh_flags()


MARK_KINDS = ("flag", "done")  # flag = reopen after restart; done = finished/dismissed


def load_flags():
    """Persistent per-session marks: {sessionId: {"flag": ts, "done": ts}}.
    Key present = mark set. Server-side JSON so marks survive a reboot and are
    shared across browsers. Legacy {"ts": …} entries are read as a flag.
    """
    try:
        with open(FLAGS_PATH) as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(d, dict):
        return {}
    out = {}
    for sid, v in d.items():
        if not isinstance(v, dict):
            continue
        marks = {k: v[k] for k in MARK_KINDS if k in v}
        if not marks and "ts" in v:  # legacy: bare {ts} meant flagged
            marks = {"flag": v["ts"]}
        if "flag" in marks and "done" in marks:
            # invariant: never both — collapse legacy rows to the more recently
            # set mark. On unparseable timestamps keep it visible (drop done), so
            # a session with pending work is never silently hidden.
            try:
                # strict '>' so a tie keeps the flag (visible), consistent with
                # the unparseable-timestamp branch below
                drop = "flag" if float(marks["done"]) > float(marks["flag"]) else "done"
            except (TypeError, ValueError):
                drop = "done"
            marks.pop(drop, None)
        if marks:
            out[sid] = marks
    return out


def save_flags(d):
    global _flags_mtime
    os.makedirs(os.path.dirname(FLAGS_PATH), exist_ok=True)
    # Unique temp per write: the dashboard process and the `--done` CLI are
    # separate writers, so a shared temp path could be clobbered / vanish before
    # the rename. mkstemp gives a private name in the same dir; os.replace is the
    # atomic swap.
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(FLAGS_PATH), prefix=".flags-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(d, fh, indent=2)
        os.replace(tmp, FLAGS_PATH)  # atomic replace
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    try:  # record our own write so refresh_flags() won't reload it needlessly
        _flags_mtime = os.stat(FLAGS_PATH).st_mtime
    except OSError:
        pass


@contextlib.contextmanager
def flags_write_lock():
    """Cross-process exclusive lock for a flags.json read-modify-write.

    The dashboard process and the `--done` CLI are independent writers; without
    this a lost update is possible (A reads, B writes, A writes over B). *Reads*
    don't need it — save_flags' atomic os.replace means a reader always sees a
    whole file — only the read-modify-write critical section does. `flock` on a
    sidecar lockfile (POSIX; macOS/Linux, this tool's targets)."""
    os.makedirs(os.path.dirname(FLAGS_PATH), exist_ok=True)
    lf = open(FLAGS_PATH + ".lock", "w")
    try:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
        lf.close()


def refresh_flags():
    """Reload FLAGS from disk if flags.json changed since we last loaded it.

    FLAGS is held in memory, but external writers (`--done`, another dashboard
    process, a hand edit) and the UI must share one source of truth — so before
    reading or mutating we re-sync from disk when the file's mtime moved. Callers
    hold `_flags_lock`; this function deliberately does NOT lock, so the POST path
    (which already holds it) can call it without deadlocking. Cheap: one stat,
    reload only on change.
    """
    global FLAGS, _flags_mtime
    try:
        m = os.stat(FLAGS_PATH).st_mtime
    except OSError:
        return  # no file yet — keep current in-memory state
    if m != _flags_mtime:
        FLAGS = load_flags()
        _flags_mtime = m


FLAGS = {}
refresh_flags()  # stat-then-load (same order as steady state) — avoids stamping a
#                  post-write mtime onto pre-write contents at startup
NAMED_RE = re.compile(r'named this session "([^"]+)"')
WRAPPER_PREFIXES = ("<system-reminder>", "<local-command", "<command-name>",
                    "<command-message>", "Caveat:", "<local-command-caveat>")

# cache: path -> (mtime, size, parsed_dict)
_CACHE = {}


def _first_text(content):
    """Pull plain text out of a message 'content' field (str or list of parts)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                return part.get("text", "")
    return ""


def _clean_preview(text):
    t = (text or "").strip()
    # peel a leading wrapper block if present
    if t.startswith("<system-reminder>"):
        end = t.find("</system-reminder>")
        if end != -1:
            t = t[end + len("</system-reminder>"):].strip()
    t = t.replace("\n", " ").strip()
    return t


def _to_ts(s):
    if not s:
        return None
    try:
        # ISO 8601 UTC (trailing Z). timegm treats the struct as UTC;
        # mktime would (wrongly) treat it as local time and skew the result.
        return calendar.timegm(time.strptime(s.split(".")[0].replace("Z", ""),
                                              "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return None


def resume_cmd(cwd, sid):
    """The one place the resume command is built (dashboard + transcript page).
    shlex.quote: cwd may contain spaces/quotes/$ — a bare "cd \"%s\"" breaks."""
    return "cd %s && claude --resume %s" % (shlex.quote(cwd), sid)


def parse_session(path):
    """Parse one .jsonl session file into a stats dict (cached by mtime+size)."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = path
    cached = _CACHE.get(key)
    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return cached[2]

    sid = os.path.basename(path)[:-6]
    cwd = None
    branch = None
    version = None
    entrypoint = None
    model = None
    title = None
    started_ts = None
    updated_ts = None
    user_msgs = 0
    asst_msgs = 0
    out_tokens = 0
    ctx_tokens = 0  # approx context size = latest input+cache tokens
    preview = ""
    preview_locked = False
    to_ts = _to_ts

    try:
        with open(path, "r", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if not isinstance(o, dict):
                    continue

                if cwd is None and o.get("cwd"):
                    cwd = o["cwd"]
                if branch is None and o.get("gitBranch"):
                    branch = o["gitBranch"]
                if version is None and o.get("version"):
                    version = o["version"]
                if entrypoint is None and o.get("entrypoint"):
                    entrypoint = o["entrypoint"]

                ts = to_ts(o.get("timestamp"))
                if ts:
                    if started_ts is None:
                        started_ts = ts
                    updated_ts = ts

                typ = o.get("type")
                msg = o.get("message") if isinstance(o.get("message"), dict) else {}

                if typ == "user":
                    user_msgs += 1
                    txt = _first_text(msg.get("content"))
                    if title is None:
                        m = NAMED_RE.search(txt)
                        if m:
                            title = m.group(1)
                    if not preview_locked:
                        cleaned = _clean_preview(txt)
                        if cleaned and not cleaned.startswith(WRAPPER_PREFIXES):
                            preview = cleaned[:400]
                            preview_locked = True
                        elif cleaned and not preview:
                            preview = cleaned[:400]
                elif typ == "assistant":
                    asst_msgs += 1
                    if msg.get("model"):
                        model = msg["model"]
                    usage = msg.get("usage") or {}
                    out_tokens += int(usage.get("output_tokens") or 0)
                    ctx = (int(usage.get("input_tokens") or 0)
                           + int(usage.get("cache_read_input_tokens") or 0)
                           + int(usage.get("cache_creation_input_tokens") or 0))
                    if ctx:
                        ctx_tokens = ctx
    except OSError:
        return None

    if started_ts is None:
        started_ts = st.st_mtime
    if updated_ts is None:
        updated_ts = st.st_mtime

    cwd = cwd or "?"
    short = project_short(cwd)
    resume = resume_cmd(cwd, sid)

    result = {
        "id": sid,
        "cwd": cwd,
        "project": short,
        "title": title or "",
        "preview": preview or "(no user message)",
        "started_ts": started_ts,
        "updated_ts": updated_ts,
        "msgs": user_msgs + asst_msgs,
        "user_msgs": user_msgs,
        "asst_msgs": asst_msgs,
        "model": short_model(model),
        "branch": branch or "",
        "version": version or "",
        "entrypoint": entrypoint or "",
        "size_bytes": st.st_size,
        "out_tokens": out_tokens,
        "ctx_tokens": ctx_tokens,
        "resume": resume,
    }
    _CACHE[key] = (st.st_mtime, st.st_size, result)
    return result


def project_short(cwd):
    """Human-friendly short project name; keeps one parent for grouped repos."""
    parts = [p for p in cwd.split("/") if p]
    if not parts:
        return cwd
    # group families one level deeper (e.g. thehighlighter/tesseretica)
    for anchor in ("GitHub", "Repositories", "repositories"):
        if anchor in parts:
            i = parts.index(anchor)
            tail = parts[i + 1:]
            if len(tail) >= 2:
                return "/".join(tail[-2:]) if tail[-2] in ("thehighlighter", "trillianverse") else tail[-1]
            if tail:
                return tail[-1]
    return parts[-1]


def short_model(m):
    if not m:
        return ""
    m = m.replace("claude-", "")
    return m


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:  # alive, just not ours
        return True
    except (OSError, ValueError, TypeError):
        return False


def _rss_for_pids(pids):
    """{pid -> RSS KB} via a single batched ps call. Empty dict on failure."""
    pids = [int(p) for p in pids if p is not None]
    if not pids:
        return {}
    try:
        out = subprocess.run(["ps", "-o", "pid=,rss=", "-p", ",".join(map(str, pids))],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return {}
    res = {}
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            try:
                res[int(parts[0])] = int(parts[1])
            except ValueError:
                pass
    return res


def open_sessions():
    """Map sessionId -> {status, rss_kb} for currently-running Claude processes.

    Each ~/.claude/sessions/<pid>.json records one running process; a session is
    "open" iff its id appears there with a still-alive PID (stale files for
    crashed processes are filtered by the pid_alive check). RSS is read in one
    batched ps call. See AI/dashboard.md.
    """
    recs = []
    for f in glob.glob(os.path.join(SESSIONS, "*.json")):
        try:
            with open(f, errors="replace") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(d, dict):
            continue
        sid = d.get("sessionId")
        pid = d.get("pid")
        if not sid or pid is None or not pid_alive(pid):
            continue
        recs.append((sid, int(pid), d.get("status") or ""))
    rss = _rss_for_pids([pid for _, pid, _ in recs])
    out = {}
    for sid, pid, st in recs:
        # if a session has multiple live procs, prefer a non-idle status
        if sid not in out or (st and st != "idle"):
            out[sid] = {"status": st, "rss_kb": rss.get(pid, 0)}
    return out


def collect():
    with _flags_lock:
        refresh_flags()  # pick up external marks (e.g. `--done`) since last scan
        # snapshot under the lock so a concurrent POST can't mutate FLAGS mid-scan
        flags_snap = {sid: dict(m) for sid, m in FLAGS.items()}
    open_map = open_sessions()
    sessions = []
    for f in glob.glob(os.path.join(PROJECTS, "*", "*.jsonl")):
        s = parse_session(f)
        # Skip non-conversation sidecar files (ai-title, bridge-session, etc.):
        # they carry no user/assistant turns, no cwd (project "?"), and aren't
        # resumable conversations. Also skip headless/SDK runs
        # (entrypoint "sdk-cli", e.g. claude -p), which aren't interactive
        # sessions to resume. See AI/dashboard.md.
        if not s:
            continue
        if (s["user_msgs"] + s["asst_msgs"]) == 0:
            continue
        if s["entrypoint"] == "sdk-cli":
            continue
        # open/rss are process-derived and flagged is store-derived — all set per
        # request, never cached in parse_session (which keys on file mtime+size).
        info = open_map.get(s["id"])
        s["open"] = info is not None
        s["live_status"] = info["status"] if info else ""
        s["rss_kb"] = info["rss_kb"] if info else 0
        marks = flags_snap.get(s["id"], {})
        s["flagged"] = "flag" in marks
        s["done"] = "done" in marks
        sessions.append(s)
    sessions.sort(key=lambda s: s["updated_ts"], reverse=True)
    return {"generated": time.time(), "sessions": sessions}


# ---------------------------------------------------------------------------
# transcript viewer backend (see AI/transcript.md)

# Separate from _CACHE on purpose: a session path keys both parse_session and
# parse_transcript, and the two value shapes differ — one shared dict would
# serve the wrong shape to whichever ran second. Same overwrite-on-change
# pattern, with an extra subagents-dir mtime in the key:
# path -> (mtime, size, subagents_dir_mtime, result).
_TRANSCRIPT_CACHE = {}

_HEX_RE = re.compile(r"[0-9a-f]+\Z")


def _part(p):
    """One raw content part (dict) → a typed, self-describing part for the
    client. Unknown types become {kind:"unknown"} — surfaced, never dropped."""
    t = p.get("type")
    if t == "text":
        return {"kind": "text", "text": p.get("text", "")}
    if t == "thinking":
        # the field is `thinking`, not `text`
        return {"kind": "thinking", "text": p.get("thinking", "")}
    if t == "tool_use":
        return {"kind": "tool_use", "id": p.get("id", ""),
                "name": p.get("name", ""), "input": p.get("input")}
    if t == "tool_result":
        out = {"kind": "tool_result", "tool_use_id": p.get("tool_use_id", ""),
               # is_error is optional in ~half the corpus: absent must mean False
               "is_error": p.get("is_error") is True}
        c = p.get("content")
        if isinstance(c, list):
            # nested parts (text/image/tool_reference/…) run through the same
            # dispatcher — same unknown-fallback, never dropped
            out["parts"] = [_part(x) if isinstance(x, dict)
                            else {"kind": "text", "text": str(x)} for x in c]
        elif isinstance(c, str) or c is None:
            out["text"] = c or ""
        else:
            out["text"] = json.dumps(c)
        return out
    if t in ("image", "document"):
        src = p.get("source") if isinstance(p.get("source"), dict) else {}
        # only base64 sources carry data; a future source shape (e.g. url) is
        # named in source_type so the client stub can say what it couldn't show
        return {"kind": t, "media_type": src.get("media_type", ""),
                "source_type": str(src.get("type") or ""),
                "data": src.get("data", "") if src.get("type") == "base64" else ""}
    if t == "tool_reference":
        return {"kind": "tool_reference", "tool_name": p.get("tool_name", "")}
    if t == "fallback":
        fr = p.get("from") if isinstance(p.get("from"), dict) else {}
        to = p.get("to") if isinstance(p.get("to"), dict) else {}
        return {"kind": "fallback", "from_model": fr.get("model", "?"),
                "to_model": to.get("model", "?")}
    return {"kind": "unknown", "raw_type": str(t)}


def _parts(content):
    """message.content (str | list | anything) → list of typed parts."""
    if isinstance(content, str):
        return [{"kind": "text", "text": content}]
    if isinstance(content, list):
        return [_part(p) if isinstance(p, dict)
                else {"kind": "text", "text": str(p)} for p in content]
    if content is None:
        return []
    return [{"kind": "unknown", "raw_type": type(content).__name__}]


def _strip_wrappers(text):
    """Human text left after peeling leading <system-reminder> blocks; a
    command-wrapper / caveat turn strips to '' (pure wrapper — not a prompt)."""
    t = (text or "").strip()
    while t.startswith("<system-reminder>"):
        end = t.find("</system-reminder>")
        if end == -1:
            return ""
        t = t[end + len("</system-reminder>"):].strip()
    if t.startswith(WRAPPER_PREFIXES):
        return ""
    return t


def extract_human_prompt(content, parts):
    """(is_prompt, nav_label) for a `user` turn. Rule (AI/transcript.md): str
    content, or a list with a text/image/document part and NO tool_result;
    wrapper-only text is not a prompt, media-only still is. Deliberately not
    _first_text/preview logic — that would misclassify media-only prompts."""
    if isinstance(content, str):
        txt = _strip_wrappers(content)
        return (bool(txt), txt)
    kinds = [p["kind"] for p in parts]
    if "tool_result" in kinds:
        return False, ""
    txt = _strip_wrappers(" ".join(p["text"] for p in parts if p["kind"] == "text"))
    if txt:
        return True, txt
    if "image" in kinds:
        return True, "[image]"
    if "document" in kinds:
        return True, "[document]"
    return False, ""


def _harness_event(content):
    """Classify a harness-injected `user` turn that the user never typed, so the
    reader can style it distinctly instead of as a human prompt.

    Currently one kind: `task_notification` — a background-task completion notice
    (a `Bash(run_in_background)` or background `Agent`/`Task` finishing), which
    Claude Code injects as a `user`-role turn whose content opens with the literal
    `<task-notification>` tag (an XML metadata block, optionally followed by a
    Markdown body). The leading tag is the reliable, structural discriminator —
    unlike the loop/queued re-injected prompts, which carry no in-band marker (see
    AI/transcript.md; those are flagged by the top-level `isMeta` field instead)."""
    txt = content if isinstance(content, str) else _first_text(content)
    if txt and txt.lstrip().startswith("<task-notification>"):
        return "task_notification"
    return None


def _subagent_dir(session_path):
    """Where this transcript's sub-agent files live.

    Top-level session `<dir>/<sid>.jsonl` → `<dir>/<sid>/subagents`. A
    sub-agent file already lives *in* a subagents/ dir, and storage is flat
    (verified: no nested subagents/ dirs; spawnDepth-2 metas sit in the same
    dir) — so its own Agent calls resolve against its containing dir."""
    parent = os.path.dirname(session_path)
    if os.path.basename(parent) == "subagents":
        return parent
    base = session_path[:-len(".jsonl")] if session_path.endswith(".jsonl") else session_path
    return os.path.join(base, "subagents")


def _read_meta(subagent_dir, agent_id):
    """One agent's `.meta.json` → dict ({} if absent/malformed). Direct read:
    labelling a single sub-agent must not scan every meta in the dir."""
    try:
        with open(os.path.join(subagent_dir, "agent-%s.meta.json" % agent_id),
                  errors="replace") as fh:
            m = json.load(fh)
    except (OSError, ValueError):
        return {}
    return m if isinstance(m, dict) else {}


# {agentId: meta} cached by the subagents/ dir mtime. Safe because meta labels
# (agentType/description/toolUseId) are written once at spawn and never edited
# in place, while file create/delete/rename — the only events that add or drop
# an agent — bump the dir mtime. Reused by parse_transcript (linkage labels) and
# list_subagents (panel labels) so one request's two callers don't both scan.
# (A dir has no size to use as a secondary key; on the macOS/Linux targets mtime
# is sub-microsecond, so two dir changes within one tick — the only stale-serve
# window — don't occur in practice.)
_METAS_CACHE = {}


def _subagent_metas(session_path):
    """{agentId: meta} from the session's `subagents/agent-*.meta.json` files.
    One-level glob — the workflows/ subtree (unlinked background routines) is
    excluded by construction. Key sets vary: only agentType/description are
    reliable; everything else via .get()."""
    d = _subagent_dir(session_path)
    try:
        dm = os.stat(d).st_mtime
    except OSError:
        return {}
    cached = _METAS_CACHE.get(d)
    if cached and cached[0] == dm:
        return cached[1]
    out = {}
    for f in glob.glob(os.path.join(glob.escape(d), "agent-*.meta.json")):
        aid = os.path.basename(f)[len("agent-"):-len(".meta.json")]
        try:
            with open(f, errors="replace") as fh:
                m = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(m, dict):
            out[aid] = m
    _METAS_CACHE[d] = (dm, out)
    return out


def list_subagents(session_path):
    """Inventory for the transcript page's sub-agents panel: every directly
    spawned sub-agent transcript (workflows/ excluded by the one-level glob),
    labelled from its .meta.json, in last-activity (file mtime) order — i.e.
    roughly completion order, not spawn order."""
    metas = _subagent_metas(session_path)
    out = []
    for f in glob.glob(os.path.join(glob.escape(_subagent_dir(session_path)),
                                    "agent-*.jsonl")):
        aid = os.path.basename(f)[len("agent-"):-len(".jsonl")]
        if not _HEX_RE.match(aid):  # only ids the ?agent= route would accept reach the client
            continue
        try:
            st = os.stat(f)
        except OSError:
            continue
        m = metas.get(aid, {})
        out.append({"agent_id": aid,
                    "agent_type": str(m.get("agentType") or ""),
                    "description": str(m.get("description") or ""),
                    "updated_ts": st.st_mtime, "size_bytes": st.st_size})
    out.sort(key=lambda a: a["updated_ts"])
    return out


def _linked_subagents(result, subagent_dir):
    """Sub-agents spawned *by this transcript*, from its own Agent-call linkage.
    Used for the panel on a sub-agent view: storage is flat, so a sub-agent's
    children share its dir with unrelated siblings — a dir glob (list_subagents)
    would wrongly list the siblings. The parsed `subagent` objects already name
    exactly this transcript's children; stat each for the panel's time/size."""
    out, seen = [], set()
    for t in result["turns"]:
        for p in t["parts"]:
            sub = p.get("subagent") if p.get("kind") == "tool_use" else None
            if not sub or sub["agent_id"] in seen:
                continue
            seen.add(sub["agent_id"])
            entry = {"agent_id": sub["agent_id"], "agent_type": sub["agent_type"],
                     "description": sub["description"], "updated_ts": 0, "size_bytes": 0}
            try:
                st = os.stat(os.path.join(subagent_dir, "agent-%s.jsonl" % sub["agent_id"]))
                entry["updated_ts"], entry["size_bytes"] = st.st_mtime, st.st_size
            except OSError:
                pass
            out.append(entry)
    out.sort(key=lambda a: a["updated_ts"])
    return out


def parse_transcript(path):
    """One .jsonl transcript (top-level session or nested sub-agent file) →
    {"meta": {...}, "turns": [...]}, cached by (mtime, size).

    Defensive per-line parse: a malformed *line* is skipped, never the file.
    Conversation turns (user/assistant) are rendered fully; `system` records
    render iff they carry reader content (a `content` field, or api_error's
    error.formatted/message) — content-less metadata is skipped. `i` is a dense
    0-based index over surviving turns (a stable client anchor, not a source
    line number)."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    # cache key includes the subagents/ dir mtime: the embedded `subagent`
    # linkage derives from files that appear *without* the parent .jsonl
    # changing (an Agent spawn writes agent-<id>.jsonl + .meta.json first, the
    # parent gets its result turn only when the agent finishes) — dir mtime
    # bumps on file creation, so a live spawn invalidates the cache
    try:
        sub_sig = os.stat(_subagent_dir(path)).st_mtime
    except OSError:
        sub_sig = 0
    cached = _TRANSCRIPT_CACHE.get(path)
    if (cached and cached[0] == st.st_mtime and cached[1] == st.st_size
            and cached[2] == sub_sig):
        return cached[3]

    sid = os.path.basename(path)[:-len(".jsonl")]
    cwd = None
    title = None
    started_ts = None
    updated_ts = None
    user_msgs = 0
    asst_msgs = 0
    turns = []
    agent_by_tool_use = {}  # tool_use_id -> agentId (from the result turn's toolUseResult)

    try:
        with open(path, "r", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if not isinstance(o, dict):
                    continue

                if cwd is None and o.get("cwd"):
                    cwd = o["cwd"]
                ts = _to_ts(o.get("timestamp"))
                if ts:
                    if started_ts is None:
                        started_ts = ts
                    updated_ts = ts

                typ = o.get("type")
                if typ in ("user", "assistant"):
                    msg = o.get("message") if isinstance(o.get("message"), dict) else {}
                    content = msg.get("content")
                    parts = _parts(content)
                    is_prompt = False
                    label = ""
                    event = None
                    if typ == "user":
                        user_msgs += 1
                        if title is None:
                            m = NAMED_RE.search(_first_text(content))
                            if m:
                                title = m.group(1)
                        is_prompt, label = extract_human_prompt(content, parts)
                        event = _harness_event(content)
                        if not event and o.get("isMeta") is True and is_prompt:
                            # isMeta:True marks a harness-injected turn (a /loop or
                            # scheduled prompt replayed on wake, a skill preamble,
                            # "Continue from where you left off") — prose the user
                            # didn't type here. Gate on `is_prompt` so we only
                            # reclassify turns that would otherwise show as human
                            # prompts; an isMeta turn carrying a tool_result (never
                            # a prompt) is untouched, so no output can be dropped.
                            event = "injected"
                        if event:
                            # a harness injection, not typed by the user — keep it
                            # in the transcript but out of the human-prompt nav
                            is_prompt = False
                        tur = o.get("toolUseResult")
                        if isinstance(tur, dict) and tur.get("agentId"):
                            # this result turn links its tool_use to a sub-agent
                            for p in parts:
                                if p["kind"] == "tool_result" and p.get("tool_use_id"):
                                    agent_by_tool_use[p["tool_use_id"]] = str(tur["agentId"])
                    else:
                        asst_msgs += 1
                    turn = {"i": 0, "role": typ, "ts": ts, "is_prompt": is_prompt,
                            "parts": parts}
                    if is_prompt:
                        turn["label"] = label[:200]
                    if event:
                        turn["event"] = event
                    turns.append(turn)
                elif typ == "system":
                    # content-driven rule: render any system record with reader
                    # content; api_error carries its text under error.* instead
                    subtype = str(o.get("subtype") or "")
                    c = o.get("content")
                    if c is None and subtype == "api_error":
                        err = o.get("error") if isinstance(o.get("error"), dict) else {}
                        c = err.get("formatted") or err.get("message") or "API error"
                    if c is not None:
                        text = c if isinstance(c, str) else json.dumps(c)
                        turns.append({"i": 0, "role": "system", "subtype": subtype,
                                      "ts": ts, "is_prompt": False,
                                      "parts": [{"kind": "text", "text": text}]})
                    # content-less system records (turn_duration, …) are pure
                    # metadata — the content check is what makes skipping safe
                # other top-level types (attachment, mode, ai-title, …) are
                # sidecar metadata, consistent with collect()'s filtering
    except OSError:
        return None

    for i, t in enumerate(turns):
        t["i"] = i

    # sub-agent linkage: primary via toolUseResult.agentId; fallback via the
    # meta files' toolUseId (a resultless Agent call — killed/interrupted —
    # still has a transcript); unresolvable -> subagent:null (client stub)
    agent_calls = [p for t in turns for p in t["parts"]
                   if p["kind"] == "tool_use" and p.get("name") == "Agent"]
    if agent_calls:
        metas = _subagent_metas(path)
        by_tool_use = {m["toolUseId"]: aid for aid, m in metas.items()
                       if isinstance(m.get("toolUseId"), str)}
        for p in agent_calls:
            aid = agent_by_tool_use.get(p["id"]) or by_tool_use.get(p["id"])
            sub = None
            if aid and _HEX_RE.match(aid) and os.path.isfile(
                    os.path.join(_subagent_dir(path), "agent-%s.jsonl" % aid)):
                m = metas.get(aid, {})
                sub = {"agent_id": aid,
                       "agent_type": str(m.get("agentType") or ""),
                       "description": str(m.get("description") or "")}
            p["subagent"] = sub

    cwd = cwd or "?"
    is_subagent = sid.startswith("agent-")
    result = {
        "meta": {
            "id": sid,
            "project": project_short(cwd),
            "cwd": cwd,
            "title": title or "",
            "started_ts": started_ts or st.st_mtime,
            "updated_ts": updated_ts or st.st_mtime,
            "msgs": user_msgs + asst_msgs,
            "user_msgs": user_msgs,
            "asst_msgs": asst_msgs,
            # a sub-agent file is not resumable — never emit a bogus
            # `claude --resume agent-<id>` (the handler adds sub-agent meta)
            "resume": "" if is_subagent else resume_cmd(cwd, sid),
        },
        "turns": turns,
    }
    _TRANSCRIPT_CACHE[path] = (st.st_mtime, st.st_size, sub_sig, result)
    return result


# ---------------------------------------------------------------------------
PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Sessions</title>
<style>
  :root{
    --bg:#0a0b10; --panel:#13151f; --panel2:#171a26; --line:#242838;
    --txt:#e7e9f2; --dim:#8b90a8; --accent:#c98a5a; --accent2:#7aa2ff;
    --green:#43d39e; --amber:#f5b34a; --red:#ef5b6b; --radius:14px;
  }
  *{box-sizing:border-box}
  body{margin:0;font:14px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",sans-serif;
    background:radial-gradient(1200px 600px at 80% -10%,#1a1d2e 0%,var(--bg) 60%);color:var(--txt);min-height:100vh}
  a{color:var(--accent2);text-decoration:none}
  .wrap{max-width:1320px;margin:0 auto;padding:28px 22px 64px}
  header{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:22px}
  .brand{display:flex;align-items:center;gap:12px}
  .logo{width:38px;height:38px;border-radius:11px;background:linear-gradient(135deg,var(--accent),#a35a3a);
    display:grid;place-items:center;font-weight:800;color:#fff;box-shadow:0 6px 24px rgba(201,138,90,.35)}
  h1{font-size:20px;margin:0;font-weight:700;letter-spacing:.2px}
  .sub{color:var(--dim);font-size:12px;margin-top:2px}
  /* compact stats: a single thin inline strip */
  .statbar{display:flex;flex-wrap:wrap;align-items:baseline;gap:2px 0;margin-bottom:18px;
    border:1px solid var(--line);border-radius:10px;background:linear-gradient(180deg,var(--panel2),var(--panel));padding:7px 4px}
  .statbar .stat{padding:1px 15px;border-right:1px solid var(--line);white-space:nowrap;line-height:1.25}
  .statbar .stat:last-child{border-right:none}
  .statbar .stat b{font-size:15px;font-weight:700;font-variant-numeric:tabular-nums}
  .statbar .stat i{font-style:normal;font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:.5px;margin-left:6px}
  .toolbar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:16px}
  input,select,button{font:inherit;color:var(--txt);background:var(--panel);border:1px solid var(--line);
    border-radius:10px;padding:9px 12px;outline:none}
  input::placeholder{color:#5d627a}
  input:focus,select:focus{border-color:var(--accent2)}
  .search{flex:1;min-width:220px}
  .chips{display:flex;gap:6px}
  .chip{cursor:pointer;background:var(--panel);border:1px solid var(--line);border-radius:999px;padding:7px 14px;color:var(--dim);font-size:13px}
  .chip.on{color:#fff;border-color:var(--accent);background:linear-gradient(180deg,#2a2030,#1d1822)}
  .favs{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
  .favs .lbl{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.6px;margin-right:2px}
  .favs .lbl.tog{cursor:pointer;user-select:none}
  .favs .lbl.tog:hover{color:var(--accent2)}
  .favs .hint{font-size:12px;color:#5d627a}
  .fav.star{border-color:var(--accent)}
  .fav{cursor:pointer;display:inline-flex;align-items:center;gap:7px;border-radius:999px;padding:5px 12px;font-size:12.5px;
    color:var(--dim);border:1px solid var(--line);background:var(--panel)}
  .fav.on{color:#fff;border-color:var(--accent);background:linear-gradient(180deg,#2a2030,#1d1822)}
  .fav .x{color:var(--dim);font-weight:700}
  .fav:hover{border-color:var(--accent2)}
  .fav .x:hover{color:var(--red)}
  .favtoggle{cursor:pointer}
  .favtoggle:hover{color:var(--accent)}
  button{cursor:pointer}
  button:hover,.chip:hover{border-color:var(--accent2)}
  .count{color:var(--dim);font-size:12px;margin-left:6px}
  table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);overflow:hidden}
  th,td{text-align:left;padding:11px 13px;border-bottom:1px solid var(--line);vertical-align:top}
  th{font-size:11px;text-transform:uppercase;letter-spacing:.6px;color:var(--dim);cursor:pointer;user-select:none;white-space:nowrap;position:sticky;top:0;background:#10121b;z-index:2}
  th:hover{color:var(--txt)}
  th .ar{font-size:16px;color:var(--accent);font-weight:700}
  tbody.entry .main>td{border-bottom:none;padding-bottom:4px}
  tbody.entry .sub>td{border-bottom:1px solid var(--line);padding-top:1px;padding-bottom:12px}
  tbody.entry:last-child .sub>td{border-bottom:none}
  tbody.entry:hover td{background:#171a27}
  tbody.entry.flagged td{background:rgba(201,138,90,.06)}
  tbody.entry.flagged .dotcell{box-shadow:inset 3px 0 0 var(--accent)}
  tbody.entry.flagged:hover td{background:rgba(201,138,90,.11)}
  tbody.entry.done .main>td,tbody.entry.done .subrow{opacity:.5}
  tbody.entry.done .dotcell{box-shadow:inset 3px 0 0 var(--red)}
  .subrow{display:flex;gap:12px;align-items:flex-start}
  .subname{white-space:nowrap;flex:0 0 auto}
  .subname .ttl{margin-left:0;max-width:220px;overflow:hidden;text-overflow:ellipsis;vertical-align:bottom}
  .subrow .prev{flex:1;min-width:0;max-width:none}
  .sidtail{flex:0 0 auto;align-self:flex-start;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
    font-size:11px;color:#6a7090;white-space:nowrap;margin-left:10px}
  td.proj{width:100%}
  .dotcell{width:1px;white-space:nowrap;text-align:center}
  .dotcell .dot{margin:0}
  .subflag{vertical-align:top;padding-top:3px}
  .proj{font-weight:650}
  .proj .fam{color:var(--dim);font-weight:500}
  .ttl{display:inline-block;font-size:11px;background:#202435;border:1px solid var(--line);border-radius:6px;padding:1px 7px;margin-left:6px;color:var(--accent)}
  .prev{color:var(--dim);font-size:12.5px;display:-webkit-box;-webkit-box-orient:vertical;
    -webkit-line-clamp:2;line-clamp:2;overflow:hidden}
  .num{font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}
  .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:middle}
  .op-open .dot{background:var(--green);box-shadow:0 0 0 3px rgba(67,211,158,.18)}
  .op-busy .dot{background:var(--green);animation:pulse 1.6s infinite}
  .op-closed .dot{background:#3a3f55}
  @keyframes pulse{0%{box-shadow:0 0 0 0 rgba(67,211,158,.55)}70%{box-shadow:0 0 0 6px rgba(67,211,158,0)}100%{box-shadow:0 0 0 0 rgba(67,211,158,0)}}
  .r-live{color:var(--green)}
  .r-recent{color:var(--amber)}
  .r-idle{color:var(--dim)}
  .badge{font-size:11px;color:var(--dim)}
  .mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
  .copy{cursor:pointer;border:1px solid var(--line);background:#1b1e2b;border-radius:8px;padding:5px 9px;font-size:11px;color:var(--dim);white-space:nowrap}
  .copy:hover{color:#fff;border-color:var(--accent)}
  a.copy{display:inline-block;color:var(--dim)}
  a.copy:hover{color:#fff;border-color:var(--accent2)}
  .actcell{white-space:nowrap}
  .flagbtn{cursor:pointer;border:1px solid var(--line);background:#1b1e2b;border-radius:8px;padding:4px 9px;font-size:13px;color:var(--dim);line-height:1}
  .flagbtn:hover{border-color:var(--accent);color:var(--accent)}
  .flagbtn.on{color:var(--accent);border-color:var(--accent);background:rgba(201,138,90,.14)}
  .subdone{vertical-align:top;padding-top:3px;text-align:right}
  .flagbtn.done.on{color:var(--red);border-color:var(--red);background:rgba(239,91,107,.14)}
  .grouphead{display:flex;align-items:center;gap:10px;margin:22px 0 8px;font-weight:700}
  .grouphead .pill{font-size:11px;color:var(--dim);background:var(--panel);border:1px solid var(--line);border-radius:999px;padding:2px 10px}
  .branch{display:block;margin-top:4px;font-size:10px;color:var(--green);opacity:.75;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
  .toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(20px);background:#1c2030;
    border:1px solid var(--accent);color:#fff;padding:10px 18px;border-radius:10px;opacity:0;transition:.25s;pointer-events:none;box-shadow:0 12px 40px rgba(0,0,0,.5)}
  .toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
  .empty{color:var(--dim);text-align:center;padding:48px}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="brand">
      <div class="logo">C</div>
      <div>
        <h1>Claude Sessions</h1>
        <div class="sub"><span id="sub">scanning…</span><span class="count" id="count"></span></div>
      </div>
    </div>
    <div class="chips">
      <button id="refresh">↻ Refresh</button>
      <label class="chip"><input type="checkbox" id="group" style="margin-right:6px">Group by project</label>
    </div>
  </header>

  <div class="statbar" id="cards2"></div>

  <div class="favs" id="favs"></div>

  <div class="toolbar">
    <input class="search" id="q" placeholder="Filter by project, title, message, branch, id…">
    <select id="projsel"><option value="">All projects</option></select>
    <span class="chip" id="openchip" title="Show only open sessions (combines with the time filter)">● Open</span>
    <span class="chip" id="flagchip" title="Show only sessions flagged to reopen after restart">⚑ Flagged</span>
    <span class="chip" id="donechip" title="Show sessions marked done (hidden by default)">✕ Done</span>
    <div class="chips" id="status">
      <span class="chip" data-s="all">All</span>
      <span class="chip" data-s="live">Live ·15m</span>
      <span class="chip" data-s="recent">Active ·2h</span>
      <span class="chip on" data-s="day">24h</span>
    </div>
  </div>

  <div id="view"></div>
</div>
<div class="toast" id="toast"></div>

<script>
const STATIC=false; // --once replaces this line: no server → no view/flag endpoints
const COLS = [
  {k:'status', t:'', sort:'updated_ts', cls:''},
  {k:'project', t:'Project', sort:'project'},
  {k:'started_ts', t:'Started', sort:'started_ts', num:true},
  {k:'updated_ts', t:'Last active', sort:'updated_ts', num:true},
  {k:'msgs', t:'Msgs', sort:'msgs', num:true},
  {k:'out_tokens', t:'Out tok', sort:'out_tokens', num:true},
  {k:'model', t:'Model', sort:'model'},
  {k:'size_bytes', t:'Size', sort:'size_bytes', num:true},
  {k:'rss_kb', t:'RAM', sort:'rss_kb', num:true},
  {k:'resume', t:'', sort:null},
];
let DATA=[], sortKey='updated_ts', sortDir=-1, statusF='day', openOnly=false, flaggedOnly=false, showDone=false, generated=0;
function loadFavs(){try{return new Set(JSON.parse(localStorage.getItem('cs_favs')||'[]'));}catch(e){return new Set();}}
function saveFavs(){try{localStorage.setItem('cs_favs',JSON.stringify([...FAVS]));}catch(e){}}
let FAVS=loadFavs();
// chip bar mode: 'favs' = saved shortcuts only; 'all' = every project, by last active
let favMode=(()=>{try{return localStorage.getItem('cs_favmode')||'favs';}catch(e){return 'favs';}})();
function saveFavMode(){try{localStorage.setItem('cs_favmode',favMode);}catch(e){}}

const $=s=>document.querySelector(s);
const now=()=>Date.now()/1000;
function rel(ts){let d=now()-ts;if(d<60)return Math.floor(d)+'s ago';if(d<3600)return Math.floor(d/60)+'m ago';
  if(d<86400)return Math.floor(d/3600)+'h ago';let days=Math.floor(d/86400);return days+'d ago';}
function abs(ts){let dt=new Date(ts*1000);return dt.toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',hour12:false});}
function stamp(ts){let d=new Date(ts*1000);let mon=d.toLocaleString([],{month:'short'});
  return d.getDate()+' '+mon+' '+String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0');}
function bytes(n){if(n<1024)return n+' B';if(n<1048576)return (n/1024).toFixed(0)+' KB';return (n/1048576).toFixed(1)+' MB';}
function ram(kb){if(!kb)return '–';let mb=kb/1024;if(mb<999.5)return Math.round(mb)+' MB';return (mb/1024).toFixed(1)+' GB';}
function ktok(n){if(!n)return '–';
  if(n<1000)return n.toLocaleString();
  if(n<1e6)return +(n/1e3).toFixed(1)+'K';
  if(n<1e9)return +(n/1e6).toFixed(1)+'M';
  if(n<1e12)return +(n/1e9).toFixed(1)+'B';
  return +(n/1e12).toFixed(1)+'T';}
function recencyClass(s){let d=now()-s.updated_ts;if(d<7200)return 'r-live';if(d<86400)return 'r-recent';return 'r-idle';}

function passStatus(s){if(s.done && !showDone)return false;
  if(openOnly && !s.open)return false;if(flaggedOnly && !s.flagged)return false;
  if(statusF==='all')return true;let d=now()-s.updated_ts;
  if(statusF==='live')return d<900;if(statusF==='recent')return d<7200;if(statusF==='day')return d<86400;return true;}

function filtered(){
  let q=$('#q').value.toLowerCase().trim();
  let p=$('#projsel').value;
  return DATA.filter(s=>{
    if(!passStatus(s))return false;
    if(p && s.project!==p)return false;
    if(q){let hay=(s.project+' '+s.title+' '+s.preview+' '+s.branch+' '+s.id+' '+s.model).toLowerCase();
      if(!hay.includes(q))return false;}
    return true;
  });
}
function sorted(list){
  let l=list.slice();
  l.sort((a,b)=>{let x=a[sortKey],y=b[sortKey];
    if(typeof x==='string'){x=x.toLowerCase();y=y.toLowerCase();}
    if(x<y)return -1*sortDir;if(x>y)return 1*sortDir;return 0;});
  return l;
}
function projName(p){
  let parts=p.split('/');
  if(parts.length>1)return '<span class="fam">'+esc(parts[0])+'/</span>'+esc(parts[1]);
  return esc(p);
}
function projCell(s){
  let fav=FAVS.has(s.project);
  return `<span class="favtoggle" data-proj="${esc(s.project)}" title="${fav?'Remove shortcut':'Add shortcut chip'}">${projName(s.project)}</span>`;
}
function esc(t){return (t||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}

const REC_TITLE={'r-live':'active in the last 2 hours','r-recent':'active in the last 24 hours','r-idle':'idle — older than 24 hours'};
function rowHtml(s){
  let rc=recencyClass(s);
  let op=s.open?(s.live_status==='busy'?'op-busy':'op-open'):'op-closed';
  let opTitle=s.open?('Open'+(s.live_status?(' · '+s.live_status):'')):'Closed';
  return `<tbody class="entry${s.flagged?' flagged':''}${s.done?' done':''}">
    <tr class="main">
      <td class="dotcell ${op}" title="${opTitle}"><span class="dot"></span></td>
      <td class="proj">${projCell(s)}${s.branch?`<div class="branch">⎇ ${esc(s.branch)}</div>`:''}</td>
      <td class="num" title="${abs(s.started_ts)}">${abs(s.started_ts)}</td>
      <td class="num ${rc}" title="${REC_TITLE[rc]} · ${abs(s.updated_ts)}">${rel(s.updated_ts)}</td>
      <td class="num">${s.msgs}<div class="badge">${s.user_msgs}u·${s.asst_msgs}a</div></td>
      <td class="num">${ktok(s.out_tokens)}</td>
      <td><span class="mono badge">${esc(s.model)||'–'}</span></td>
      <td class="num">${bytes(s.size_bytes)}</td>
      <td class="num">${s.rss_kb?ram(s.rss_kb):'–'}</td>
      <td class="actcell">${STATIC?'':`<a class="copy" href="session?id=${encodeURIComponent(s.id)}" target="_blank" rel="noopener" title="Open the transcript">view</a> `}<span class="copy" data-cmd="${esc(s.resume)}">⧉ resume</span></td>
    </tr>
    <tr class="sub">
      <td class="dotcell subflag"><span class="flagbtn${s.flagged?' on':''}" data-id="${s.id}" data-kind="flag" title="${s.flagged?'Flagged — reopen after restart':'Flag to reopen after restart'}">⚑</span></td>
      <td colspan="${COLS.length-2}"><div class="subrow"><span class="subname">${s.title?`<span class="ttl">${esc(s.title)}</span>`:''}</span><div class="prev">${esc(s.preview)}</div>${s.title?'':`<span class="sidtail" title="session ${esc(s.id)} — matches the statusline #${esc(s.id.slice(-4))}">#${esc(s.id.slice(-4))}</span>`}</div></td>
      <td class="subdone"><span class="flagbtn done${s.done?' on':''}" data-id="${s.id}" data-kind="done" title="${s.done?'Marked done — click to restore':'Mark done (hide)'}">✕</span></td>
    </tr>
  </tbody>`;
}

function thHtml(){
  return '<tr>'+COLS.map(c=>{
    if(!c.sort)return '<th></th>';
    let ar=sortKey===c.sort?(sortDir<0?'▾':'▴'):'';
    return `<th data-sort="${c.sort}">${c.t} <span class="ar">${ar}</span></th>`;
  }).join('')+'</tr>';
}

function render(){
  let list=sorted(filtered());
  $('#count').textContent='· '+list.length+' of '+DATA.length+' sessions';
  let grouped=$('#group').checked;
  let v=$('#view');
  if(!list.length){v.innerHTML='<div class="empty">No sessions match.</div>';bindRows();return;}
  if(grouped){
    let by={};list.forEach(s=>{(by[s.project]=by[s.project]||[]).push(s);});
    let keys=Object.keys(by).sort((a,b)=>by[b].length-by[a].length||a.localeCompare(b));
    v.innerHTML=keys.map(k=>{
      let rows=by[k];let tok=rows.reduce((a,s)=>a+s.out_tokens,0);let fav=FAVS.has(k);
      return `<div class="grouphead"><span class="favtoggle" data-proj="${esc(k)}" title="${fav?'Remove shortcut':'Add shortcut chip'}">${esc(k)}</span><span class="pill">${rows.length} session${rows.length>1?'s':''}</span>
        <span class="pill">${ktok(tok)} out tok</span></div>
        <table><thead>${thHtml()}</thead>${rows.map(rowHtml).join('')}</table>`;
    }).join('');
  }else{
    v.innerHTML=`<table><thead>${thHtml()}</thead>${list.map(rowHtml).join('')}</table>`;
  }
  bindRows();
}

function bindRows(){
  document.querySelectorAll('th[data-sort]').forEach(th=>th.onclick=()=>{
    let k=th.dataset.sort;if(sortKey===k)sortDir*=-1;else{sortKey=k;sortDir=(k==='project'||k==='preview'||k==='model')?1:-1;}
    render();
  });
  // [data-cmd] only: the "view" link shares .copy for styling but is a plain <a>
  document.querySelectorAll('.copy[data-cmd]').forEach(b=>b.onclick=()=>{
    navigator.clipboard.writeText(b.dataset.cmd).then(()=>toast('Copied: '+b.dataset.cmd.slice(0,48)+'…'));
  });
  document.querySelectorAll('.favtoggle').forEach(el=>el.onclick=()=>toggleFav(el.dataset.proj));
  document.querySelectorAll('.flagbtn').forEach(b=>b.onclick=()=>toggleFlag(b.dataset.id,b.dataset.kind));
}
let _flagBusy=new Set();
async function toggleFlag(id,kind){
  if(_flagBusy.has(id))return;  // ignore rapid re-clicks on the same row until the POST settles
  let s=DATA.find(x=>x.id===id);if(!s)return;
  let key=kind==='done'?'done':'flagged';
  let want=!s[key];let prevFlagged=s.flagged,prevDone=s.done;s[key]=want;
  if(want){if(kind==='done')s.flagged=false;else s.done=false;}  // never both (server matches)
  _flagBusy.add(id);render();cards2();
  try{let r=await fetch('api/flag',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({id:id,kind:kind,value:want})});
    if(!r.ok)throw 0;}
  catch(e){s.flagged=prevFlagged;s.done=prevDone;render();cards2();toast('Could not save');}
  finally{_flagBusy.delete(id);}
}
function toggleFav(p){
  if(FAVS.has(p)){FAVS.delete(p);toast('Removed shortcut: '+p);}else{FAVS.add(p);toast('Added shortcut: '+p);}
  saveFavs();renderFavs();render();
}
function allProjectsByLastActive(){
  let last={};
  DATA.forEach(s=>{if(!(s.project in last)||s.updated_ts>last[s.project])last[s.project]=s.updated_ts;});
  return Object.keys(last).sort((a,b)=>last[b]-last[a]);
}
function renderFavs(){
  let el=$('#favs');let cur=$('#projsel').value;
  let label=`<span class="lbl tog" id="favmode" title="Toggle: shortcuts ⇄ all projects">`
    +`${favMode==='all'?'All projects':'Shortcuts'} ⇄</span>`;
  let chips;
  if(favMode==='all'){
    chips=allProjectsByLastActive().map(p=>
      `<span class="fav${p===cur?' on':''}${FAVS.has(p)?' star':''}" data-proj="${esc(p)}" title="Filter to ${esc(p)}">${esc(p)}</span>`).join('');
  }else{
    let favs=[...FAVS].sort();
    chips=favs.length
      ? favs.map(p=>`<span class="fav${p===cur?' on':''}" data-proj="${esc(p)}" title="Filter to ${esc(p)}">${esc(p)} <span class="x" data-rm="1" title="Remove shortcut">✕</span></span>`).join('')
      : '<span class="hint">click a project name to add a shortcut, or toggle ⇄ to list all</span>';
  }
  el.innerHTML=label+chips;
  bindFavMode();
  el.querySelectorAll('.fav').forEach(b=>b.onclick=e=>{
    if(e.target.dataset.rm){toggleFav(b.dataset.proj);return;}
    let sel=$('#projsel');sel.value=(sel.value===b.dataset.proj)?'':b.dataset.proj;render();renderFavs();
  });
}
function bindFavMode(){
  let t=$('#favmode');if(t)t.onclick=()=>{favMode=(favMode==='all'?'favs':'all');saveFavMode();renderFavs();};
}
function toast(t){let el=$('#toast');el.textContent=t;el.classList.add('show');clearTimeout(el._t);el._t=setTimeout(()=>el.classList.remove('show'),1800);}

function cards2(){
  let s=DATA;
  let open=s.filter(x=>x.open).length;
  let live=s.filter(x=>now()-x.updated_ts<900).length;
  let flagged=s.filter(x=>x.flagged).length;
  let done=s.filter(x=>x.done).length;
  let ramkb=s.reduce((a,x)=>a+(x.rss_kb||0),0);
  let projs=new Set(s.map(x=>x.project)).size;
  let msgs=s.reduce((a,x)=>a+x.msgs,0);
  let tok=s.reduce((a,x)=>a+x.out_tokens,0);
  let size=s.reduce((a,x)=>a+x.size_bytes,0);
  let S=[[s.length,'sessions'],[open,'open'],[live,'live ·15m'],[flagged,'flagged'],[done,'done'],[ram(ramkb),'ram·open'],
    [projs,'projects'],[msgs.toLocaleString(),'msgs'],[ktok(tok),'out tok'],[bytes(size),'on disk']];
  $('#cards2').innerHTML=S.map(x=>`<span class="stat"><b>${x[0]}</b><i>${x[1]}</i></span>`).join('');
}

function fillProjects(){
  let ps=[...new Set(DATA.map(s=>s.project))].sort();
  let sel=$('#projsel');let cur=sel.value;
  sel.innerHTML='<option value="">All projects</option>'+ps.map(p=>`<option ${p===cur?'selected':''}>${esc(p)}</option>`).join('');
}

async function load(){
  let r=await fetch('api/sessions?t='+Date.now());let j=await r.json();
  DATA=j.sessions;generated=j.generated;
  $('#sub').textContent='Updated '+stamp(generated);
  cards2();fillProjects();renderFavs();render();
}

$('#q').oninput=render;
$('#projsel').onchange=()=>{render();renderFavs();};
$('#group').onchange=render;
$('#refresh').onclick=load;
document.querySelectorAll('#status .chip').forEach(c=>c.onclick=()=>{
  // click the active chip again to fall back to "all"
  statusF=(statusF===c.dataset.s && c.dataset.s!=='all')?'all':c.dataset.s;
  document.querySelectorAll('#status .chip').forEach(x=>x.classList.toggle('on',x.dataset.s===statusF));
  render();
});
$('#openchip').onclick=()=>{openOnly=!openOnly;$('#openchip').classList.toggle('on',openOnly);render();};
$('#flagchip').onclick=()=>{flaggedOnly=!flaggedOnly;$('#flagchip').classList.toggle('on',flaggedOnly);render();};
$('#donechip').onclick=()=>{showDone=!showDone;$('#donechip').classList.toggle('on',showDone);render();};
renderFavs();
load();
setInterval(load,30000);
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# The transcript page (GET /session?id=<id>[&agent=<agentId>]).
# XSS: every transcript-derived string is set via textContent / DOM
# construction (el()); no innerHTML with transcript data, no transcript data
# in URLs except encodeURIComponent'd server-validated ids and the
# allow-listed data: image URL. See AI/transcript.md.
TRANSCRIPT_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Session</title>
<style>
  :root{
    --bg:#0a0b10; --panel:#13151f; --panel2:#171a26; --line:#242838;
    --txt:#e7e9f2; --dim:#8b90a8; --accent:#c98a5a; --accent2:#7aa2ff;
    --green:#43d39e; --amber:#f5b34a; --red:#ef5b6b; --radius:14px;
  }
  *{box-sizing:border-box}
  body{margin:0;font:14px/1.55 -apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",sans-serif;
    background:var(--bg);color:var(--txt);min-height:100vh}
  a{color:var(--accent2);text-decoration:none}
  .top{position:sticky;top:0;z-index:10;display:flex;align-items:center;gap:10px;flex-wrap:wrap;
    padding:10px 16px;background:#10121bee;border-bottom:1px solid var(--line);backdrop-filter:blur(6px)}
  .top .back{color:var(--dim);white-space:nowrap}
  .top .back:hover{color:var(--txt)}
  .top .proj{font-weight:700}
  .ttl{display:inline-block;font-size:11px;background:#202435;border:1px solid var(--line);border-radius:6px;padding:1px 7px;color:var(--accent);
    max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;vertical-align:bottom}
  .metabits{color:var(--dim);font-size:12px;white-space:nowrap}
  .top .sp{flex:1}
  input,button{font:inherit;color:var(--txt);background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:6px 10px;outline:none}
  input:focus{border-color:var(--accent2)}
  button{cursor:pointer}
  button:hover{border-color:var(--accent2)}
  .copy{cursor:pointer;border:1px solid var(--line);background:#1b1e2b;border-radius:8px;padding:6px 10px;font-size:12px;color:var(--dim);white-space:nowrap}
  .copy:hover{color:#fff;border-color:var(--accent)}
  .qwrap{display:flex;align-items:center;gap:6px}
  #q{width:220px}
  .qcount{color:var(--dim);font-size:12px;min-width:70px}
  .scope{font-size:12px;color:var(--dim);white-space:nowrap;cursor:pointer;user-select:none}
  .agentbar{padding:8px 16px;background:rgba(122,162,255,.08);border-bottom:1px solid var(--line);font-size:13px}
  .agentbar b{color:var(--accent2)}
  .layout{display:flex;align-items:flex-start}
  #side{position:sticky;top:53px;width:300px;flex:0 0 300px;max-height:calc(100vh - 53px);overflow-y:auto;
    padding:12px;border-right:1px solid var(--line)}
  .sect{font-size:11px;text-transform:uppercase;letter-spacing:.6px;color:var(--dim);margin:14px 0 6px}
  .sect:first-child{margin-top:0}
  .pitem{cursor:pointer;padding:6px 8px;border-radius:8px;border:1px solid transparent;margin-bottom:2px}
  .pitem:hover{background:var(--panel2)}
  .pitem.cur{border-color:var(--accent);background:rgba(201,138,90,.08)}
  .plabel{font-size:12.5px;overflow:hidden;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;line-clamp:2}
  .ptime{font-size:11px;color:var(--dim);margin-top:1px}
  .aitem{cursor:pointer;display:block;padding:6px 8px;border-radius:8px;border:1px solid transparent;margin-bottom:2px;color:var(--txt)}
  .aitem:hover{background:var(--panel2)}
  .aitem.cur{border-color:var(--accent2);background:rgba(122,162,255,.12)}
  .atype{font-size:12px;color:var(--accent2)}
  .adesc{font-size:12px;color:var(--dim);overflow:hidden;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;line-clamp:2}
  .hint{font-size:12px;color:#5d627a;padding:4px 8px}
  #main{flex:1;min-width:0;padding:18px 22px 80px;max-width:980px}
  body.paneopen #main{max-width:none}
  .turn{margin-bottom:14px;scroll-margin-top:64px}
  .thead{display:flex;align-items:center;gap:8px;font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--dim);margin-bottom:4px}
  .cpy{flex:0 0 auto;margin-left:auto;cursor:pointer;font-size:11px;line-height:1;color:var(--dim);
    background:#1b1e2b;border:1px solid var(--line);border-radius:6px;padding:3px 7px}
  .cpy:hover{color:#fff;border-color:var(--accent)}
  /* human prompts: distinct colour + larger serif + elevator gutter — the loudest element */
  .turn.prompt{display:flex;gap:10px;align-items:flex-start;
    background:linear-gradient(180deg,#182a44,#132135);border:1px solid #2b446b;
    border-left:4px solid var(--accent2);border-radius:12px;padding:12px 15px 14px}
  .turn.prompt .thead{color:var(--accent2)}
  .turn.prompt .pbody{flex:1;min-width:0}
  .turn.prompt .ptext{font-family:ui-serif,Georgia,"Times New Roman",serif;font-size:16px;line-height:1.55;color:#f3f5fc}
  .elev{display:flex;flex-direction:column;gap:4px;flex:0 0 auto;padding-top:1px}
  .elev button{width:24px;height:21px;padding:0;font-size:10px;line-height:1;color:var(--dim);
    background:#182644;border:1px solid var(--line);border-radius:6px}
  .elev button:hover{color:#fff;border-color:var(--accent2);background:#22345c}
  .turn.assistant{padding:0 14px 0 17px}
  .turn.toolio{padding:0 14px 0 17px}
  .turn.sys{padding:0 14px 0 17px}
  /* harness-injected background-task notice — distinct card, not a prompt */
  .turn.event{background:var(--panel);border:1px solid var(--line);
    border-left:3px solid var(--accent);border-radius:10px;padding:9px 13px 11px;margin:6px 0}
  .turn.event .thead{color:var(--accent)}
  .xmlblock{margin:7px 0 0;padding:9px 11px;background:var(--bg);border:1px solid var(--line);
    border-radius:8px;font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;
    white-space:pre-wrap;word-break:break-word;overflow-wrap:anywhere;overflow:auto}
  .xmlblock .xtag{color:var(--accent2)}
  .xmlblock .xval{color:var(--txt)}
  /* harness-injected prose (loop/scheduled prompts, skill preambles) — badged */
  .turn.event.injected{border-left-color:var(--dim)}
  .ihead{display:flex;align-items:center;gap:8px;margin-bottom:5px}
  .badge{flex:0 0 auto;font-size:10px;text-transform:uppercase;letter-spacing:.6px;color:var(--dim);
    background:#1b1e2b;border:1px solid var(--line);border-radius:999px;padding:2px 8px}
  .ihint{font-size:11px;color:var(--dim)}
  .ibody{color:#c2c7dc}
  .ibody .ptext{font-size:13.5px;line-height:1.55;color:#c2c7dc}
  .ptext{white-space:pre-wrap;word-break:break-word;overflow-wrap:anywhere}
  .fold{border:1px solid var(--line);border-radius:8px;background:var(--panel);margin:6px 0}
  .fold>summary{cursor:pointer;padding:6px 10px;font-size:12px;color:var(--dim);user-select:none;
    display:flex;align-items:center;gap:8px;list-style:none}
  .fold>summary::-webkit-details-marker{display:none}
  .fold>summary::before{content:'▸';flex:0 0 auto;font-size:10px;transition:transform .12s;color:var(--dim)}
  .fold[open]>summary::before{transform:rotate(90deg)}
  .fold>summary .flabel{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .fold>summary:hover{color:var(--txt)}
  .fold[open]>summary{border-bottom:1px solid var(--line);color:var(--txt)}
  .fold>pre,.fold .fbody{margin:0;padding:10px;font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;
    white-space:pre-wrap;word-break:break-word;overflow-wrap:anywhere;max-height:480px;overflow:auto}
  .fold .fbody{font:inherit}
  .fold.thinking>summary{color:#8f86b8}
  .fold.result.err{border-color:var(--red)}
  .fold.result.err>summary{color:var(--red)}
  .fold.agent{border-color:#2c3a5e;background:#141a2a}
  .fold.agent>summary{color:var(--accent2)}
  .fold.unknown{border-color:var(--amber)}
  .fold.unknown>summary{color:var(--amber)}
  .fold.sysm{border-color:#2a2f45}
  /* rendered Markdown (assistant output) */
  .md{font-size:14px;line-height:1.6}
  .md>:first-child{margin-top:0}
  .md>:last-child{margin-bottom:0}
  .md p{margin:0 0 9px}
  .md h1,.md h2,.md h3,.md h4,.md h5,.md h6{margin:14px 0 7px;line-height:1.3;font-weight:700}
  .md h1{font-size:20px}.md h2{font-size:17px}.md h3{font-size:15px}.md h4,.md h5,.md h6{font-size:14px}
  .md ul,.md ol{margin:5px 0 9px;padding-left:22px}
  .md li{margin:2px 0}
  .md code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;
    background:#1c2030;border:1px solid var(--line);border-radius:5px;padding:1px 5px}
  .md pre.md-code{margin:8px 0;padding:10px 12px;background:var(--panel);border:1px solid var(--line);border-radius:8px;overflow:auto}
  .md pre.md-code code{background:none;border:none;padding:0;font-size:12.5px;line-height:1.5;white-space:pre}
  .md blockquote{margin:8px 0;padding:2px 12px;border-left:3px solid var(--line);color:var(--dim)}
  .md a{color:var(--accent2);text-decoration:underline}
  .md hr{border:none;border-top:1px solid var(--line);margin:12px 0}
  .md table.md-table{border-collapse:collapse;margin:8px 0;font-size:13px;display:block;overflow-x:auto}
  .md table.md-table th,.md table.md-table td{border:1px solid var(--line);padding:5px 9px;text-align:left}
  .md table.md-table th{background:#161a27}
  .md strong{color:#f1f3fb;font-weight:700}
  .md em{font-style:italic}
  .abody{padding:8px 10px}
  .viewbtn{margin-top:8px;font-size:12px;color:var(--accent2);background:#16223c;border:1px solid #2c3a5e;border-radius:7px;padding:5px 11px}
  .viewbtn:hover{background:#1d2c4d;border-color:var(--accent2)}
  .viewbtn.active{background:var(--accent2);color:#0a0b10;border-color:var(--accent2);font-weight:600}
  .marker{font-size:12px;color:var(--amber);margin:6px 0}
  .stub{font-size:12px;color:var(--dim);border:1px dashed var(--line);border-radius:8px;padding:6px 10px;margin:6px 0}
  .pimg{max-width:100%;max-height:480px;border-radius:8px;border:1px solid var(--line);display:block;margin:6px 0}
  .turn.hit{box-shadow:inset 3px 0 0 var(--accent2)}
  .turn.hitcur{outline:2px solid var(--accent2);outline-offset:2px;border-radius:10px}
  .err{color:var(--red);padding:30px}
  .empty{color:var(--dim);padding:30px}
  .toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(20px);background:#1c2030;
    border:1px solid var(--accent);color:#fff;padding:10px 18px;border-radius:10px;opacity:0;transition:.25s;pointer-events:none}
  .toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
  /* 3rd pane: sub-agent transcript, right of the main column */
  #apane{position:sticky;top:53px;flex:0 0 46%;max-width:46%;max-height:calc(100vh - 53px);
    overflow-y:auto;border-left:1px solid var(--line);background:#0c0e16;display:none}
  #apane.open{display:block}
  .ahead{position:sticky;top:0;z-index:5;display:flex;align-items:center;gap:10px;padding:10px 14px;
    background:#121623f2;border-bottom:1px solid var(--line);backdrop-filter:blur(6px)}
  .ahead .atitle{flex:1;min-width:0}
  .ahead .atype{font-size:13px;color:var(--accent2);font-weight:600}
  .ahead .adesc2{font-size:12px;color:var(--dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .pbtn{flex:0 0 auto;width:28px;height:26px;padding:0;font-size:13px;color:var(--dim);
    background:#1b1e2b;border:1px solid var(--line);border-radius:7px}
  .pbtn:hover{color:#fff;border-color:var(--accent2)}
  .abody2{padding:14px 16px 60px}
  .flash{animation:flashk 1.3s ease-out}
  @keyframes flashk{0%{box-shadow:0 0 0 3px var(--accent2)}100%{box-shadow:0 0 0 12px rgba(122,162,255,0)}}
  @media (max-width:900px){#side{display:none}}
  @media (max-width:1100px){#apane{flex-basis:58%;max-width:58%}}
  @media (max-width:820px){#apane{position:fixed;inset:53px 0 0 0;max-width:none;flex-basis:auto;z-index:30}}
</style>
</head>
<body>
<div class="top">
  <a class="back" href="./">← sessions</a>
  <span class="proj" id="proj"></span>
  <span class="ttl" id="ttl" style="display:none"></span>
  <span class="metabits" id="metabits"></span>
  <span class="sp"></span>
  <div class="qwrap">
    <input id="q" placeholder="search…  ( / )">
    <label class="scope" title="Also search tool input/output, thinking and system markers">
      <input type="checkbox" id="qall" style="vertical-align:-2px"> all text</label>
    <span class="qcount" id="qcount"></span>
  </div>
  <button id="expand" title="Expand/collapse every folded block">expand all</button>
  <button id="refresh" title="Re-fetch the transcript (the view is a load-time snapshot)">↻</button>
  <span class="copy" id="resume" style="display:none">⧉ resume</span>
</div>
<div class="agentbar" id="agentbar" style="display:none"></div>
<div class="layout">
  <nav id="side">
    <div class="sect">Prompts <span id="pcount" style="text-transform:none;letter-spacing:0"></span></div>
    <div id="plist"></div>
    <div id="asect" style="display:none">
      <div class="sect">Sub-agents <span id="acount" style="text-transform:none;letter-spacing:0"></span></div>
      <div id="alist"></div>
    </div>
  </nav>
  <main id="main"><div class="empty">loading…</div></main>
  <aside id="apane"></aside>
</div>
<div class="toast" id="toast"></div>

<script>
const $=s=>document.querySelector(s);
const params=new URLSearchParams(location.search);
const SID=params.get('id')||'', AGENT=params.get('agent')||'';
let DATA=null, PROMPTS=[], curPrompt=-1, HITS=[], curHit=-1, allOpen=false, TOOLNAME={}, paneStack=[];

function el(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;
  if(text!=null)e.textContent=text;return e;}
const now=()=>Date.now()/1000;
function rel(ts){if(!ts)return'';let d=now()-ts;if(d<60)return Math.floor(d)+'s ago';
  if(d<3600)return Math.floor(d/60)+'m ago';if(d<86400)return Math.floor(d/3600)+'h ago';
  return Math.floor(d/86400)+'d ago';}
function abs(ts){if(!ts)return'';let dt=new Date(ts*1000);
  return dt.toLocaleString([],{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',hour12:false});}
function hhmm(ts){if(!ts)return'';let d=new Date(ts*1000);
  return String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0');}
function toast(t){let e=$('#toast');e.textContent=t;e.classList.add('show');
  clearTimeout(e._t);e._t=setTimeout(()=>e.classList.remove('show'),1800);}
function oneline(s,n){s=(s||'').replace(/\s+/g,' ').trim();return s.length>n?s.slice(0,n)+'…':s;}

function foldBlock(cls,label,text,copyText){
  const d=document.createElement('details');d.className='fold '+cls;
  const s=document.createElement('summary');
  s.appendChild(el('span','flabel',label));
  if(copyText)s.appendChild(copyBtn(()=>copyText));
  d.appendChild(s);
  const pre=el('pre',null,text==null?'':text);d.appendChild(pre);return d;
}

// Copy the RAW text (Markdown source left intact, per the spec). stopPropagation
// so a copy click inside a <summary> doesn't also toggle the fold.
function copyBtn(getText){
  const b=el('button','cpy','⧉ copy');b.title='Copy the raw text';
  b.onclick=(e)=>{e.preventDefault();e.stopPropagation();
    const t=getText()||'';
    navigator.clipboard.writeText(t).then(()=>toast('Copied '+t.length+' chars'))
      .catch(()=>toast('Copy failed'));};
  return b;
}
function rawText(t){return (t.parts||[]).filter(p=>p.kind==='text').map(p=>p.text).join('\n\n');}

// --- minimal, XSS-safe Markdown → DOM (no innerHTML, no external lib) --------
// Everything is built with createElement/createTextNode so no transcript-derived
// string is ever parsed as HTML. Unsafe link schemes (javascript:, data:) are
// dropped to plain text.
// http(s)/mailto explicitly, plus any scheme-LESS href (relative path, anchor,
// query — e.g. README.md, ../x, #sec). Reject protocol-relative "//host" and any
// other scheme (javascript:, data:, file:, vbscript:, …) → those degrade to text.
function mdSafeHref(href){
  if(/^(https?:\/\/|mailto:)/i.test(href))return true;
  if(/^\/\//.test(href))return false;                  // protocol-relative = external
  return !/^[a-z][a-z0-9+.\-]*:/i.test(href);           // any explicit scheme → unsafe
}
function mdLink(text,url){
  if(!mdSafeHref(url))return document.createTextNode(text||url);  // unsafe scheme → plain text
  const a=el('a',null,text||url);a.href=url;a.target='_blank';a.rel='noopener';return a;
}
function mdInline(s){
  const out=[];
  // bound the regex cost: a pathological very-long single line (e.g. a pasted
  // one-line blob of unmatched `[`/`*`) is O(n^2) here — render it verbatim
  if(s.length>2000){out.push(document.createTextNode(s));return out;}
  const re=/(`+)([\s\S]*?)\1|(\*\*|__)([\s\S]+?)\3|(\*|_)([\s\S]+?)\5|~~([\s\S]+?)~~|\[([^\]]*)\]\(\s*([^)\s]+)[^)]*\)|(https?:\/\/[^\s)]+)/g;
  let m,last=0;
  while((m=re.exec(s))){
    if(m.index>last)out.push(document.createTextNode(s.slice(last,m.index)));
    if(m[1]!==undefined){out.push(el('code',null,m[2]));}
    else if(m[3]!==undefined){const b=el('strong');mdInline(m[4]).forEach(n=>b.appendChild(n));out.push(b);}
    else if(m[5]!==undefined){const it=el('em');mdInline(m[6]).forEach(n=>it.appendChild(n));out.push(it);}
    else if(m[7]!==undefined){const dl=el('del');mdInline(m[7]).forEach(n=>dl.appendChild(n));out.push(dl);}
    else if(m[8]!==undefined){out.push(mdLink(m[8],m[9]));}
    else if(m[10]!==undefined){out.push(mdLink(m[10],m[10]));}
    last=re.lastIndex;
    if(re.lastIndex===m.index)re.lastIndex++;  // guard against a zero-length match loop
  }
  if(last<s.length)out.push(document.createTextNode(s.slice(last)));
  return out;
}
function mdToDom(src,depth){
  depth=depth||0;
  const frag=document.createDocumentFragment();
  // only blockquotes recurse; a single line of N leading '>' would otherwise be
  // N stack frames. Past the cap, render the rest verbatim rather than overflow.
  if(depth>24){frag.appendChild(el('pre','md-code',src||''));return frag;}
  const lines=(src||'').replace(/\r\n?/g,'\n').split('\n');
  let i=0;
  const isBlockStart=l=>/^\s*(#{1,6}\s|```|~~~|>|([-*+]|\d+[.)])\s)/.test(l);
  while(i<lines.length){
    const line=lines[i];
    const fence=line.match(/^\s*(```+|~~~+)/);
    if(fence){
      const close=fence[1][0]==='`'?/^\s*```+/:/^\s*~~~+/;i++;
      const buf=[];
      while(i<lines.length&&!close.test(lines[i])){buf.push(lines[i]);i++;}
      i++;  // consume closing fence (if any)
      const pre=el('pre','md-code');pre.appendChild(el('code',null,buf.join('\n')));frag.appendChild(pre);
      continue;
    }
    if(/^\s*$/.test(line)){i++;continue;}
    const h=line.match(/^\s*(#{1,6})\s+(.*)$/);
    if(h){const hd=el('h'+h[1].length);mdInline(h[2]).forEach(n=>hd.appendChild(n));frag.appendChild(hd);i++;continue;}
    if(/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)){frag.appendChild(el('hr'));i++;continue;}
    if(/^\s*>/.test(line)){
      const buf=[];
      while(i<lines.length&&/^\s*>/.test(lines[i])){buf.push(lines[i].replace(/^\s*>\s?/,''));i++;}
      const bq=el('blockquote');bq.appendChild(mdToDom(buf.join('\n'),depth+1));frag.appendChild(bq);continue;
    }
    if(line.includes('|')&&i+1<lines.length&&/^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/.test(lines[i+1])){
      const row=r=>r.replace(/^\s*\|/,'').replace(/\|\s*$/,'').split('|').map(c=>c.trim());
      const table=el('table','md-table'),thead=el('thead'),trh=el('tr');
      row(line).forEach(c=>{const th=el('th');mdInline(c).forEach(n=>th.appendChild(n));trh.appendChild(th);});
      thead.appendChild(trh);table.appendChild(thead);i+=2;
      const tb=el('tbody');
      while(i<lines.length&&lines[i].includes('|')&&!/^\s*$/.test(lines[i])){
        const tr=el('tr');row(lines[i]).forEach(c=>{const td=el('td');mdInline(c).forEach(n=>td.appendChild(n));tr.appendChild(td);});
        tb.appendChild(tr);i++;
      }
      table.appendChild(tb);frag.appendChild(table);continue;
    }
    if(/^\s*([-*+]|\d+[.)])\s+/.test(line)){
      const ordered=/^\s*\d+[.)]\s+/.test(line);
      const listEl=el(ordered?'ol':'ul');
      while(i<lines.length&&/^\s*([-*+]|\d+[.)])\s+/.test(lines[i])){
        let item=lines[i].replace(/^\s*([-*+]|\d+[.)])\s+/,'');i++;
        while(i<lines.length&&/^\s+\S/.test(lines[i])&&!/^\s*([-*+]|\d+[.)])\s+/.test(lines[i])){item+=' '+lines[i].trim();i++;}
        const li=el('li');mdInline(item).forEach(n=>li.appendChild(n));listEl.appendChild(li);
      }
      frag.appendChild(listEl);continue;
    }
    const buf=[line];i++;
    while(i<lines.length&&!/^\s*$/.test(lines[i])&&!isBlockStart(lines[i])){buf.push(lines[i]);i++;}
    const p=el('p');
    buf.forEach((pl,idx)=>{if(idx)p.appendChild(el('br'));mdInline(pl).forEach(n=>p.appendChild(n));});
    frag.appendChild(p);
  }
  return frag;
}
function mdNode(src){
  const d=el('div','md');
  // never let a renderer fault (e.g. deep recursion) blank the turn — fall back
  // to the raw text, which is always fully visible
  try{d.appendChild(mdToDom(src));}
  catch(e){d.replaceChildren(el('pre','md-code',src||''));}
  return d;
}

const IMGMIME=['image/png','image/jpeg','image/gif','image/webp'];
function imageNode(p){
  if(IMGMIME.includes(p.media_type)&&/^[A-Za-z0-9+\/=\s]*$/.test(p.data||'')&&p.data){
    const img=document.createElement('img');img.className='pimg';img.loading='lazy';
    img.src='data:'+p.media_type+';base64,'+p.data.replace(/\s+/g,'');
    return img;
  }
  const why=p.source_type&&p.source_type!=='base64'
    ?('unsupported source "'+p.source_type+'"'):'not inlined';
  return el('div','stub','[image · '+(p.media_type||'unknown type')+' — '+why+']');
}

function inputHint(input){
  try{const s=JSON.stringify(input);return s&&s!=='null'?' · '+oneline(s,90):'';}
  catch(e){return'';}
}

function agentNode(p,ctx){
  ctx=ctx||{};
  const sub=p.subagent;
  const d=document.createElement('details');d.className='fold agent';
  if(sub&&sub.agent_id)d.dataset.agentCall=sub.agent_id;  // jump target for the nav
  const s=document.createElement('summary');
  let label='⛭ sub-agent';
  if(sub&&sub.agent_type)label+=' · '+sub.agent_type;
  if(sub&&sub.description)label+=' — '+oneline(sub.description,110);
  if(!sub)label+=inputHint(p.input);
  s.appendChild(el('span','flabel',label));
  if(p.input!=null)s.appendChild(copyBtn(()=>jstr(p.input)));  // copy the Agent task input
  d.appendChild(s);
  const body=el('div','abody');
  body.appendChild(foldBlock('tool','task prompt',jstr(p.input),jstr(p.input)));
  if(sub&&sub.agent_id){
    // open in the right split-pane, not inline / not a new tab. Inside the pane
    // (ctx.pane) a nested Agent pushes onto the pane stack.
    const btn=el('button','viewbtn','open transcript →');
    btn.dataset.agentBtn=sub.agent_id;
    btn.onclick=()=>{ctx.pane?panePush(sub):openAgentPane(sub,false);};
    body.appendChild(btn);
  }else{
    body.appendChild(el('div','stub','sub-agent transcript unavailable'));
  }
  d.appendChild(body);return d;
}

// --- sub-agent split-pane -------------------------------------------------
function markActiveCall(aid){
  document.querySelectorAll('#main .viewbtn.active').forEach(b=>b.classList.remove('active'));
  document.querySelectorAll('.aitem.cur').forEach(e=>e.classList.remove('cur'));
  if(!aid)return;
  const btn=document.querySelector('#main [data-agent-btn="'+aid+'"]');
  if(btn)btn.classList.add('active');
  const li=document.querySelector('.aitem[data-agent-id="'+aid+'"]');
  if(li)li.classList.add('cur');
}
function flash(node){node.classList.remove('flash');void node.offsetWidth;node.classList.add('flash');}
function openAgentPane(sub,jumpToCall){
  // fresh stack; highlight the originating call and (optionally) scroll to it
  paneStack=[sub];
  markActiveCall(sub.agent_id);
  const call=document.querySelector('#main [data-agent-call="'+sub.agent_id+'"]');
  if(call){call.open=true;if(jumpToCall){call.scrollIntoView({block:'center'});flash(call);}}
  renderPane();
}
function panePush(sub){paneStack.push(sub);renderPane();}
function panePop(){paneStack.pop();paneStack.length?renderPane():closePane();}
let paneSeq=0;  // bumped on every render/close so a stale fetch can't paint a closed pane
function closePane(){
  paneStack=[];paneSeq++;document.body.classList.remove('paneopen');
  const pane=$('#apane');pane.classList.remove('open');
  pane.replaceChildren();  // don't keep a large hidden sub-agent transcript mounted
  markActiveCall(null);
}
function renderPane(){
  const pane=$('#apane');pane.classList.add('open');document.body.classList.add('paneopen');
  const cur=paneStack[paneStack.length-1];
  pane.replaceChildren();
  const head=el('div','ahead');
  if(paneStack.length>1){const b=el('button','pbtn','←');b.title='back';b.onclick=panePop;head.appendChild(b);}
  const tt=el('div','atitle');
  tt.appendChild(el('div','atype','⛭ '+(cur.agent_type||'sub-agent')));
  if(cur.description)tt.appendChild(el('div','adesc2',cur.description));
  head.appendChild(tt);
  const x=el('button','pbtn','✕');x.title='close';x.onclick=closePane;head.appendChild(x);
  pane.appendChild(head);
  const body=el('div','abody2');body.appendChild(el('div','empty','loading sub-agent transcript…'));
  pane.appendChild(body);
  pane.scrollTop=0;
  loadAgentInto(cur.agent_id,body,++paneSeq);
}
function loadAgentInto(aid,body,seq){
  fetch('api/session?id='+encodeURIComponent(SID)+'&agent='+encodeURIComponent(aid))
    .then(async r=>{if(!r.ok){let m='HTTP '+r.status;
        try{const j=await r.json();if(j.error)m=j.error;}catch(e){}
        throw new Error(m);}return r.json();})
    .then(j=>{if(seq!==paneSeq)return;  // superseded (pane closed or navigated on)
      body.replaceChildren();
      indexToolNames(j.turns);  // ids are uuids — no collision with the parent's
      if(!j.turns.length){body.appendChild(el('div','empty','(empty transcript)'));return;}
      j.turns.forEach(t=>{try{body.appendChild(turnNode(t,false,{pane:true}));}
        catch(e){body.appendChild(el('div','stub','⚠ could not render turn '+t.i+': '+e.message));}});})
    .catch(e=>{if(seq!==paneSeq)return;body.replaceChildren(el('div','err','could not load: '+e.message));});
}

function jstr(v){try{return JSON.stringify(v,null,2);}catch(e){return String(v);}}

function partNode(p,ctx){
  switch(p.kind){
    case 'text':return (ctx&&ctx.md)?mdNode(p.text):el('div','ptext',p.text);
    case 'thinking':return foldBlock('thinking','✦ thinking · '+oneline(p.text,80),p.text,p.text);
    case 'tool_use':
      if(p.name==='Agent')return agentNode(p,ctx);
      return foldBlock('tool','⚙ '+(p.name||'tool')+inputHint(p.input),jstr(p.input),jstr(p.input));
    case 'tool_result':{
      const d=document.createElement('details');
      d.className='fold result'+(p.is_error?' err':'');
      const name=TOOLNAME[p.tool_use_id]||'';
      const s=document.createElement('summary');
      const preview=p.parts?'':oneline(p.text,90);
      s.appendChild(el('span','flabel',(p.is_error?'✗ ':'→ ')+(name||'result')+(p.is_error?' · error':'')+(preview?' · '+preview:'')));
      // verbatim: string content copies as-is; list content joins its text
      // parts without trimming (button shown only when there's real content)
      const rout=p.parts?p.parts.map(x=>x.kind==='text'?x.text:'').join('\n'):(p.text||'');
      if(rout.trim())s.appendChild(copyBtn(()=>rout));
      d.appendChild(s);
      if(p.parts){const b=el('div','fbody');p.parts.forEach(x=>b.appendChild(partNode(x,ctx)));d.appendChild(b);}
      else d.appendChild(el('pre',null,p.text||''));
      return d;}
    case 'image':return imageNode(p);
    case 'document':return el('div','stub','[document · '+(p.media_type||'unknown type')+']');
    case 'tool_reference':return el('div','stub','↪ tool reference: '+p.tool_name);
    case 'fallback':return el('div','marker','⇄ model fallback: '+p.from_model+' → '+p.to_model);
    default:return foldBlock('unknown','⚠ unrecognized block · '+(p.raw_type||'?'),jstr(p),jstr(p));
  }
}

function turnNode(t,anchor,ctx){
  ctx=ctx||{};
  const isPrompt=t.is_prompt;
  let cls='turn ';
  if(t.event)cls+='event'+(t.event==='injected'?' injected':'');
  else if(t.role==='system')cls+='sys';
  else if(isPrompt)cls+='prompt';
  else if(t.role==='assistant')cls+='assistant';
  else cls+='toolio';
  const d=el('article',cls);
  if(anchor)d.id='t'+t.i;
  if(t.event==='task_notification'){taskNotifNode(d,t);return d;}
  if(t.event==='injected'){injectedNode(d,t);return d;}
  if(t.role==='system'){
    const label='◈ '+(t.subtype||'system').replace(/_/g,' ')+(t.ts?' · '+hhmm(t.ts):'');
    const text=(t.parts||[]).map(p=>p.text||'').join('\n');
    d.appendChild(foldBlock('sysm',label+' · '+oneline(text,90),text,text));
    return d;
  }
  if(isPrompt){
    // elevator (up = prev prompt, down = next); only in the main transcript —
    // the sidebar nav tracks the main session, not a pane's sub-conversation
    if(!ctx.pane){
      const elev=el('div','elev');
      const up=el('button',null,'▲');up.title='previous prompt';
      const dn=el('button',null,'▼');dn.title='next prompt';
      up.onclick=()=>stepPrompt(t.i,-1);dn.onclick=()=>stepPrompt(t.i,1);
      elev.append(up,dn);d.appendChild(elev);
    }
    const pbody=el('div','pbody');
    pbody.appendChild(turnHead('you'+(t.ts?' · '+hhmm(t.ts):''),t));
    (t.parts||[]).forEach(p=>pbody.appendChild(partNode(p,ctx)));  // prompts stay plain (raw markdown visible)
    d.appendChild(pbody);
    return d;
  }
  if(t.role==='assistant'){
    d.appendChild(turnHead('claude'+(t.ts?' · '+hhmm(t.ts):''),t));
    // agent output renders as Markdown; the copy button still yields raw source
    const actx=Object.assign({},ctx,{md:true});
    (t.parts||[]).forEach(p=>d.appendChild(partNode(p,actx)));
    return d;
  }
  (t.parts||[]).forEach(p=>d.appendChild(partNode(p,ctx)));
  return d;
}

function turnHead(label,t){
  const h=el('div','thead');h.appendChild(el('span','tlabel',label));
  const rt=rawText(t);if(rt)h.appendChild(copyBtn(()=>rt));  // copy the turn's raw text
  return h;
}

// A harness-injected background-task notice (not a human prompt). Raw content is
// kept verbatim but split for readability: the leading <task-notification> XML
// metadata block is pretty-printed with coloured tags, and any Markdown body
// after the closing tag renders as Markdown (the same renderer as assistant text).
function taskNotifNode(d,t){
  const raw=rawText(t);
  const close='</task-notification>';
  const i=raw.indexOf(close);
  const xml=(i===-1?raw:raw.slice(0,i+close.length)).trim();
  const body=(i===-1?'':raw.slice(i+close.length)).trim();
  const status=(xml.match(/<status>([^<]*)<\/status>/)||[])[1]||'';
  d.appendChild(turnHead('⚙ background task'+(status?' · '+status:'')+(t.ts?' · '+hhmm(t.ts):''),t));
  const pre=el('pre','xmlblock');xmlToPre(pre,xml);d.appendChild(pre);
  if(body)d.appendChild(mdNode(body));
}

// A harness-injected prose turn (a /loop or scheduled prompt replayed on wake, a
// skill preamble, "Continue…") — real content the user wants to read, but NOT
// typed here, so it wears an "injected" badge instead of the human-prompt panel.
// Content renders through partNode (plain, like a prompt body) so text/image/
// document parts all show; nothing is dropped.
function injectedNode(d,t){
  const head=el('div','ihead');
  head.appendChild(el('span','badge','injected'));
  if(t.ts)head.appendChild(el('span','ihint',hhmm(t.ts)));
  const rt=rawText(t);if(rt)head.appendChild(copyBtn(()=>rt));
  d.appendChild(head);
  const body=el('div','ibody');
  (t.parts||[]).forEach(p=>body.appendChild(partNode(p,{})));
  d.appendChild(body);
}

// Pretty-print an XML-ish snippet into `pre` as coloured spans, indented by
// nesting depth. DOM-only (createElement/createTextNode) — no innerHTML — so the
// untrusted tag/value text can never be parsed as HTML. An <open>text</close>
// triple stays on one line; bare open tags indent their children.
function xmlToPre(pre,xml){
  const toks=[];const re=/<\/?[^>]*>/g;let last=0,m;
  while((m=re.exec(xml))){
    if(m.index>last){const s=xml.slice(last,m.index).trim();if(s)toks.push({k:'text',v:s});}
    const rawTag=m[0];
    let k='open';
    if(rawTag.startsWith('</'))k='close';else if(rawTag.endsWith('/>'))k='self';
    toks.push({k,v:rawTag});last=re.lastIndex;
  }
  if(last<xml.length){const s=xml.slice(last).trim();if(s)toks.push({k:'text',v:s});}
  let depth=0,first=true;
  const line=()=>{if(!first)pre.appendChild(document.createTextNode('\n'));
    pre.appendChild(document.createTextNode('  '.repeat(depth)));first=false;};
  const tag=v=>pre.appendChild(el('span','xtag',v));
  const val=v=>pre.appendChild(el('span','xval',v));
  for(let j=0;j<toks.length;j++){
    const tk=toks[j];
    if(tk.k==='close'){depth=Math.max(0,depth-1);line();tag(tk.v);continue;}
    if(tk.k==='self'){line();tag(tk.v);continue;}
    if(tk.k==='text'){line();val(tk.v);continue;}
    // open tag: keep <open>text</close> or empty <open></close> on one line
    if(toks[j+1]&&toks[j+1].k==='text'&&toks[j+2]&&toks[j+2].k==='close'){
      line();tag(tk.v);val(toks[j+1].v);tag(toks[j+2].v);j+=2;continue;}
    if(toks[j+1]&&toks[j+1].k==='close'){line();tag(tk.v);tag(toks[j+1].v);j+=1;continue;}
    line();tag(tk.v);depth++;
  }
}

function renderMeta(){
  const m=DATA.meta;
  $('#proj').textContent=m.project||'';
  if(m.title){$('#ttl').textContent=m.title;$('#ttl').style.display='';}
  const nsub=(DATA.subagents||[]).length;
  $('#metabits').textContent='started '+abs(m.started_ts)+' · last '+rel(m.updated_ts)
    +' · '+m.msgs+' msgs ('+m.user_msgs+'u·'+m.asst_msgs+'a)'+(nsub?' · '+nsub+' sub-agents':'');
  if(m.resume){const b=$('#resume');b.style.display='';
    b.onclick=()=>navigator.clipboard.writeText(m.resume).then(()=>toast('Copied resume command'));}
  if(m.agent_id){
    const bar=$('#agentbar');bar.style.display='';bar.replaceChildren();
    bar.appendChild(el('b',null,'sub-agent'));
    bar.appendChild(document.createTextNode(' '+(m.agent_type||'')+(m.description?' — '+m.description:'')+' · '));
    const a=el('a',null,'parent session');a.href='session?id='+encodeURIComponent(SID);
    bar.appendChild(a);
    document.title='agent · '+(m.agent_type||m.agent_id)+' — '+(m.project||'');
  }else{
    document.title=(m.project||'session')+(m.title?' · '+m.title:' · #'+(m.id||'').slice(-4));
  }
}

function indexToolNames(turns){
  turns.forEach(t=>(t.parts||[]).forEach(p=>{
    if(p.kind==='tool_use'&&p.id)TOOLNAME[p.id]=p.name;}));
}

function renderTurns(){
  TOOLNAME={};
  indexToolNames(DATA.turns);
  const main=$('#main');main.replaceChildren();
  if(!DATA.turns.length){main.appendChild(el('div','empty','No renderable turns in this transcript.'));return;}
  // isolate per turn: one bad turn surfaces a visible marker, never blanks the rest
  DATA.turns.forEach(t=>{try{main.appendChild(turnNode(t,true));}
    catch(e){main.appendChild(el('div','stub','⚠ could not render turn '+t.i+': '+e.message));}});
}

function renderNav(){
  PROMPTS=DATA.turns.filter(t=>t.is_prompt);
  $('#pcount').textContent=PROMPTS.length?'· '+PROMPTS.length:'';
  const box=$('#plist');box.replaceChildren();
  if(!PROMPTS.length)box.appendChild(el('div','hint','no prompts'));
  PROMPTS.forEach((t,idx)=>{
    const d=el('div','pitem');d.title=abs(t.ts);
    d.appendChild(el('div','plabel',t.label||'[media]'));
    d.appendChild(el('div','ptime',rel(t.ts)));
    d.onclick=()=>jumpPrompt(idx);
    box.appendChild(d);
  });
}

function renderAgents(){
  const list=DATA.subagents||[];
  $('#asect').style.display=list.length?'':'none';
  $('#acount').textContent=list.length?'· '+list.length:'';
  const box=$('#alist');box.replaceChildren();
  list.forEach(a=>{
    const d=el('div','aitem');d.dataset.agentId=a.agent_id;d.title=abs(a.updated_ts);
    d.appendChild(el('div','atype','⛭ '+(a.agent_type||'agent')));
    if(a.description)d.appendChild(el('div','adesc',a.description));
    // open in the right pane and jump the main transcript to the call site
    d.onclick=()=>openAgentPane(a,true);
    box.appendChild(d);
  });
}

function stepPrompt(turnI,delta){
  const idx=PROMPTS.findIndex(p=>p.i===turnI);
  jumpPrompt((idx<0?curPrompt:idx)+delta);
}

function jumpPrompt(idx){
  if(!PROMPTS.length)return;
  curPrompt=(idx+PROMPTS.length)%PROMPTS.length;
  document.querySelectorAll('.pitem').forEach((e,i)=>e.classList.toggle('cur',i===curPrompt));
  const li=document.querySelectorAll('.pitem')[curPrompt];
  if(li)li.scrollIntoView({block:'nearest'});
  const t=document.getElementById('t'+PROMPTS[curPrompt].i);
  if(t)t.scrollIntoView({block:'start'});
}

function turnSearchText(t,all){
  let s='';
  const walk=ps=>ps.forEach(p=>{
    // default scope = human prompts + assistant text; wrapper-only user turns
    // (is_prompt:false but still rendered) and system markers are all-scope only
    if(p.kind==='text'){if(t.role==='assistant'||(t.role==='user'&&t.is_prompt)||all)s+=' '+p.text;}
    else if(all){
      if(p.kind==='thinking')s+=' '+p.text;
      else if(p.kind==='tool_use')s+=' '+(p.name||'')+' '+jstr(p.input);
      else if(p.kind==='tool_result'){if(p.parts)walk(p.parts);else s+=' '+(p.text||'');}
      else if(p.kind==='tool_reference')s+=' '+(p.tool_name||'');
    }
  });
  // default scope = human prompts + assistant text; tool deliveries have no
  // text parts, so they drop out naturally
  if(t.role==='system'&&!all)return'';
  walk(t.parts||[]);
  return s;
}

function runSearch(){
  const q=$('#q').value.trim().toLowerCase();
  document.querySelectorAll('.turn.hit').forEach(e=>e.classList.remove('hit'));
  document.querySelectorAll('.turn.hitcur').forEach(e=>e.classList.remove('hitcur'));
  HITS=[];curHit=-1;
  if(!q){$('#qcount').textContent='';return;}
  const all=$('#qall').checked;
  DATA.turns.forEach(t=>{
    if(turnSearchText(t,all).toLowerCase().includes(q))HITS.push(t.i);
  });
  HITS.forEach(i=>{const e=document.getElementById('t'+i);if(e)e.classList.add('hit');});
  $('#qcount').textContent=HITS.length?HITS.length+' turns':'no match';
  if(HITS.length)gotoHit(0);
}

function gotoHit(n){
  if(!HITS.length)return;
  curHit=(n+HITS.length)%HITS.length;
  document.querySelectorAll('.turn.hitcur').forEach(e=>e.classList.remove('hitcur'));
  const e=document.getElementById('t'+HITS[curHit]);
  if(!e)return;
  e.classList.add('hitcur');
  if($('#qall').checked)e.querySelectorAll('details').forEach(d=>d.open=true);
  e.scrollIntoView({block:'center'});
  $('#qcount').textContent=(curHit+1)+' / '+HITS.length+' turns';
}

$('#expand').onclick=()=>{
  allOpen=!allOpen;
  // agent folds no longer fetch on open (transcripts load in the pane), so
  // expand-all is safe to apply to every fold
  document.querySelectorAll('#main details').forEach(d=>{d.open=allOpen;});
  $('#expand').textContent=allOpen?'collapse all':'expand all';
};
$('#refresh').onclick=()=>load();
{let _t;$('#q').oninput=()=>{clearTimeout(_t);_t=setTimeout(runSearch,250);};}
$('#qall').onchange=runSearch;

document.addEventListener('keydown',e=>{
  const tag=(e.target.tagName||'').toLowerCase();
  if(tag==='input'||tag==='textarea'){
    if(e.key==='Escape')e.target.blur();
    if(e.key==='Enter'&&e.target.id==='q'){runSearch();if(HITS.length)gotoHit(e.shiftKey?curHit-1:curHit+1);}
    return;
  }
  if(e.metaKey||e.ctrlKey||e.altKey)return;
  if(e.key==='Escape'){closePane();return;}
  if(e.key==='/'){e.preventDefault();$('#q').focus();$('#q').select();}
  else if(e.key===']'||e.key==='j')jumpPrompt(curPrompt+1);
  else if(e.key==='['||e.key==='k')jumpPrompt(curPrompt-1);
  else if(e.key==='n')gotoHit(curHit+1);
  else if(e.key==='N')gotoHit(curHit-1);
});

async function load(){
  if(!SID){$('#main').replaceChildren(el('div','err','No session id — open this page from a dashboard row.'));return;}
  let r;
  try{
    r=await fetch('api/session?id='+encodeURIComponent(SID)
      +(AGENT?'&agent='+encodeURIComponent(AGENT):''));
  }catch(e){closePane();$('#main').replaceChildren(el('div','err','Could not reach the server: '+e.message));return;}
  if(!r.ok){
    let msg='HTTP '+r.status;
    try{const j=await r.json();if(j.error)msg=j.error+(j.matches?' — matches: '+j.matches.join(', '):'');}catch(e){}
    closePane();$('#main').replaceChildren(el('div','err','Could not load transcript: '+msg));
    return;
  }
  DATA=await r.json();
  closePane();  // a refetch rebuilds #main; drop any stale pane/highlight state
  renderMeta();renderTurns();renderNav();renderAgents();
  allOpen=false;$('#expand').textContent='expand all';
  runSearch();
}
load();
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200, gz=False):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        # gz: transcripts can be ~19 MB of JSON — gzip hard on the wire when the
        # client accepts it (browsers always do; keep the fallback for curl -s)
        if gz and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            body = gzip.compress(body)
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, page):
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path.startswith("/api/flag"):
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                body = {}
            sid = body.get("id")
            kind = body.get("kind", "flag")
            value = bool(body.get("value", body.get("flagged")))
            if not sid or kind not in MARK_KINDS:
                self._json({"ok": False, "error": "bad request"}, 400)
                return
            with _flags_lock, flags_write_lock():
                refresh_flags()  # don't clobber external writes (e.g. `--done`)
                marks = FLAGS.get(sid, {})
                if value:
                    marks[kind] = time.time()
                    # invariant: never both done + flag — setting one clears the other
                    marks.pop("done" if kind == "flag" else "flag", None)
                else:
                    marks.pop(kind, None)
                if marks:
                    FLAGS[sid] = marks
                else:
                    FLAGS.pop(sid, None)
                save_flags(FLAGS)
            self._json({"ok": True, "kind": kind, "value": value})
        else:
            self._json({"ok": False, "error": "not found"}, 404)

    def do_GET(self):
        # exact-path dispatch (urlsplit): "/api/sessions?…".startswith("/api/session")
        # is True, so the old startswith idiom would swallow the sessions poll
        u = urlsplit(self.path)
        if u.path == "/api/sessions":
            self._json(collect())
        elif u.path == "/api/session":
            self._api_session(u.query)
        elif u.path == "/session":
            self._html(TRANSCRIPT_PAGE)
        else:
            self._html(PAGE)

    def _api_session(self, query):
        """GET /api/session?id=<id>[&agent=<agentId>] → parsed transcript JSON.
        404 unknown id / missing file, 409 ambiguous fragment, 400 bad agent id."""
        q = parse_qs(query)
        token = (q.get("id") or [""])[0].strip()
        agent = (q.get("agent") or [""])[0].strip()
        if not token:
            self._json({"error": "missing id"}, 400)
            return
        sid, cands = _find_session_id(token)
        if not sid:
            if cands:
                self._json({"error": "ambiguous id", "matches": cands[:20]}, 409)
            else:
                self._json({"error": "unknown session id"}, 404)
            return
        path = _session_path(sid)
        if not path:
            self._json({"error": "unknown session id"}, 404)
            return
        if agent:
            # bare lowercase hex (NOT "agent-…" — the param has no prefix);
            # anything else is rejected: no path traversal
            if not _HEX_RE.match(agent):
                self._json({"error": "bad agent id"}, 400)
                return
            apath = os.path.join(_subagent_dir(path), "agent-%s.jsonl" % agent)
            if not os.path.isfile(apath):
                self._json({"error": "sub-agent transcript not found"}, 404)
                return
            r = parse_transcript(apath)
            if not r:
                self._json({"error": "could not read sub-agent transcript"}, 404)
                return
            m = _read_meta(_subagent_dir(path), agent)  # one file, not the dir
            # shallow copies: never mutate the cached result. Panel lists this
            # sub-agent's OWN children (from its Agent-call linkage) — a dir glob
            # would list its siblings, since storage is flat.
            r = dict(r, meta=dict(r["meta"], id=sid, agent_id=agent,
                                  agent_type=str(m.get("agentType") or ""),
                                  description=str(m.get("description") or "")),
                     subagents=_linked_subagents(r, _subagent_dir(path)))
            self._json(r, gz=True)
        else:
            r = parse_transcript(path)
            if not r:
                self._json({"error": "could not read session"}, 404)
                return
            # subagents inventory is filesystem-derived and changes independently
            # of the .jsonl, so it's attached per request, not cached
            r = dict(r, subagents=list_subagents(path))
            self._json(r, gz=True)


def write_static():
    data = collect()

    def must_replace(page, old, new):
        # crash loudly if PAGE drifted from the replace target — a silent no-op
        # would ship a snapshot that still tries to fetch / shows dead links
        out = page.replace(old, new)
        if out == page:
            raise SystemExit("write_static: replace target not found in PAGE "
                             "(PAGE drifted?): %r" % old[:60])
        return out

    inlined = must_replace(
        PAGE,
        "async function load(){\n  let r=await fetch('api/sessions?t='+Date.now());let j=await r.json();",
        "async function load(){\n  let j=" + json.dumps(data) + ";",
    )
    # suppress per-row "view" links: a file:// snapshot has no /session route
    inlined = must_replace(inlined, "const STATIC=false;", "const STATIC=true;")
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
    with open(out, "w") as fh:
        fh.write(inlined)
    print("Wrote", out)
    return out


# Session ids (and the statusline last-4 fragment) are UUIDs — hex + hyphen
# only. Gating every token to this charset before it reaches a glob blocks
# path traversal: '/' and '.' can't appear, so `../` can't escape PROJECTS.
# glob.escape neutralizes glob metachars but NOT separators, so it is not a
# traversal defense on its own. Verified: all 274 local ids match.
_SID_RE = re.compile(r"[0-9a-fA-F-]+\Z")


def _session_path(sid):
    """Absolute path to a session's .jsonl, or None if not found locally.
    Rejects any token outside the session-id charset (path-traversal guard)."""
    if not sid or not _SID_RE.match(sid):
        return None
    m = glob.glob(os.path.join(PROJECTS, "*", glob.escape(sid) + ".jsonl"))
    return m[0] if m else None


def _find_session_id(token):
    """Non-raising resolver: full id or trailing fragment → (sid|None, candidates).
    sid is set iff the match is unique; candidates lets callers distinguish
    none ([]) from ambiguous (2+). The HTTP handler needs this form —
    resolve_session_id raises SystemExit, which would kill a request thread."""
    if not token or not _SID_RE.match(token):  # charset gate before any glob
        return None, []
    if _session_path(token):  # already a full id with a local file
        return token, [token]
    cands = sorted({os.path.basename(p)[:-6]
                    for p in glob.glob(os.path.join(PROJECTS, "*", "*" + glob.escape(token) + ".jsonl"))})
    return (cands[0] if len(cands) == 1 else None), cands


def resolve_session_id(token):
    """Resolve a full session id, or a trailing fragment (e.g. the statusline's
    last-4), to a full session id. Exits with a clear message on no/ambiguous
    match rather than guessing."""
    sid, cands = _find_session_id(token)
    if sid:
        return sid
    if not cands:
        raise SystemExit("No local session matches %r." % token)
    raise SystemExit("Ambiguous %r — matches %d sessions:\n  %s\n"
                     "Use more characters or the full id."
                     % (token, len(cands), "\n  ".join(cands)))


def mark_done_cli(token):
    """Mark a session `done` (work complete) from the CLI and exit.

    With no token, mark the *current* session via $CLAUDE_CODE_SESSION_ID (set in
    every Claude Code shell), so `--done` inside a session needs no argument.
    Otherwise `token` is a full id or a trailing fragment (statusline last-4).
    The read-modify-write is guarded by flags_write_lock() (cross-process flock),
    so a simultaneous UI toggle can't cause a lost update; save_flags writes a
    unique temp + atomic os.replace. A running dashboard reflects the mark on its
    next scan via refresh_flags(). Setting done also clears any reopen flag."""
    # tolerate a leading '#' (the statusline prints "#1234") and stray whitespace
    token = (token or "").strip().lstrip("#").strip()
    if token:
        sid = resolve_session_id(token)
    else:
        sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
        if not sid:
            raise SystemExit(
                "No session id. Run this inside a Claude Code session (it reads "
                "$CLAUDE_CODE_SESSION_ID), or pass one: --done <id-or-last4>")
    with flags_write_lock():
        refresh_flags()
        marks = FLAGS.get(sid, {})
        # "done" means finished/nothing-pending, the opposite of "flag"
        # (reopen-later) — so clear any stale reopen flag rather than leave the
        # session both hidden-as-done and counted-as-flagged.
        cleared_flag = marks.pop("flag", None) is not None
        marks["done"] = time.time()
        FLAGS[sid] = marks
        save_flags(FLAGS)
    label = "#" + sid[-4:]
    path = _session_path(sid)
    if path:
        s = parse_session(path)
        if s and s.get("project"):
            label += "  [%s]" % s["project"]
    else:
        # warn, don't fail: a brand-new session's transcript may not be flushed
        # yet (the mark correctly waits for the row), or this is a non-interactive
        # session whose id isn't a local file. Surface it rather than silently
        # printing an unqualified success.
        print("  ⚠ no local transcript found for this id yet — the mark is saved "
              "and will apply once the session's row appears.", file=sys.stderr)
    print("✓ Marked done: %s%s" % (label, " (cleared its reopen flag)" if cleared_flag else ""))
    print("  Hidden from the dashboard's default view; still resumable "
          "(claude --resume %s)." % sid)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7878)
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--once", action="store_true", help="write a static index.html and exit")
    ap.add_argument("--done", nargs="?", const="", metavar="SESSION",
                    help="mark a session 'done' (work complete) and exit; no arg = "
                         "current session via $CLAUDE_CODE_SESSION_ID, or pass a full "
                         "id / trailing fragment (e.g. the statusline's last-4)")
    args = ap.parse_args()

    if args.done is not None:  # mark-done CLI: no server, doesn't need PROJECTS to exist
        mark_done_cli(args.done)
        return

    if not os.path.isdir(PROJECTS):
        print("No ~/.claude/projects directory found.", file=sys.stderr)
        sys.exit(1)

    if args.once:
        out = write_static()
        if not args.no_open:
            webbrowser.open("file://" + out)
        return

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = "http://127.0.0.1:%d" % args.port
    n = len(collect()["sessions"])
    print("claude-status serving %d sessions at %s  (Ctrl-C to stop)" % (n, url))
    if not args.no_open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
