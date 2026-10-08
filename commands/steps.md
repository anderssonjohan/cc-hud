---
description: Track the steps left in this piece of work on the session board
argument-hint: "[what is left to do]"
allowed-tools: Bash(__HUD__ step:*)
---

Steps belong to this piece of work, not to this session: after `/clear` the next session in this tab picks them up. The board shows them as `2/5` on the session's card.

If `$ARGUMENTS` is empty, run `__HUD__ step ls` and show the output.

Otherwise turn `$ARGUMENTS` into steps, in order, one command per step. When it refers to the conversation ("the plan", "what's left"), take the steps from there.

- A PR that has to be merged: `__HUD__ step add --pr owner/repo#123`. It ticks itself when the PR merges.
- A release that has to come out: `__HUD__ step add --release owner/repo`, or `--release 'owner/repo@v2.*'` to count only matching tags. It ticks itself on the first matching release published after the steps before it are done, so put it after the PR it ships.
- Anything a person or a session has to do: `__HUD__ step add "Trigger the downstream release"`.

Don't add steps that are already done. When you finish a manual step later in this session, tick it with `__HUD__ step done <n>`.

Reply with the output of the last command.
