---
name: lane-dispatch-omp
description: lane-dispatch with the executor type pinned to omp — omp executors (interactive omp TUI). Use when asked to dispatch / 派发 lanes to omp, or for /lane-dispatch-omp.
---

# lane-dispatch — omp executors

Follow the **lane-dispatch** skill exactly. Its `SKILL.md` sits in the sibling directory
`../lane-dispatch/` next to this skill's directory (read it first); `$L` is `python3 <that directory>/bin/lanectl.py`.
The only difference is that the executor type is pinned to `omp`:

- Step 3 offers only the `omp` presets listed under `types.omp` in `$L config`, with the type
  default marked recommended. Still ask the plan question and the executor question in one ask.
- Specs write `"executor": "omp"` (the type's default preset) or a `omp` preset name.
- omp has no OS sandbox: `lanectl` refuses it under `sandbox: safe`. Each omp lane needs `sandbox: yolo`, approved by the user for that lane in the step-3 question. Ask it explicitly.
- Sessions live in `<lane dir>/omp-sessions`; a restarted runner resumes with `--continue`.
