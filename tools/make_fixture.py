#!/usr/bin/env python3
"""Generate a throwaway ~/.claude tree of *fabricated* Claude Code sessions so
the dashboard can be screenshotted (docs/screenshot.png) with zero real or
private data.

Everything here is invented — project names, prompts, ids. It writes into an
isolated $FIXTURE_HOME, then you point the dashboard at that HOME:

    cd tools
    FIXTURE_HOME="$PWD/fixture-home" python3 make_fixture.py
    HOME="$PWD/fixture-home" python3 ../claude-status.py --port 7900

Because claude-status.py resolves PROJECTS/SESSIONS/FLAGS through
os.path.expanduser("~/..."), overriding $HOME fully isolates it from your real
~/.claude. Regenerate whenever the dashboard's look changes and a fresh
screenshot is needed.

The script also spawns a few short-lived helper processes that hold real RAM
(and touch every page so it's actually resident) with matching
~/.claude/sessions/<pid>.json files, so the RAM column and header chip render
realistic numbers. It prints the pids and the `kill` line to stop them.
"""
import json, os, sys, random, subprocess
from datetime import datetime, timezone, timedelta

HOME = os.path.abspath(os.environ.get("FIXTURE_HOME", "fixture-home"))
PROJECTS = os.path.join(HOME, ".claude", "projects")
SESSIONS = os.path.join(HOME, ".claude", "sessions")
CONFIG = os.path.join(HOME, ".config", "claude-sessions")
for d in (PROJECTS, SESSIONS, CONFIG):
    os.makedirs(d, exist_ok=True)

rnd = random.Random(1234)  # deterministic fixture
now = datetime.now(timezone.utc)

def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

def sid():
    return "%08x-%04x-%04x-%04x-%012x" % (
        rnd.getrandbits(32), rnd.getrandbits(16), rnd.getrandbits(16),
        rnd.getrandbits(16), rnd.getrandbits(48))

def encdir(cwd):
    return cwd.replace("/", "-").replace(".", "-").lstrip("-")

MODELS = ["claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5-20251001"]
BASE = "/home/dev/Repositories/GitHub"

# (project, branch, title-or-None, first prompt, n_user, n_asst, out_tok, model_idx, mins_ago)
SPEC = [
    ("acme-web",        "main",            "checkout redesign",  "Refactor the cart drawer so the totals update without a full re-render.", 14, 13, 48200, 0, 3),
    ("data-pipeline",   "fix/backfill",    None,                 "The nightly backfill is dropping rows when a partition is empty — can you trace where?", 9, 9, 31700, 1, 8),
    ("ml-playground",   "feature/eval",    "eval harness",       "Wire up the eval harness to score the new prompt variants against the golden set.", 22, 21, 91400, 0, 41),
    ("acme-api",        "main",            None,                 "Add rate limiting to the public /search endpoint and cover it with tests.", 7, 7, 22800, 1, 88),
    ("notes-app",       "main",            "offline sync",       "Make notes editable offline and reconcile on reconnect without clobbering.", 18, 17, 63900, 0, 150),
    ("infra-terraform", "chore/upgrade",   None,                 "Upgrade the RDS module to the new major version and plan the migration.", 5, 5, 14300, 2, 320),
    ("docs-site",       "main",            None,                 "Turn the getting-started guide into a runnable tutorial with copy buttons.", 11, 10, 28600, 1, 640),
    ("acme-web",        "feature/search",  "typeahead",          "Build a typeahead for the product search box, debounced, keyboard-navigable.", 16, 15, 55100, 0, 900),
    ("data-pipeline",   "main",            None,                 "Document the schema for the events table and add a freshness check.", 6, 6, 17400, 1, 1180),
]

# which rows are "open" (index -> status); those get a live process + RAM
OPEN = {0: "busy", 1: "idle", 2: "idle"}
# hold real, resident RAM so the RAM column / header chip look realistic (MB)
RAM_MB = {0: 420, 1: 360, 2: 300}
FLAG_ROWS = {1, 5}   # ⚑ flagged (reboot-survival demo)

live_pids = []
sids = []
for i, (proj, branch, title, prompt, nu, na, out_tok, mi, mins) in enumerate(SPEC):
    cwd = "%s/%s" % (BASE, proj)
    s = sid(); sids.append(s)
    ddir = os.path.join(PROJECTS, encdir(cwd))
    os.makedirs(ddir, exist_ok=True)
    start = now - timedelta(minutes=mins + nu * 4)
    lines = []
    # first record carries cwd/branch/version/entrypoint (interactive = "cli")
    lines.append({"type": "user", "cwd": cwd, "gitBranch": branch,
                  "version": "2.0.14", "entrypoint": "cli",
                  "timestamp": iso(start),
                  "message": {"role": "user", "content": prompt}})
    if title:
        lines.append({"type": "user", "cwd": cwd, "timestamp": iso(start + timedelta(seconds=5)),
                      "message": {"role": "user",
                                  "content": '<system-reminder>named this session "%s"</system-reminder>' % title}})
    t = start
    for k in range(max(nu, na)):
        t = t + timedelta(minutes=rnd.randint(2, 6))
        if k < na:
            lines.append({"type": "assistant", "cwd": cwd, "timestamp": iso(t),
                          "message": {"role": "assistant", "model": MODELS[mi],
                                      "usage": {"output_tokens": out_tok // na,
                                                "input_tokens": rnd.randint(800, 3000),
                                                "cache_read_input_tokens": rnd.randint(20000, 90000),
                                                "cache_creation_input_tokens": rnd.randint(1000, 8000)}}})
        if k + 1 < nu:
            t = t + timedelta(minutes=rnd.randint(1, 4))
            lines.append({"type": "user", "cwd": cwd, "timestamp": iso(t),
                          "message": {"role": "user", "content": "follow-up %d" % (k + 1)}})
    # pin the last timestamp to exactly "mins" ago (drives the recency colour)
    lines[-1]["timestamp"] = iso(now - timedelta(minutes=mins))
    with open(os.path.join(ddir, s + ".jsonl"), "w") as fh:
        for o in lines:
            fh.write(json.dumps(o) + "\n")

    if i in OPEN:
        mb = RAM_MB[i]
        # Hold memory that actually stays resident in `ps -o rss=` (what the
        # dashboard reads). Two OS behaviours fight this: a zero-filled buffer
        # gets collapsed (macOS memory compressor / Linux shared zero page),
        # and *idle* pages get evicted/swapped even when incompressible. So:
        # fill with incompressible os.urandom AND keep the pages hot with a
        # cheap strided re-read once a second (one access per 4 KB page marks
        # it recently-used → not reclaimed; ~size/4096 reads/s, negligible CPU).
        code = ("import os, time\n"
                "x = os.urandom(%d * 1024 * 1024)\n"
                "while True:\n"
                "    s = 0\n"
                "    for i in range(0, len(x), 4096): s += x[i]\n"
                "    time.sleep(1)\n") % mb
        # Detach (own session, no inherited stdio) so the helper never holds a
        # parent pipe open — otherwise a wrapping shell can hang on it.
        proc = subprocess.Popen([sys.executable, "-c", code],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL, start_new_session=True)
        pid = proc.pid
        live_pids.append(pid)
        with open(os.path.join(SESSIONS, "%d.json" % pid), "w") as fh:
            json.dump({"pid": pid, "sessionId": s, "cwd": cwd,
                       "status": OPEN[i], "kind": "cli", "name": title or ""}, fh)

# flags.json (⚑ flagged rows: key present = mark set)
flags = {sids[i]: {"flag": iso(now)} for i in FLAG_ROWS}
with open(os.path.join(CONFIG, "flags.json"), "w") as fh:
    json.dump(flags, fh)

print("FIXTURE_HOME=%s" % HOME)
print("sessions: %d   open(with RAM): %d   flagged: %d" % (len(SPEC), len(live_pids), len(FLAG_ROWS)))
print("\nServe it:\n  HOME=%s python3 ../claude-status.py --port 7900" % HOME)
print("\nStop the RAM-holding helper processes when done:\n  kill %s"
      % " ".join(str(p) for p in live_pids))
