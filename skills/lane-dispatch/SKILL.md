---
name: lane-dispatch
description: Orchestrate coding work as lanes in isolated git worktrees and herdr workspaces — you plan and verify, an executor agent implements (by type claude / codex / omp / pi / grok; default Claude Opus 5.5 running its real TUI in the lane pane, codex headless unless a preset opts into its TUI), a disk event wakes you (no polling loop), acceptance checks and codex local_review gate every lane. Use when asked to dispatch / 派发 / fan out tasks to lanes, run parallel or multi-repo implementation through herdr, or resume a lane-dispatch run.
---

# lane-dispatch

You are the **orchestrator**. You split work into lanes, write each lane's plan and acceptance
criteria, get the user's confirmation once, then let the executor implement. You never implement
lane work yourself. All mechanics go through one script:

    L="python3 <this skill dir>/bin/lanectl.py"

State lives on disk under `~/.agents/lane-dispatch/runs/<run>/` — `state.json` and each lane's
files. **Never trust conversation memory over `$L status <run>`.**

## Roles and configuration

| Role | Runs as | Model |
|---|---|---|
| orchestrator (you) | omp or Claude Code, inside a herdr pane | `claude-fable-5-1`; fall back to `claude-opus-5-5` |
| impl (one per lane) | the lane's executor in its herdr pane `impl` | configurable, see below |
| review | `codex exec -s read-only` using a `local_review` skill when codex has one (the prompt itself asks for the graded report and the `DECISION:` line) | codex `gpt-6-sol` (config `reviewer`; provider is always codex) |

Executors are presets grouped by **agent type**. `$L config` prints the effective set (built-in
defaults merged with the optional `~/.agents/lane-dispatch/config.json`) and a `types` summary:
each type's presets, its default preset, modes, whether `safe` has an OS sandbox, and whether the
CLI is installed.

| Type | Built-in preset (type default) | Mode | `sandbox: safe` |
|---|---|---|---|
| `claude` | `opus` — Claude Opus 5.5, xhigh | **interactive** (a preset may set `"mode": "headless"` → `claude -p`) | yes (Claude sandbox + `acceptEdits` + tool allowlist + `--add-dir`) |
| `codex` | `sol` — `gpt-6-sol`, xhigh | headless (`codex exec`; a preset may set `"mode": "interactive"` → codex TUI) | yes (`workspace-write`, both modes) |
| `omp` | `omp-opus` | interactive | **no** → yolo only |
| `pi` | `pi-gpt` — `openai-codex/gpt-5.5` | interactive | **no** → yolo only |
| `grok` | `grok-4.6` | interactive | **no** → yolo only |

The overall default is `opus`. A lane spec picks `"executor": "<preset>"` or `"executor": "<type>"`
(→ that type's default preset); omitted means the configured default. The user changes defaults
or adds presets only in `config.json` (`{"executor": "sol"}`, `{"defaults": {"claude": "sonnet"}}`,
`{"executors": {"name": {"provider": "claude|codex|omp|pi|grok", "model": "…", "effort": "…",
"mode": "interactive|headless"}}}`). The reviewer is always codex.

**Interactive** executors run their own TUI in the lane pane, so the user can watch and type into
it. Each attempt's prompt is a file (`<lane dir>/attempt-N.prompt.md`) that ends with the
`lanectl report` command the agent runs to record its result; the in-pane runner watches herdr's
agent state and writes the attempt's exit marker when the turn ends. The agent stays alive between
attempts: continuations are delivered into the same session.

**Entry skills by type:** `lane-dispatch-claude`, `lane-dispatch-codex`, `lane-dispatch-omp`,
`lane-dispatch-pi`, `lane-dispatch-grok` run this same procedure with the executor type pinned
(step 3 then offers only that type's presets).

Launching the orchestrator: omp `omp --model claude-fable-5-1`; Claude Code
`claude-herdr --model claude-fable-5-1`. Use whatever the current session runs; do not restart it.

## Host adapter

Write the action; pick the tool from this table.

| Action | omp | Claude Code |
|---|---|---|
| ask the user (plan confirmation, escalations) | `ask` | `AskUserQuestion` |
| track lanes | `todo` | `TodoWrite` |
| background wait that reports back on exit | `bash` with `async: true` | `Bash` with `run_in_background: true` |

## Invariants

1. A lane is done only after `verify` passed **and** local_review decided `PASS` for exactly the
   verified HEAD. There is no override: a non-PASS lane is fixed or escalated, never marked ready.
   Only the user can end it otherwise: `$L abandon <run> <lane> --reason "<user's words; where the
   work went>"` makes it terminal (`abandoned`), never `ready`.
2. Lanes commit only. You never merge, force-push, rebase, delete worktrees, or touch panes and
   workspaces you did not create. The one cleanup you do yourself is `$L close <run>` at the end
   (step 9): it closes only this run's finished-lane workspaces. Worktree removal and merges are
   printed for the user to run.
3. **Publishing is local by default.** Set a lane's `publish` to `pr` only when the task header
   explicitly asks to upload / 提 PR. Then `publish` pushes that branch and opens a normal
   (ready-for-review) PR to the kind's target branch.
4. Base branch comes from the lane `kind`, never from the checkout's current branch.
5. Sandbox is `safe` (codex: workspace-write + network + explicit writable roots; claude: Claude Code
   sandbox + `acceptEdits` + tool allowlist + `--add-dir` roots). A lane goes `yolo`
   only after the user approves it for that lane, typically because a build tool was blocked.
   omp / pi / grok have no OS sandbox: `lanectl` refuses them under `safe`, so choosing one of
   those types **is** a per-lane yolo approval — ask for it explicitly, never imply it.
6. Limits are enforced by `lanectl`: max 4 lanes running, 3 continuations (partial or failed
   verify) per lane, local_review up to 7 rounds (6 fix rounds, same cycle, counter never resets).
   At a limit → escalate (round 7 non-PASS = HUMAN_CONFIRMATION_REQUIRED).
7. A local_review that fails to run is reported with its exact command and error. Never replace
   it with your own opinion.

## Branch policy (kinds)

`$L config` shows the configured `kinds`. Each kind fixes the base / PR target branch, the branch
prefix and the commit type; `new` refuses an unknown kind or a `--base` that disagrees with it.
Built-in kinds (`config.json` `"kinds"` adds kinds or overrides these by name):

| kind | base → PR target | branch |
|---|---|---|
| `feature` (default) | `main` | `feature/<slug>` |
| `fix` | `main` | `fix/<slug>` |

A repository whose default branch is not `main`, or a team with `develop` / release branches, defines
its own kinds in `config.json`, e.g. `{"kinds": {"feature": {"target": "develop", "prefix": "feature/", "commit": "feat"}}}`.

A kind with `confirm` needs the user's explicit confirmation before `new … --confirm-kind`; a
`local_only` kind never gets a PR. Promotion between long-lived branches never goes through lanes.
The worktree is created at `<repo parent>/.codex-worktrees/<run>/<repo>-<lane>`. The full 40-char
base SHA is recorded.

## Procedure

### 1. Gate
`test "$HERDR_ENV" = 1 && test -n "$HERDR_PANE_ID"`. Lanes run only inside herdr: outside it `new` marks
the lane `failed` and `start` refuses. Tell the user to start the orchestrator in a herdr pane.

### 2. Plan the lanes
- Same lane when tasks touch the same files or depend on each other; separate lanes only when
  they can run concurrently without shared files. Cross-repo work = one lane per repo (or more
  when a repo splits cleanly). A run may span several repos.
- Ground every plan step in the actual code (read it). Acceptance commands come from the repo's
  real scripts/CI (`package.json`, Gradle, `pyproject.toml`, Makefile …), each with the
  expected exit code.
- Write one spec per lane to `<run dir>/specs/<lane>.json`:

```json
{
  "lane": "i18n-api", "repo": "<code-root>/my-service",
  "kind": "feature", "slug": "add-spanish-locale", "publish": "local", "sandbox": "safe",
  "objective": "Add Spanish (es) to my-service",
  "plan": "1. ...\n2. ...",
  "checklist": ["...", "..."],
  "acceptance": [{"cmd": "uv run pytest tests/ -q", "expect_exit": 0}],
  "setup": ["uv sync"],
  "writable": ["~/.cache/uv"],
  "pr_title": "feat(i18n): add Spanish locale"
}
```

`repo` is the repo's main checkout on this machine (`<code-root>` above is a placeholder; write the
real path, absolute or `~/…`). `setup` runs outside the sandbox before the executor starts (dependency installs). `writable`
lists tool caches the executor must write to during builds.

### 3. Confirm once (plan + executor)
Run `$L config` first. Show a lane table (lane / repo / kind → branch / publish / sandbox /
**executor**), then each lane's plan and acceptance. In the same single ask, put two questions:

1. **Executor for this dispatch**: offer the types from `$L config` `types` (installed ones), each
   with its default preset, and name other presets of the type if any; the configured default
   (normally `opus` — Claude Opus 5.5, interactive) marked recommended. Always ask, even if the user
   did not mention it; no answer or "default" → the default. A per-lane choice is allowed if the
   user names lanes. Picking omp / pi / grok also needs the lane's `sandbox: yolo` approval (inv. 5).
   Invoked as `lane-dispatch-<type>`: offer only that type's presets.
2. **Plan**: dispatch as planned / change plan or acceptance / merge lanes / I'll adjust.

Write the chosen preset (or type) into each spec's `executor`. Create nothing before the answers.
Interactive Claude asks for folder trust in every new worktree; the runner answers it only when
the user already trusts the lane's main checkout in Claude Code, otherwise it becomes an attention
event — tell the user up front when a repo is untrusted. Interactive codex applies trust to the
repository root: a main checkout trusted in `~/.codex/config.toml` is not asked again; an untrusted
one becomes an attention event (the runner never answers codex's trust dialog).

For an existing lane (e.g. a fix round) whose executor differs from the user's choice, run
`$L set-executor <run> <lane> <preset|type>` before `start` (refused while the lane's interactive
agent is still running in its pane — the user exits it there first). Sessions do not cross providers, so the next
attempt starts fresh: its prompt must say to read `TASK.md` and `progress.md` first, then list the
work (e.g. the must_fix items verbatim).

### 4. Create and start
```bash
$L init --label <short-task-name>          # → run id; records your pane as the doorbell target
$L new <run> --spec <run dir>/specs/<lane>.json   # worktree + herdr workspace + TASK.md + setup
$L start <run> <lane>                      # runs the lane's executor in its `impl` pane (interactive: its TUI)
```
Start at most 4. Queue the rest and start them as others leave `running`.
A `start` whose herdr launch fails leaves the lane unchanged. For an interactive lane whose agent is
still alive in the pane, `start` never types into the pane: the pane's runner confirms the attempt
and delivers the prompt into the same session.

### 5. Wait — no polling
Run `$L wait <run>` as a background job (host adapter). It blocks at zero token cost and returns
when any running lane's attempt ends (headless: the process exits; interactive: the agent's turn
ends, it exits, or it never starts), marking that lane `judging` — or when an interactive agent is
**blocked** on a dialog (an `attention` event; the lane stays `running`). End your turn. When it
returns, handle every event, then re-arm `$L wait <run>` if any lane is still `running`.

If your session dies, lanes keep running in their panes. When no waiter is alive, a lane whose
attempt ends or blocks prompts your pane: `Resume lane-dispatch run <run>`.

### 6. Handle each event

| Event | Action |
|---|---|
| `result.status == done` | `$L verify <run> <lane>` |
| `partial` | `$L start <run> <lane> --reason continue --prompt "Continue from progress.md. Remaining: …"` |
| `blocked`, answer is inside the confirmed plan | `--reason answer --prompt "<answer from the plan>"` |
| `blocked`, outside the plan | `$L escalate … --reason "<blocker>"`, ask the user quoting it; after the answer `--reason answer --user-answer "<user's words>" --prompt "…"` |
| exit ≠ 0, usage/rate limit in `log_tail` / `screen_tail` | `--reason ratelimit --after <minutes to reset> --prompt "Continue."` |
| exit ≠ 0, other | read `log_tail`; continue once if recoverable, else escalate |
| interactive `via: stopped` (turn ended without `report`) | read `screen_tail`: a question → treat as `blocked` above; otherwise `--reason continue --prompt "Finish the attempt and record it with lanectl report as the prompt file says."` |
| interactive `via: exited` | the agent quit (user `/exit`, crash): `--reason continue` starts a fresh runner that resumes the same session |
| interactive `via: never_started` / `prompt_failed` | read `screen_tail` / `error` (login screen, dead TUI); escalate to the user with it — never log in or install on their behalf |
| `via: lost` (runner died hard: SIGKILL, crash) | `wait` already stopped the orphaned executor and repaired the pane's terminal; if the event carries `orphan`, ask the user to stop that process in the lane pane first (`start` refuses while it runs). Then continue like `via: exited` |
| `attention: blocked` (lane still `running`) | a permission or trust dialog in the pane: quote `screen_tail` to the user and ask them to answer it in the lane pane; never answer permissions yourself. Re-arm `wait` |
| `lanectl` refuses (a limit or an illegal transition) | escalate to the user with the reason; never work around it |

Verify and review run long; run them as background jobs too.

### 7. Verify → review → fix
- `verify` re-runs everything itself: branch and clean tree before **and after** the acceptance
  commands, commits since base, freshness (merges `origin/<target>` if behind; conflicts go back
  to the lane), every acceptance command. It records the verified HEAD.
  Failed → `start --reason verify --prompt "<failures>"`.
  If you amend a lane's acceptance, re-run `verify` (allowed from `verified` / `reviewed`); the lane
  drops back to `verified` and needs a new review before `ready`. Never run `verify` in the same
  parallel batch as the amendment or a `start` — each reads state once.
- `review`, `ready` and `publish` each refuse unless the worktree is still on the lane branch,
  clean, and at exactly the verified/reviewed HEAD. If the target branch moved before `publish`,
  run `verify` again (allowed from `ready`), then `review`.
- `review [--context "<facts>"]` runs `codex exec -s read-only` with the `local_review` skill (if installed) over
  `<target>...HEAD`. The round is reserved atomically (phase `reviewing`); a second concurrent
  review is refused, a dead owner's reservation is reclaimed. It records the single
  `DECISION: PASS|BLOCK|UNVERIFIED` line found outside code fences in this invocation's report;
  a failed or verdict-less run does not consume the round (retry it, e.g. after
  `Selected model is at capacity`).
  Use `--context` for facts outside the lane's repo the reviewer must weigh (e.g. "the matching API
  change ships in other-repo branch X / PR #N"); the reviewer is told to verify, not trust, them.
  - `PASS` → `$L ready <run> <lane>`.
  - Otherwise → read the report; `start --reason fix --prompt "<must_fix / blocking items verbatim>"`,
    then verify and review again, up to review 7/7. A non-PASS at 7/7 → `escalate`; the user decides what happens
    next outside this lane (`UNVERIFIED` needs new evidence, `must_fix` cannot be waived).

### 8. Publish (only when `publish == "pr"`)
Before publishing, write `<lane dir>/pr.md` following the target repo's
`.github/PULL_REQUEST_TEMPLATE.md` (every section, real commands and results, related PRs or
`N/A`, the agent-authorship line).
`$L publish <run> <lane>` then binds the PR repo to the GitHub `origin`, checks open PRs, pushes
exactly the reviewed commit (no force), and creates a normal PR — or refreshes the body of the open
PR on the same target. If the target moves after publishing, re-run verify → review → publish.

### 9. Report (Chinese)
Per lane: repo, branch, worktree, phase, attempts / continuations / fix rounds, each acceptance
command's result, local_review round + decision (quote the must_fix / should_fix classification
from the report), deviations from the plan, PR link or "本地保留".

Then close the workspaces yourself: once every lane is `published`, `ready`, `failed` or `abandoned`, run
`$L close <run>` (without asking). It closes only workspaces this run created, only for finished
lanes, and only after checking the recorded pane still sits in that lane's worktree. A workspace
that is already gone is recorded as such. Report what it closed or kept. If a lane later needs more
work (e.g. PR feedback), `$L reopen <run> <lane>` gives it a fresh workspace on the same worktree.

Print, do not run (worktree removal discards uncommitted work; merging is the user's decision):

```bash
git -C <repo> worktree remove <worktree>              # after the PR is merged or the work abandoned
gh pr merge <number> --merge --delete-branch          # after human review; use the repo's merge style
```

## Resume
`$L runs` lists runs with open lanes. Then `$L status <run>`: `running` lanes → re-arm
`$L wait <run>` (it immediately picks up lanes that exited while nobody listened); `judging` /
`verify_failed` / `verified` / `reviewed` lanes → continue at step 6 or 7; `escalated` → ask the user
(continue with `--reason answer`, or `abandon` when they end the lane, e.g. its work was merged elsewhere).
