---
name: lane-dispatch-grok
description: lane-dispatch with the executor type pinned to grok — grok executors (interactive Grok Build TUI). Use when asked to dispatch / 派发 lanes to grok, or for /lane-dispatch-grok.
---

# lane-dispatch — grok executors

Follow the **lane-dispatch** skill exactly. Its `SKILL.md` sits in the sibling directory
`../lane-dispatch/` next to this skill's directory (read it first); `$L` is `python3 <that directory>/bin/lanectl.py`.
The only difference is that the executor type is pinned to `grok`:

- Step 3 offers only the `grok` presets listed under `types.grok` in `$L config`, with the type
  default marked recommended. Still ask the plan question and the executor question in one ask.
- Specs write `"executor": "grok"` (the type's default preset) or a `grok` preset name.
- grok runs with `--permission-mode bypassPermissions` and its configured sandbox untouched: each grok lane needs `sandbox: yolo`, approved by the user for that lane in the step-3 question.
- grok must be logged in (`grok login`) before dispatch. An unauthenticated grok sits on its login screen and the attempt ends as `never_started`. Check `grok models` first; if it says `You are not authenticated`, tell the user instead of dispatching.
