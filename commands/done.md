<!--
  Template for a `/done` Claude Code slash command.

  INSTALL:
    1. Copy this file to ~/.claude/commands/done.md
    2. Replace /ABSOLUTE/PATH/TO/claude-sessions below with the absolute path to
       claude-status.py in your checkout.

  Then, inside any Claude Code session, `/done` marks that session complete in the
  dashboard (drops it from the default view; still resumable). It reads
  $CLAUDE_CODE_SESSION_ID, so it takes no argument.
-->
---
description: Mark this Claude Code session "done" (work complete) in the claude-sessions dashboard
allowed-tools: Bash(python3:*)
---
Mark the **current** session **done** ("work complete, nothing pending") in the
claude-sessions dashboard, so it drops out of the default view (still resumable
from the JSON). Run exactly this, then report the confirmation line (or its
error) back to me:

```
python3 /ABSOLUTE/PATH/TO/claude-sessions/claude-status.py --done
```

It reads `$CLAUDE_CODE_SESSION_ID` to mark *this* session. Run it verbatim — no
arguments, no other flags.
