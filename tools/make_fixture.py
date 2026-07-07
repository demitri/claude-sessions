#!/usr/bin/env python3
"""Generate a self-contained demo page of *fabricated* Claude Code sessions so
the dashboard can be screenshotted (docs/screenshot.png) with zero real or
private data — and without spawning processes or holding real RAM.

Everything here is invented — project names, prompts, ids, RAM figures. The
approach:

  1. Write an isolated $FIXTURE_HOME/.claude tree of invented session files.
  2. Run `claude-status.py --once` against that HOME to produce the real static
     page (reusing the app's own write_static(), so the HTML never drifts from
     the live dashboard when the UI changes).
  3. Inject open-state + RAM numbers straight into the page's inlined JSON.
     Those fields (open / live_status / rss_kb) are normally process-derived;
     faking them in the data is what lets the RAM column, the header RAM chip,
     and the green/pulsing "open" dots render without any live Claude process.

Output: $FIXTURE_HOME/demo.html — open it in a browser and screenshot. It's a
plain file:// page: no server, no processes, nothing to kill afterward.

Usage:
    cd tools
    FIXTURE_HOME="$PWD/fixture-home" python3 make_fixture.py
    open "$PWD/fixture-home/demo.html"     # then screenshot -> docs/screenshot.png

Re-run whenever the dashboard's look changes and a fresh screenshot is needed.
"""
import json, os, sys, random, subprocess
from datetime import datetime, timezone, timedelta

HOME = os.path.abspath(os.environ.get("FIXTURE_HOME", "fixture-home"))
PROJECTS = os.path.join(HOME, ".claude", "projects")
CONFIG = os.path.join(HOME, ".config", "claude-sessions")
APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "claude-status.py")
# write_static() always writes index.html next to claude-status.py:
STATIC_OUT = os.path.join(os.path.dirname(os.path.abspath(APP)), "index.html")
DEMO_OUT = os.path.join(HOME, "demo.html")
for d in (PROJECTS, CONFIG):
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

# index -> (live_status, rss_MB): the "open" sessions and their fabricated RAM.
OPEN = {0: ("busy", 420), 1: ("idle", 360), 2: ("idle", 300)}
FLAG_ROWS = {1, 5}   # ⚑ flagged (reboot-survival demo)

sids = []
inject = {}   # session id -> {"open", "live_status", "rss_kb"}
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
        status, mb = OPEN[i]
        inject[s] = {"open": True, "live_status": status, "rss_kb": mb * 1024}

# flags.json (⚑ flagged rows: key present = mark set)
flags = {sids[i]: {"flag": iso(now)} for i in FLAG_ROWS}
with open(os.path.join(CONFIG, "flags.json"), "w") as fh:
    json.dump(flags, fh)

# --- render the real static page via the app's own write_static(), then inject.
env = dict(os.environ, HOME=HOME)
subprocess.run([sys.executable, APP, "--once", "--no-open"], env=env, check=True)
html = open(STATIC_OUT, encoding="utf-8").read()
os.remove(STATIC_OUT)  # don't leave a snapshot in the repo tree

# The page inlines the dataset as `let j=<json>;`. Parse exactly that JSON
# object (raw_decode finds its end), rewrite the fabricated fields, splice back.
i = html.index("let j=") + len("let j=")
data, end = json.JSONDecoder().raw_decode(html, i)
hit = 0
for s in data["sessions"]:
    patch = inject.get(s["id"])
    if patch:
        s.update(patch)
        hit += 1
if hit != len(inject):
    raise SystemExit("inject: matched %d of %d open sessions (id mismatch?)" % (hit, len(inject)))
html = html[:i] + json.dumps(data) + html[end:]
# re-enable the per-row "view" links so the demo matches the live dashboard
# (they're dead on file://, but never clicked in a screenshot).
html = html.replace("const STATIC=true;", "const STATIC=false;")

with open(DEMO_OUT, "w", encoding="utf-8") as fh:
    fh.write(html)

total_gb = sum(p["rss_kb"] for p in inject.values()) / 1024 / 1024
print("\nsessions: %d   open+RAM: %d (%.2f GB)   flagged: %d"
      % (len(SPEC), len(inject), total_gb, len(FLAG_ROWS)))
print("demo page: %s" % DEMO_OUT)
print("open it and screenshot -> docs/screenshot.png:\n  open %s" % DEMO_OUT)
