# Claude Session Notifications & Inter-Session Communication

> Report for later pickup. Captures a session's investigation into how Claude
> Code sessions can **send** notifications, **receive** input, and **talk to
> each other** — with the real cost model spelled out.
> Date: 2026-06-21.

## TL;DR

- **Sending a push to your phone** is solved two ways: your own `pushover`
  script (reliable, no app pairing needed) and the harness `PushNotification`
  tool (needs Remote Control paired for phone delivery).
- **A session cannot be pushed to unprompted.** There is no per-session
  listening socket / inbox. A session is reactive — it only runs when invoked.
- **The cost driver is model invocation, not polling.** Background bash that
  sleeps or blocks costs ~zero tokens. The model wakes once, when an event
  fires.
- **Cheapest inter-session channel: a FIFO** (named pipe). Kernel-blocked read,
  zero CPU, zero polling, wakes instantly on write, one model invocation on
  delivery.

---

## 1. Sending notifications (the easy direction)

### Your own Pushover script
- `~/.local/bin/pushover [-p priority] [-t title] [-u url] message...`
- Credentials in `~/.config/pushover/config`; `Bash(pushover *)` is allowlisted
  globally (no permission prompt).
- Real Pushover push to all your devices. **Does not depend on the mobile app
  being paired.** This is the reliable phone-reaching path.
- Policy: send only on explicit per-task request (suggesting is fine). Lead with
  the actionable outcome. `-p 1` only if asked. Script exits non-zero on
  credential/API failure — report it, don't swallow it.

### Harness `PushNotification` tool
- Fires a desktop notification in the terminal; **also** pushes to phone **only
  if Remote Control is connected** (mobile app paired to the session).
- Separate from Pushover. Generic "pull attention back" mechanism.
- Best when you're loosely watching the same machine; Pushover is better when
  you've walked away.

---

## 2. Receiving / reacting (the hard direction)

A session has **no idle event loop on a socket**. The "external service fires a
webhook → my specific running terminal session lights up" pattern does **not
exist**. Three real channels take outside input:

| You want… | Possible? | How |
|---|---|---|
| Message *this live session* from your phone | ✅ | Remote Control (mobile app paired) — bidirectional, but keeps **you** in the loop |
| Session react to an external event while running | ✅ (by polling/blocking) | `Monitor` / background `Bash` |
| External trigger *start* a session | ✅ | `/schedule` / `RemoteTrigger` — but spawns a **new** (usually cloud) run, not your current terminal |
| Arbitrary service push into a *specific idle* session unprompted | ❌ | no per-session endpoint exists |

Remote Control is the only channel that delivers *into* a live session, which is
why it kept "you in the loop" — not what we want for unattended automation.

---

## 3. The cost model (the key correction)

**What costs money/tokens is the *model* being invoked — not the polling.**

- A background bash `until [ -f /tmp/sig ]; do sleep 1; done` loop runs in the
  shell, burns negligible CPU, and costs **zero tokens** while spinning. The
  model wakes **exactly once**, when the file appears. A 1-second poll is cheap.
- The expensive pattern is **`ScheduleWakeup`** (loop / dynamic mode): there the
  *model itself* wakes on every tick. Each tick = one model invocation. Avoid
  for plain waiting. (The 5-min prompt-cache TTL concern applies here, not to
  file-watching.)

### Ranking, cheapest first
| Mechanism | CPU while waiting | Model cost | Notes |
|---|---|---|---|
| **FIFO blocking read** | zero (kernel-blocked) | 1 invocation on delivery | true IPC mailbox; cheapest |
| File-appearance `until` poll | ~nil (sleep loop) | 1 invocation on delivery | simplest; poll interval ~irrelevant to cost |
| `ScheduleWakeup` (loop mode) | n/a | **1 invocation per tick** | expensive — don't use for waiting |

---

## 4. Inter-session communication patterns

The **filesystem is the cheap IPC channel.**

### FIFO mailbox (cheapest, instant)
```bash
mkfifo /tmp/sess-mailbox            # once

# receiver session (run as a background Monitor):
cat /tmp/sess-mailbox               # kernel-blocks, 0% CPU, wakes instantly on write

# sender session:
echo "ingest done: 1243 papers" > /tmp/sess-mailbox
```
- `read`/`cat` on a FIFO blocks **in the kernel** — no sleep loop, no interval.
- **Caveat:** a FIFO is a *rendezvous, not a buffer*. A write blocks until a
  reader is present (and vice versa). If the sender may fire before the receiver
  is listening, use the append-file pattern instead.

### File-signal handoff (one-shot)
```bash
# session A:  touch /tmp/phase1.done
# session B:  background bash  until [ -f /tmp/phase1.done ]; do sleep 1; done
```

### Append-file + tail (buffered, survives timing)
- Sender appends lines to a file; receiver `tail -f`s it via `Monitor`.
- One model invocation per message line. Buffers naturally — no rendezvous
  requirement. Best when timing between sessions is uncertain.

### Request/reply
- Two FIFOs (one per direction), or a directory where each session drops
  `*.msg` files the other watches.

---

## 5. Platform notes

- This machine is macOS (darwin). Native event-driven watch tools differ from
  Linux: `inotifywait` is **Linux-only** (inotify-tools); macOS uses `fswatch`
  / kqueue, which may not be installed. Per policy, don't silently degrade —
  but note the **FIFO and `until`-poll patterns need no extra tools**, so they
  are the portable, no-install choices on this Mac.

---

## 6. Open next steps (pick up here)

1. **Decide the channel for the real use case** — FIFO (instant, rendezvous) vs
   append-file (buffered, survives timing). Likely append-file if sessions
   start at unpredictable times.
2. Set up a concrete **two-session mailbox** demo and confirm the receiver wakes
   the model exactly once per message.
3. Consider a small **convention**: a fixed mailbox dir (e.g. `~/.claude/ipc/`),
   message file naming, and a tiny helper script to send/await — so any session
   can join without re-deriving the pattern.
4. Decide whether to wire the **final-delivery push** (Pushover) onto the
   receiver so a completed cross-session handoff also reaches the phone.
5. Open question to resolve later: do we want a *daemon-ish* always-listening
   session, or ephemeral sessions that each arm a watch and exit? The former
   holds a model context open; the latter is cheaper but needs an external
   starter (`/schedule` / `RemoteTrigger`).

---

## 7. Ecosystem survey — named n-session chat (added 2026-07-05)

Follow-up question: n *named* sessions, chat-style, one-to-many (generalizing
the GH-issue-label ticket relay). Findings from a docs-verification agent and
a web survey agent:

### Native (Anthropic)
- **Agent Teams** (experimental, `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`):
  named teammates, file-based mailboxes under `~/.claude/teams/{team}/`,
  shared task list, `SendMessage` by name. **Topology mismatch for our case:**
  the lead *spawns* teammates; independently-started terminals cannot join a
  team. Local-only, no `/resume` of teammates.
  https://code.claude.com/docs/en/agent-teams
- **Channels** (research preview, v2.1.80+): MCP-based *push into a running
  session* — the official fix for the receive side (no blocking watch needed).
  Telegram/Discord/iMessage plugins; custom channels buildable but
  allowlist-gated during preview. The piece to watch.
  https://code.claude.com/docs/en/channels

### Third-party, best fits first
- **mco-org/squad** — closest to the ask: independent terminals `squad join
  <name>`, `squad send` / `squad receive --wait` (bounded blocking read),
  `@all` broadcast, task subsystem. Shared SQLite, **no daemon**. Supports
  Claude Code / Codex / Gemini CLI. ~570 stars, active (v0.7.6 Apr 2026).
  https://github.com/mco-org/squad
- **AMQ (avivsinai/agent-message-queue)** — Maildir-format file mailbox, no
  daemon, atomic/crash-safe, **cross-project addressing** (`codex@infra-lib`),
  delivery receipts, interops with Agent Teams. Most active development
  (v0.39.0 June 2026). https://github.com/avivsinai/agent-message-queue
- **claude-peers-mcp** — ad-hoc: localhost broker + MCP; every running Claude
  session auto-registers and is discoverable with cwd/branch summary. Young.
  https://github.com/louislva/claude-peers-mcp
- **Real chat as bus**: jeremylongshore/claude-code-slack-channel (n sessions
  as named bots in one Slack channel); official Discord/Telegram channel
  plugins could be bent to this. Cloud dependency; human-observable transcript
  is the selling point.
- **Rejected/mismatched**: parruda/swarm (hierarchical delegation, not peer
  chat); claude-squad & Tmux-Orchestrator (parallel-instance managers /
  keystroke injection); A2A protocol (enterprise HTTP service layer, no
  Claude Code integration — open FR anthropics/claude-code#28300).

### Direction (2026-07-05)
Plan: trial **squad** for mechanics. Strong draw toward **chat-as-bus** for
human observability + participation (Demitri as a peer in the channel).
Tradeoff found: **Slack has no official Channels plugin** (receive side =
Socket Mode helper or the claude-code-slack-channel bridge; bots don't see
bot messages by default → `allowBotIds`); **Discord/Telegram/iMessage have
official Channels plugins** = native push into a live session. Discord gives
the same draw with far less plumbing. Cost/loop control in any chat-as-bus:
mention-addressing discipline (react only to own @mentions + @all).
Hybrid fallback: squad transport + read/write chat mirror.

### Live two-session trial (2026-07-05) — VALIDATED
squad 0.7.6 installed at `/usr/local/bin/squad`; workspace `~/tmp/.squad`
(walk-up discovery from cwd). Two independently-started Claude sessions
(`hq` = this one, `scout` = second terminal) completed the full loop:

- **One model invocation per message confirmed** — `squad receive <id> --wait
  --json` armed as a background Bash task; unblocked ~30ms after send
  (mechanically a 500ms DB poll inside squad, ~free).
- **Raw SQLite INSERT is a first-class participant** — a `webapp` agent row +
  message inserted via `sqlite3` delivered identically to CLI sends. The
  "protocol" is just 3 tables (`agents`, `messages`, `tasks`); web
  transcript/search app = weekend project. Pin the version; add a startup
  `PRAGMA table_info` shape check that fails loud.
- **Task state machine** (queued → acked-with-15-min-lease → completed with
  `result_summary`) is the structured replacement for the GH-label relay,
  and expresses crash-recovery (requeue) that labels couldn't.
- **Gotcha: task completion is NOT push-delivered** to the creator — only
  `task create` auto-sends a message. The creator must poll `squad task
  list` or the worker must send an explicit note. Convention to adopt:
  workers send a one-line note after `task complete`.
- **Gotcha: `squad init` globally installs `/squad` slash commands** into
  `~/.claude/commands/`, `~/.gemini/`, `~/.codex/` (remove: `squad cleanup`).
- Role prompts: `squad join --role X` prints `.squad/roles/X.md` — the home
  for "each session is told its name and what the others do."
- Placement decision still open: `.squad/` at `$GH/` would give every repo
  one shared bus (walk-up finds it) and the web app a single `messages.db`.
- Viewer level-0: `datasette ~/tmp/.squad/messages.db` (read-only; never
  touch the `read` flag).

### Architectural convergence (validates §3–4)
Nobody who survived uses a blocking broker subscribe (`redis-cli subscribe`,
`nats sub`) inside the agent — it burns the turn and hits tool timeouts.
Every working design converged on: (1) file inbox + push injection,
(2) MCP push (Channels), or (3) one-shot bounded blocking read
(`squad receive --wait`). The **email/inbox model beat pub/sub**: buffered,
addressed, survives the receiver being mid-task. If ever rolling a broker:
Redis *Streams* + consumer groups (`XREAD BLOCK`), never pub/sub.
