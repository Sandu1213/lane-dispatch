---
name: lane-dispatch-pi
description: lane-dispatch with the executor type pinned to pi — pi executors (interactive pi TUI). Use when asked to dispatch / 派发 lanes to pi, or for /lane-dispatch-pi.
---

# lane-dispatch — pi executors

Follow the **lane-dispatch** skill exactly. Its `SKILL.md` sits in the sibling directory
`../lane-dispatch/` next to this skill's directory (read it first); `$L` is `python3 <that directory>/bin/lanectl.py`.
The only difference is that the executor type is pinned to `pi`:

- Step 3 offers only the `pi` presets listed under `types.pi` in `$L config`, with the type
  default marked recommended. Still ask the plan question and the executor question in one ask.
- Specs write `"executor": "pi"` (the type's default preset) or a `pi` preset name.
- pi has no permission system and no OS sandbox: each pi lane needs `sandbox: yolo`, approved by the user for that lane in the step-3 question.
- The session id is fixed per lane (`--session-id`), so a restarted runner resumes the same session.
