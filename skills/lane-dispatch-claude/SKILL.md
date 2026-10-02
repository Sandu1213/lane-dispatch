---
name: lane-dispatch-claude
description: lane-dispatch with the executor type pinned to claude — Claude Code executors (interactive TUI by default; a preset with `"mode": "headless"` runs `claude -p`). Use when asked to dispatch / 派发 lanes to claude, or for /lane-dispatch-claude.
---

# lane-dispatch — claude executors

Follow the **lane-dispatch** skill exactly. Its `SKILL.md` sits in the sibling directory
`../lane-dispatch/` next to this skill's directory (read it first); `$L` is `python3 <that directory>/bin/lanectl.py`.
The only difference is that the executor type is pinned to `claude`:

- Step 3 offers only the `claude` presets listed under `types.claude` in `$L config`, with the type
  default marked recommended. Still ask the plan question and the executor question in one ask.
- Specs write `"executor": "claude"` (the type's default preset) or a `claude` preset name.
- Interactive Claude runs in the lane pane with the Claude sandbox, `acceptEdits`, the tool allowlist and `--add-dir` roots (`sandbox: safe`).
- Every new worktree triggers Claude's folder-trust dialog. The runner answers it only when the user already trusts the lane's main checkout in Claude Code; otherwise it is an `attention` event. Check `~/.claude.json` trust for each repo before dispatch and tell the user about untrusted ones.
