---
description: Mark this session done (or park it) on the session board
argument-hint: "[park] [note]"
allowed-tools: Bash(__HUD__ done:*), Bash(__HUD__ park:*)
---

Update this session on the session board, then reply with one short line saying what you did.

- If `$ARGUMENTS` starts with `park`, run `__HUD__ park` and pass the rest as `-m "<note>"`.
- Otherwise run `__HUD__ done`, passing `$ARGUMENTS` as `-m "<note>"` when it is non-empty.

The session id comes from `$CLAUDE_CODE_SESSION_ID`, which the script reads itself.

Before marking done: if this session started from someone's question (a Slack message, a DM, a request from a person) and the conversation shows no reply was sent to them, say so in your one line instead of staying silent.
