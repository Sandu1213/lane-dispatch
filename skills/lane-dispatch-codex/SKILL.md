---
name: lane-dispatch-codex
description: lane-dispatch with the executor type pinned to codex — codex executors (`codex exec` headless by default; a preset with `"mode": "interactive"` runs the codex TUI). Use when asked to dispatch / 派发 lanes to codex, or for /lane-dispatch-codex.
---

# lane-dispatch — codex executors

Follow the **lane-dispatch** skill exactly. Its `SKILL.md` sits in the sibling directory
`../lane-dispatch/` next to this skill's directory (read it first); `$L` is `python3 <that directory>/bin/lanectl.py`.
The only difference is that the executor type is pinned to `codex`:

- Step 3 offers only the `codex` presets listed under `types.codex` in `$L config`, with the type
  default marked recommended. Still ask the plan question and the executor question in one ask.
- Specs write `"executor": "codex"` (the type's default preset) or a `codex` preset name.
- codex lanes are headless by default: the pane shows `codex exec` output and the attempt ends when the
  process exits. A preset with `"mode": "interactive"` runs the codex TUI instead (same sandbox; the
  attempt ends via `lanectl report` and herdr's turn state). Offer it only when the user wants to watch
  or type into the lane; an untrusted repo root makes codex ask for folder trust (an attention event).
- `sandbox: safe` = codex `workspace-write` + network + the lane's writable roots.
