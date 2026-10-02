#!/usr/bin/env python3
"""lane-dispatch: worktree lanes executed by coding agents inside herdr panes.

The orchestrator (omp / Claude Code) calls these subcommands; `run` executes inside a lane's
herdr pane. Executors are presets grouped by agent type (claude / codex / omp / pi / grok):
interactive presets run their real TUI in the pane and finish each attempt with `lanectl report`;
headless presets (codex by default, `claude -p` on request) run non-interactively. Review always
uses codex `local_review`. Each attempt ends in an exit marker —
`wait` blocks (zero LLM tokens) until one appears, so the orchestrator wakes once per event.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import shlex
import signal
import shutil
import subprocess
import sys
import time
import uuid
from typing import Any, Iterator

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
SELF = os.path.realpath(__file__)
SCHEMA = os.path.join(SKILL_DIR, "schema", "lane-result.schema.json")
HOME = os.path.expanduser(os.environ.get("LANE_DISPATCH_HOME", "~/.agents/lane-dispatch"))
RUNS_DIR = os.path.join(HOME, "runs")
CONFIG_PATH = os.path.join(HOME, "config.json")

# Built-in defaults; ~/.agents/lane-dispatch/config.json may override any key (executors merge per name).
DEFAULT_CONFIG: dict[str, Any] = {
    "executor": "opus",
    "executors": {
        "opus": {"provider": "claude", "model": "claude-opus-5-5", "effort": "xhigh"},
        "sol": {"provider": "codex", "model": "gpt-6-sol", "effort": "xhigh"},
        "omp-opus": {"provider": "omp", "model": "anthropic/claude-opus-5-5", "effort": "xhigh"},
        "pi-gpt": {"provider": "pi", "model": "openai-codex/gpt-5.5", "effort": "xhigh"},
        "grok-4.6": {"provider": "grok", "model": "grok-4.6", "effort": "xhigh"},
    },
    # agent type -> preset used when a spec names the type instead of a preset
    "defaults": {"claude": "opus", "codex": "sol", "omp": "omp-opus", "pi": "pi-gpt", "grok": "grok-4.6"},
    "reviewer": {"provider": "codex", "model": "gpt-6-sol", "effort": "xhigh"},
    "orchestrator_models": ["claude-fable-5-1", "claude-opus-5-5"],
    # kind -> branch rule: `target` (base / PR target), `prefix`, conventional-commit type `commit`;
    # optional `confirm` (`new` needs --confirm-kind) and `local_only` (never published as a PR).
    # config.json "kinds" adds kinds or overrides these per name.
    "kinds": {
        "feature": {"target": "main", "prefix": "feature/", "commit": "feat"},
        "fix": {"target": "main", "prefix": "fix/", "commit": "fix"},
    },
}
# agent type -> modes it supports (first = default) and whether `sandbox: safe` has an OS sandbox behind it
AGENT_TYPES: dict[str, dict[str, Any]] = {
    "claude": {"modes": ("interactive", "headless"), "sandboxed": True},
    "codex": {"modes": ("headless", "interactive"), "sandboxed": True},
    "omp": {"modes": ("interactive",), "sandboxed": False},
    "pi": {"modes": ("interactive",), "sandboxed": False},
    # ponytail: grok's `--sandbox workspace` cannot add the lane dir / git common dir; a sandbox.toml
    # profile with those roots would make `safe` possible if grok lanes become common
    "grok": {"modes": ("interactive",), "sandboxed": False},
}
# Appended to every non-codex executor's system prompt (C: repo hooks only see edit-tool writes)
LANE_RULES = (
    "You are an unattended lane executor. Read files with your file-read tool and change them with your "
    "edit/write tools; never rewrite source files through python, sed, perl or shell redirection, because the "
    "repository's hooks (formatters, lint and i18n guards) only run on the edit tools. Follow the repository's "
    "own agent rules (CLAUDE.md, AGENTS.md, .claude/rules) for code search, e.g. its code-graph tool when it "
    "requires one. Never wait for a human in the terminal: put open questions in your result's blockers."
)
INTERACTIVE_TICK = 2  # seconds between the interactive runner's herdr state checks
START_GRACE = 180  # an agent that never starts working within this (login screen, dead TUI) ends the attempt
# Claude Code: tools the executor may use without prompts (Bash is confined by the sandbox)
CLAUDE_TOOLS = "Bash,Edit,MultiEdit,Write,Read,Glob,Grep,TodoWrite"
CLAUDE_SANDBOX = {"sandbox": {"enabled": True, "autoAllowBashIfSandboxed": True, "allowUnsandboxedCommands": False}}
MAX_RUNNING = 4
MAX_CONTINUE = 3  # partial / verify-failure continuations per lane
MAX_REVIEW_ROUNDS = 7  # matches local_review's 1/7..7/7 cycle; fix rounds = MAX_REVIEW_ROUNDS - 1
POLL_SECONDS = 5
START_CONFIRM_SECONDS = 15  # `start` waits this long for the runner's attempt-N.started marker

KIND_FIELDS = {"target": str, "prefix": str, "commit": str, "confirm": bool, "local_only": bool}
LANE_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]*[a-z0-9]$")
TERMINAL = {"ready", "published", "failed", "abandoned"}
# phase -> reasons `start` accepts from it (limits below still apply on top)
ALLOWED = {
    "planned": {"initial"},
    "judging": {"continue", "answer", "ratelimit"},
    "verify_failed": {"verify", "ratelimit"},
    "reviewed": {"fix"},
    "escalated": {"answer"},
}
# reason -> counter it consumes (None = free, but still bounded by MAX_ATTEMPTS)
REASONS = {
    "initial": None,
    "continue": "continues",
    "verify": "continues",
    "fix": "fixes",
    "answer": None,
    "ratelimit": None,
}
MAX_ATTEMPTS = 1 + MAX_CONTINUE + (MAX_REVIEW_ROUNDS - 1) + 4  # hard ceiling per lane across every reason
DECISION_LINE_RE = re.compile(r"^DECISION:\s*`?(PASS|BLOCK|UNVERIFIED)`?\s*$")


# ---------------------------------------------------------------- pure helpers (unit-tested)


def kind_rule(config: dict[str, Any], kind: str) -> dict[str, Any]:
    if kind not in config["kinds"]:
        raise ValueError(f"unknown kind {kind!r}; configured kinds: {sorted(config['kinds'])}")
    return config["kinds"][kind]


def expected_base(rule: dict[str, Any], has_origin: bool) -> tuple[str, str]:
    """Return (base ref, PR target branch) for a lane kind; never derived from the current branch."""
    target = rule["target"]
    return (f"origin/{target}" if has_origin else target), target


def check_base_override(kind: str, rule: dict[str, Any], has_origin: bool, override: str | None) -> None:
    if override is None:
        return
    ref, target = expected_base(rule, has_origin)
    if override not in (ref, target):
        raise ValueError(
            f"--base {override!r} conflicts with kind {kind!r} (expects {ref}); "
            "change the kind instead of overriding the base"
        )


def branch_name(rule: dict[str, Any], slug: str, run_id: str, taken: bool) -> str:
    if not SLUG_RE.match(slug):
        raise ValueError(f"slug {slug!r} must be kebab-case, letter-leading")
    name = rule["prefix"] + slug
    return f"{name}-{run_id[-3:]}" if taken else name


def toml_array(items: list[str]) -> str:
    return json.dumps(items)  # a JSON string array is a valid TOML array of basic strings


EFFORTS = {"low", "medium", "high", "xhigh", "max"}


def merge_config(user: dict[str, Any]) -> dict[str, Any]:
    config = {**DEFAULT_CONFIG, **{k: v for k, v in user.items() if k not in ("executors", "defaults", "kinds")}}
    if not isinstance(user.get("kinds", {}), dict):
        raise ValueError("kinds must be an object of kind name -> rule")
    config["executors"] = {**DEFAULT_CONFIG["executors"], **user.get("executors", {})}
    config["defaults"] = {**DEFAULT_CONFIG["defaults"], **user.get("defaults", {})}
    config["kinds"] = {**DEFAULT_CONFIG["kinds"], **user.get("kinds", {})}
    for kind, rule in config["kinds"].items():
        if not (isinstance(rule, dict) and all(isinstance(rule.get(f), str) and rule[f] for f in ("target", "prefix", "commit"))):
            raise ValueError(f"kinds.{kind} needs non-empty strings target, prefix and commit")
        bad = [f for f, v in rule.items() if f not in KIND_FIELDS or not isinstance(v, KIND_FIELDS[f])]
        if bad:
            raise ValueError(f"kinds.{kind}: unknown or mistyped fields {bad}; allowed {sorted(KIND_FIELDS)}")
        for ref in (rule["target"], rule["prefix"] + "slug"):  # what `new` fetches and creates
            if subprocess.run(["git", "check-ref-format", f"refs/heads/{ref}"], capture_output=True).returncode:
                raise ValueError(f"kinds.{kind}: {ref!r} is not a valid branch name")
    for name, ex in list(config["executors"].items()) + [("reviewer", config["reviewer"])]:
        if ex.get("provider") not in AGENT_TYPES or not ex.get("model"):
            raise ValueError(f"{name!r} needs provider in {sorted(AGENT_TYPES)} and a model")
        ex = {"effort": "xhigh", **ex}  # a preset may omit effort; it defaults, never crashes a launch
        if not isinstance(ex["effort"], str) or ex["effort"] not in EFFORTS:
            raise ValueError(f"{name!r} effort must be one of {sorted(EFFORTS)}")
        if name == "reviewer":
            config["reviewer"] = ex
            continue
        modes = AGENT_TYPES[ex["provider"]]["modes"]
        ex = {"mode": modes[0], **ex}
        if ex["mode"] not in modes:
            raise ValueError(f"{name!r}: {ex['provider']} supports mode {'/'.join(modes)}, not {ex['mode']!r}")
        config["executors"][name] = ex
    if config["reviewer"]["provider"] != "codex":
        raise ValueError("reviewer must be codex (local_review is a codex skill)")
    for kind, preset in config["defaults"].items():
        if config["executors"].get(preset, {}).get("provider") != kind:
            raise ValueError(f"defaults.{kind} must name a {kind} preset, not {preset!r}")
    if config["executor"] not in config["executors"]:
        raise ValueError(f"default executor {config['executor']!r} is not defined")
    return config


def resolve_executor(choice: Any, config: dict[str, Any]) -> dict[str, Any]:
    """Spec `executor`: omitted → config default; a preset name; or an agent type name (→ that type's
    default preset, e.g. `codex` → `sol`). Never an ad-hoc object."""
    name = choice or config["executor"]
    if name not in config["executors"] and name in AGENT_TYPES:
        name = config["defaults"].get(name) or next(
            (p for p, ex in config["executors"].items() if ex["provider"] == name), name)
    if name not in config["executors"]:
        raise ValueError(f"unknown executor {name!r}; presets: {sorted(config['executors'])}, "
                         f"types: {sorted(AGENT_TYPES)}")
    return {"name": name, **config["executors"][name]}


def executor_mode(ex: dict[str, Any]) -> str:
    """Lanes created before agent types existed carry no mode: they were all headless."""
    return ex.get("mode", "headless")


def sandbox_violation(ex: dict[str, Any], sandbox: str) -> str | None:
    if sandbox == "safe" and not AGENT_TYPES[ex["provider"]]["sandboxed"]:
        return (f"{ex['provider']} has no OS sandbox, so `safe` cannot be enforced; a lane using it needs "
                "`sandbox: yolo` (only with the user's approval for that lane)")
    return None


def build_exec_cmd(lane: dict[str, Any], attempt: int, prompt: str, schema: str) -> list[str]:
    """First attempt starts a session; later attempts resume the same one (same thread, same sandbox)."""
    ex = lane["executor"]
    resume = attempt > 1 and bool(lane.get("session_id"))
    if ex["provider"] == "claude":
        cmd = ["claude", "-p", prompt, "--model", ex["model"], "--effort", ex["effort"],
               "--output-format", "stream-json", "--verbose",
               "--json-schema", open(schema, encoding="utf-8").read()]
        if resume:
            cmd += ["--resume", lane["session_id"]]
        return cmd + claude_access(lane)
    cmd = ["codex", "exec"]
    if resume:
        cmd += ["resume", lane["session_id"]]
    cmd += [
        "-m", ex["model"],
        "-c", f'model_reasoning_effort="{ex["effort"]}"',
        "--output-schema", schema,
        "-o", os.path.join(lane["lane_dir"], f"result-{attempt}.json"),
        *codex_access(lane),
    ]
    if not resume:
        cmd += ["-C", lane["worktree"]]
    cmd.append(prompt)
    return cmd


def codex_access(lane: dict[str, Any]) -> list[str]:
    """Sandbox + approvals, shared by `codex exec` and the codex TUI (both take these as `-c`)."""
    if lane["sandbox"] == "yolo":
        return ["--dangerously-bypass-approvals-and-sandbox"]
    roots = [lane["lane_dir"], lane["git_common_dir"], *lane.get("writable", [])]
    return [
        "-c", 'sandbox_mode="workspace-write"',
        "-c", "sandbox_workspace_write.network_access=true",
        "-c", f"sandbox_workspace_write.writable_roots={toml_array(roots)}",
        "-c", 'approval_policy="never"',
    ]


def claude_access(lane: dict[str, Any]) -> list[str]:
    """Permissions + lane rules, shared by headless and interactive Claude (`--add-dir` is variadic: last)."""
    if lane["sandbox"] == "yolo":
        return ["--append-system-prompt", LANE_RULES, "--dangerously-skip-permissions"]
    return ["--append-system-prompt", LANE_RULES, "--settings", json.dumps(CLAUDE_SANDBOX),
            "--permission-mode", "acceptEdits", "--allowedTools", CLAUDE_TOOLS,
            "--add-dir", lane["lane_dir"], lane["git_common_dir"], *lane.get("writable", [])]


def new_session(lane: dict[str, Any]) -> str | None:
    """Claude/pi/grok take a fresh session id; omp keeps one session directory per lane and resumes with
    --continue; codex assigns its own thread id, which the runner learns from herdr (None until then)."""
    if lane["executor"]["provider"] == "omp":
        return os.path.join(lane["lane_dir"], "omp-sessions")
    if lane["executor"]["provider"] == "codex":
        return None
    return str(uuid.uuid4())


def build_interactive_cmd(lane: dict[str, Any], prompt: str, session: str, resume: bool) -> list[str]:
    """The agent's own TUI in the lane pane, started (or resumed) with the attempt prompt."""
    ex = lane["executor"]
    if ex["provider"] == "claude":
        return ["claude", prompt, "--model", ex["model"], "--effort", ex["effort"],
                *(["--resume", session] if resume else ["--session-id", session]), *claude_access(lane)]
    if ex["provider"] == "omp":  # no OS sandbox: `new` only admits yolo lanes for omp
        cmd = ["omp", "--model", ex["model"], "--thinking", ex["effort"], "--session-dir", session,
               "--append-system-prompt", LANE_RULES, "--approval-mode", "yolo", *(["--continue"] if resume else [])]
        for path in (lane["lane_dir"], lane["git_common_dir"], *lane.get("writable", [])):
            cmd += ["--add-dir", path]
        return cmd + [prompt]
    if ex["provider"] == "pi":  # no permission system at all: yolo lanes only; --session-id creates or resumes
        return ["pi", "--model", ex["model"], "--thinking", ex["effort"], "--session-id", session,
                "--append-system-prompt", LANE_RULES, prompt]
    if ex["provider"] == "grok":  # approvals bypassed, sandbox left as configured: yolo lanes only
        return ["grok", prompt, "-m", ex["model"], "--effort", ex["effort"],
                *(["--resume", session] if resume else ["--session-id", session]),
                "--rules", LANE_RULES, "--permission-mode", "bypassPermissions"]
    if ex["provider"] == "codex":  # LANE_RULES reach codex through TASK.md's Boundaries
        return ["codex", *(["resume", session] if resume else []), "-m", ex["model"],
                "-c", f'model_reasoning_effort="{ex["effort"]}"', *codex_access(lane), prompt]
    raise ValueError(f"{ex['provider']} has no interactive driver")


def claude_display(event: dict[str, Any]) -> str | None:
    """One human-readable pane line per stream-json event worth seeing."""
    if event.get("type") == "assistant":
        parts = []
        for block in event.get("message", {}).get("content", []):
            if block.get("type") == "text" and block.get("text", "").strip():
                parts.append(block["text"].strip())
            elif block.get("type") == "tool_use":
                arg = block.get("input", {})
                hint = arg.get("command") or arg.get("file_path") or arg.get("pattern") or ""
                parts.append(f"→ {block.get('name')} {str(hint)[:160]}")
        return "\n".join(parts) or None
    if event.get("type") == "result":
        return f"[result] {'error' if event.get('is_error') else 'ok'} session={event.get('session_id')}"
    return None


def start_violation(lanes: dict[str, dict[str, Any]], name: str, reason: str,
                    user_answer: str | None = None) -> str | None:
    """Why `start` must refuse, or None. Limits bind the phase transition, not the caller's reason."""
    lane = lanes[name]
    if reason not in REASONS:
        return f"unknown reason {reason!r}"
    allowed = ALLOWED.get(lane["phase"], set())
    if reason not in allowed:
        return f"lane {name} is {lane['phase']}; start accepts {sorted(allowed) or 'nothing'} from there"
    if reason == "fix" and lane.get("review", {}).get("decision") == "PASS":
        return "reason 'fix' needs a review decision other than PASS"
    if reason == "fix" and lane.get("review", {}).get("round", 0) >= MAX_REVIEW_ROUNDS:
        return f"review {MAX_REVIEW_ROUNDS}/{MAX_REVIEW_ROUNDS} was not PASS: HUMAN_CONFIRMATION_REQUIRED, escalate"
    if lane["phase"] == "escalated" and not user_answer:
        return "an escalated lane restarts only with --user-answer quoting the user's decision"
    if lane["attempt"] >= MAX_ATTEMPTS:
        return f"attempt limit {MAX_ATTEMPTS} reached for lane {name}"
    counter = REASONS[reason]
    limit = {"continues": MAX_CONTINUE, "fixes": MAX_REVIEW_ROUNDS - 1}.get(counter or "")
    if counter and lane[counter] >= limit:
        return f"{counter} limit {limit} reached for lane {name}; escalate to the user"
    running = sum(1 for n, l in lanes.items() if n != name and (l["phase"] == "running" or launch_active(l)))
    if running >= MAX_RUNNING:
        return f"concurrency limit {MAX_RUNNING} reached (running or launching); start after another lane exits"
    return None


def launch_fresh(pending: dict[str, Any] | None, now_epoch: float | None = None) -> bool:
    """A launch reservation still inside its confirmation window (plus slack)."""
    if not pending:
        return False
    return (now_epoch or time.time()) - pending.get("at_epoch", 0) < START_CONFIRM_SECONDS + 5


def launch_active(lane: dict[str, Any]) -> bool:
    """A reservation is live while fresh, and forever once its runner confirmed with the same token
    (a confirmed orphan is a real running executor until `start` adopts it)."""
    pending = lane.get("launching")
    if not pending:
        return False
    if launch_fresh(pending):
        return True
    marker = os.path.join(lane.get("lane_dir", ""), f"attempt-{pending['attempt']}.started")
    return marker_matches(marker, pending["launch"])


def commit_launch(lane: dict[str, Any], attempt: int, reason: str, user_answer: str | None) -> None:
    counter = REASONS[reason]
    if counter:
        lane[counter] += 1
    lane["attempt"], lane["phase"] = attempt, "running"
    lane["history"].append({"attempt": attempt, "reason": reason, "at": now(),
                            **({"user_answer": user_answer} if user_answer else {})})


def parse_decision(text: str) -> str | None:
    """The verdict is a line that is exactly `DECISION: X` outside fenced code blocks (examples inside
    ``` fences never count; trailing citations after it are fine). Conflicting or missing → None."""
    found: set[str] = set()
    fence: tuple[str, int] | None = None  # (char, length) of the open fence, CommonMark-style
    for raw in text.splitlines():
        line = raw.strip()
        indent = len(raw) - len(raw.lstrip(" "))
        run = re.match(r"^(`{3,}|~{3,})", line) if indent <= 3 else None  # 4+ spaces = content, not a fence
        if run:
            char, size = run.group(1)[0], len(run.group(1))
            if fence is None:
                fence = (char, size)
                continue
            if char == fence[0] and size >= fence[1] and line == run.group(1):
                fence = None
                continue
        if fence is None and (m := DECISION_LINE_RE.match(line)):
            found.add(m.group(1))
    return found.pop() if len(found) == 1 else None


def pick_pr(open_prs: list[dict[str, Any]], base: str, repo_slug: str) -> dict[str, Any] | None:
    """Reuse an open PR only when its head lives in this very repo and it targets the lane's base.
    `gh pr list --head` matches the branch name alone, so fork PRs with the same name are skipped;
    closed PRs never count; more than one own match is ambiguous and refused."""
    own = []
    for pr in open_prs:
        head_repo = f"{(pr.get('headRepositoryOwner') or {}).get('login')}/{(pr.get('headRepository') or {}).get('name')}"
        if pr.get("isCrossRepository") is not False or head_repo.lower() != repo_slug.lower():
            continue
        if pr.get("baseRefName") != base:
            raise ValueError(f"open PR {pr.get('url')} targets {pr.get('baseRefName')}, expected {base}")
        own.append(pr)
    if len(own) > 1:
        raise ValueError(f"{len(own)} open PRs from this branch; resolve the ambiguity by hand")
    return own[0] if own else None


GITHUB_REMOTE_RE = re.compile(r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/\s]+/[^/\s]+?)(?:\.git)?/?$")

def github_slug(remote_url: str) -> str:
    match = GITHUB_REMOTE_RE.match(remote_url)
    if not match:
        raise ValueError(f"origin {remote_url!r} is not a GitHub remote; cannot bind the PR repo to it")
    return match.group(1)


# ---------------------------------------------------------------- io helpers


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def die(msg: str, code: int = 2) -> None:
    print(f"lanectl: {msg}", file=sys.stderr)
    sys.exit(code)


def emit(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def sh(args: list[str], cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if check and proc.returncode != 0:
        die(f"{shlex.join(args)} failed ({proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}")
    return proc


def shell(cmd: str, cwd: str) -> subprocess.CompletedProcess[str]:
    # non-login shell: inherit the orchestrator's PATH (a login bash on macOS resolves /usr/bin/python3 3.9)
    return subprocess.run(["bash", "-c", cmd], cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL)


def tail(text: str, lines: int = 30) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def run_dir(run: str) -> str:
    path = os.path.join(RUNS_DIR, run)
    if not os.path.isdir(path):
        die(f"run {run} not found under {RUNS_DIR}")
    return path


def read_json(path: str) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: str, obj: Any) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


@contextlib.contextmanager
def locked_state(run: str) -> Iterator[dict[str, Any]]:
    """Read-modify-write state.json under an exclusive lock (runner and orchestrator never race)."""
    base = run_dir(run)
    with open(os.path.join(base, "state.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read_json(os.path.join(base, "state.json"))
        yield state
        write_json(os.path.join(base, "state.json"), state)


def load_state(run: str) -> dict[str, Any]:
    return read_json(os.path.join(run_dir(run), "state.json"))


def load_config() -> dict[str, Any]:
    try:
        return merge_config(read_json(CONFIG_PATH) if os.path.exists(CONFIG_PATH) else {})
    except (ValueError, json.JSONDecodeError) as exc:
        die(f"invalid {CONFIG_PATH}: {exc}")
        raise  # unreachable; keeps type checkers honest


def get_lane(state: dict[str, Any], name: str) -> dict[str, Any]:
    if name not in state["lanes"]:
        die(f"lane {name} not in run {state['run_id']}")
    return state["lanes"][name]


def herdr_json(args: list[str]) -> dict[str, Any] | None:
    proc = subprocess.run(["herdr", *args], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def tree_state(wt: str) -> dict[str, Any]:
    branch = sh(["git", "-C", wt, "symbolic-ref", "--short", "-q", "HEAD"], check=False).stdout.strip()
    return {"head": sh(["git", "-C", wt, "rev-parse", "HEAD"]).stdout.strip(), "branch": branch,
            "dirty": sh(["git", "-C", wt, "status", "--porcelain"]).stdout.strip().splitlines()}


def require_bound(lane: dict[str, Any], head: str | None, gate: str) -> dict[str, Any]:
    """The worktree must still be exactly what the previous gate checked: same branch, same HEAD, clean."""
    t = tree_state(lane["worktree"])
    if t["branch"] != lane["branch"]:
        die(f"{gate}: worktree is on {t['branch'] or 'detached HEAD'}, expected {lane['branch']}")
    if t["dirty"]:
        die(f"{gate}: worktree has uncommitted changes; run verify again")
    if head is None or t["head"] != head:
        die(f"{gate}: HEAD is {t['head'][:12]}, last checked {str(head)[:12]}; run verify again")
    return t


def pid_alive(path: str) -> bool:
    try:
        with open(path) as fh:
            os.kill(int(fh.read().strip()), 0)
        return True
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------- brief


BRIEF = """# Lane {lane} — {objective}

Run `{run}` · repo `{repo}` · branch `{branch}` (from `{base_ref}` @ `{base_sha}`)

## Objective
{objective}

## Plan (confirmed by the user — execute in order)
{plan}

When reality contradicts a step, record the deviation and its reason prominently in
`{lane_dir}/progress.md` instead of silently re-designing.

## Checklist
{checklist}

## Acceptance criteria (these, not the checklist ticks, define done)
{acceptance}

## Boundaries
- Work only inside `{worktree}`. Never touch the main checkout `{repo}` or any other worktree.
- Commit only. Never push, merge, rebase, amend, open a pull request, or change branches.
- Never connect to shared databases, staging, or production. Local/isolated resources only.
- Do not edit generated files by hand; run their generators.
- Change files with your edit/write tools, not by rewriting them through python/sed/shell: the
  repository's hooks (formatters, lint and i18n guards) only run on the edit tools.

## Commit policy
Commit continuously, one coherent unit per commit, Conventional Commits
(`<type>(<scope>): <description>`). Stage only the paths of that unit (`git add <paths>`, never
`git add -A`). Never amend or rewrite a commit you already made; correct it with a follow-up.

## Progress protocol
After every checklist item, rewrite `{lane_dir}/progress.md` with the checklist and tick state,
what you just did, what comes next, and any decision a fresh reader would need. Your context may
be compacted at any time; that file is your memory.

## Result
End every attempt with this JSON result (headless: your final structured output; interactive: the
attempt's prompt gives the `lanectl report` command that records it):
- `status`: `done` only when every checklist item is done, every acceptance command passes, and
  `git status` is clean; `partial` when work remains; `blocked` when you cannot proceed without a
  decision or resource outside this plan (put the exact question in `blockers`).
- `checks`: every acceptance command you ran with its exit code.
- `commits`: `<short sha> <subject>` for each commit you made in this attempt.
"""


def render_brief(spec: dict[str, Any], lane: dict[str, Any], run: str) -> str:
    checklist = "\n".join(f"- [ ] {item}" for item in spec["checklist"])
    acceptance = "\n".join(
        f"- `{a['cmd']}` → exit {a.get('expect_exit', 0)}" + (f" — {a['note']}" if a.get("note") else "")
        for a in spec["acceptance"]
    )
    return BRIEF.format(
        lane=lane["name"], objective=spec["objective"], run=run, repo=lane["repo"],
        branch=lane["branch"], base_ref=lane["base_ref"], base_sha=lane["base_sha"],
        plan=spec["plan"].strip(), checklist=checklist, acceptance=acceptance,
        worktree=lane["worktree"], lane_dir=lane["lane_dir"],
    )


# ---------------------------------------------------------------- commands


def cmd_init(args: argparse.Namespace) -> None:
    label = re.sub(r"[^a-z0-9-]+", "-", (args.label or "run").lower()).strip("-")[:24] or "run"
    run = f"{dt.datetime.now():%Y%m%d-%H%M%S}-{label}"
    base = os.path.join(RUNS_DIR, run)
    os.makedirs(os.path.join(base, "lanes"), exist_ok=False)
    pane = os.environ.get("HERDR_PANE_ID")
    agent = None
    if pane:
        info = herdr_json(["agent", "get", pane])
        agent = (info or {}).get("result", {}).get("agent", {}).get("agent")
    write_json(os.path.join(base, "state.json"), {
        "run_id": run, "created_at": now(), "orchestrator": {"pane": pane, "agent": agent}, "lanes": {},
    })
    emit({"run": run, "dir": base, "orchestrator_pane": pane, "orchestrator_agent": agent})


def cmd_new(args: argparse.Namespace) -> None:
    spec = read_json(args.spec)
    for key in ("lane", "repo", "slug", "objective", "plan", "checklist", "acceptance"):
        if not spec.get(key):
            die(f"spec missing {key!r}")
    name, kind = spec["lane"], spec.get("kind", "feature")
    publish, sandbox = spec.get("publish", "local"), spec.get("sandbox", "safe")
    if not LANE_RE.match(name):
        die(f"lane name {name!r} must match {LANE_RE.pattern}")
    if publish not in ("local", "pr") or sandbox not in ("safe", "yolo"):
        die("publish must be local|pr and sandbox must be safe|yolo")
    config = load_config()
    try:
        rule = kind_rule(config, kind)
        executor = resolve_executor(spec.get("executor"), config)
    except ValueError as exc:
        die(str(exc))
    if rule.get("confirm") and not args.confirm_kind:
        die(f"kind {kind} needs --confirm-kind (explicit user confirmation)")
    if rule.get("local_only") and publish == "pr":
        die(f"kind {kind} is local-only: publish must be local")
    if (problem := sandbox_violation(executor, sandbox)):
        die(problem)
    repo = os.path.realpath(os.path.expanduser(spec["repo"]))
    if sh(["git", "-C", repo, "rev-parse", "--git-dir"]).stdout.strip() != ".git":
        die(f"{repo} must be a main checkout, not a linked worktree")
    state = load_state(args.run)
    if name in state["lanes"]:
        die(f"lane {name} already exists in run {args.run}")

    has_origin = sh(["git", "-C", repo, "remote", "get-url", "origin"], check=False).returncode == 0
    base_ref, pr_base = expected_base(rule, has_origin)
    try:
        check_base_override(kind, rule, has_origin, spec.get("base"))
    except ValueError as exc:
        die(str(exc))
    if has_origin:
        sh(["git", "-C", repo, "fetch", "origin", pr_base])
    base_sha = sh(["git", "-C", repo, "rev-parse", "--verify", f"{base_ref}^{{commit}}"]).stdout.strip()
    wanted = rule["prefix"] + spec["slug"]
    taken = sh(["git", "-C", repo, "show-ref", "--verify", "--quiet", f"refs/heads/{wanted}"], check=False).returncode == 0
    if has_origin and not taken:
        taken = bool(sh(["git", "-C", repo, "ls-remote", "--heads", "origin", wanted]).stdout.strip())
    try:
        branch = branch_name(rule, spec["slug"], args.run, taken)
    except ValueError as exc:
        die(str(exc))

    worktree = os.path.join(os.path.dirname(repo), ".codex-worktrees", args.run, f"{os.path.basename(repo)}-{name}")
    sh(["git", "-C", repo, "worktree", "add", "-b", branch, worktree, base_sha])
    git_common = sh(["git", "-C", worktree, "rev-parse", "--path-format=absolute", "--git-common-dir"]).stdout.strip()
    lane_dir = os.path.join(run_dir(args.run), "lanes", name)
    os.makedirs(lane_dir, exist_ok=True)

    lane: dict[str, Any] = {
        "name": name, "repo": repo, "kind": kind, "kind_rule": rule,
        "branch": branch, "base_ref": base_ref,
        "base_sha": base_sha, "pr_base": pr_base, "has_origin": has_origin, "worktree": worktree,
        "git_common_dir": git_common, "lane_dir": lane_dir, "publish": publish, "sandbox": sandbox,
        "writable": [os.path.expanduser(p) for p in spec.get("writable", [])],
        "executor": executor, "reviewer": config["reviewer"],
        "objective": spec["objective"], "pr_title": spec.get("pr_title"),
        "acceptance": spec["acceptance"], "phase": "planned", "attempt": 0, "continues": 0,
        "fixes": 0, "session_id": None, "created_at": now(), "history": [],
    }
    with open(os.path.join(lane_dir, "TASK.md"), "w", encoding="utf-8") as fh:
        fh.write(render_brief(spec, lane, args.run))
    open(os.path.join(lane_dir, "progress.md"), "a").close()

    setup = []
    for cmd in spec.get("setup", []):
        proc = shell(cmd, worktree)
        setup.append({"cmd": cmd, "exit": proc.returncode})
        if proc.returncode != 0:
            lane.update(phase="failed", failure=f"setup `{cmd}` exit {proc.returncode}: {tail(proc.stderr or proc.stdout)}")
            break
    lane["setup"] = setup
    if not open_workspace(lane):  # lanes only run inside herdr: without a pane there is nothing to launch into
        lane.update(pane_id=None, phase="failed", failure="herdr workspace create failed (run lanectl inside herdr)")

    with locked_state(args.run) as st:
        st["lanes"][name] = lane
    emit({k: lane.get(k) for k in ("name", "phase", "branch", "base_ref", "base_sha", "worktree",
                                    "lane_dir", "workspace_id", "pane_id", "setup", "failure")})


def cmd_start(args: argparse.Namespace) -> None:
    """Two-phase launch. Phase 1 (locked): validate, write attempt metadata with a unique launch token,
    mark the lane `launching`. Unlocked: clear the pane prompt, run the runner, wait for its marker.
    Phase 2 (locked): commit `running` only for a marker carrying this token; otherwise withdraw the
    metadata. The runner confirms under the same lock and only if the metadata still has its token,
    so exactly one of "runner confirmed" or "start withdrew" can happen."""
    launch = f"{os.getpid()}-{time.time_ns()}"
    with locked_state(args.run) as state:
        lane = get_lane(state, args.lane)
        if not lane.get("pane_id"):
            die(f"lane {args.lane} has no herdr pane (closed?); run `lanectl reopen {args.run} {args.lane}` inside herdr")
        pending = lane.get("launching")
        if launch_fresh(pending):
            die(f"attempt {pending['attempt']} is being launched right now")
        if pending:  # abandoned by a start that died between its two phases
            old_meta = os.path.join(lane["lane_dir"], f"attempt-{pending['attempt']}.json")
            old_marker = os.path.join(lane["lane_dir"], f"attempt-{pending['attempt']}.started")
            lane.pop("launching")
            if marker_matches(old_marker, pending["launch"]):  # its runner did come up: adopt, don't double-launch
                meta = read_json(old_meta) if os.path.exists(old_meta) else {}
                commit_launch(lane, pending["attempt"], meta.get("reason", "answer"), meta.get("user_answer"))
                emit({"lane": args.lane, "adopted_attempt": pending["attempt"], "phase": "running"})
                return
            if os.path.exists(old_meta) and read_json(old_meta).get("launch") == pending["launch"]:
                os.replace(old_meta, f"{old_meta}.withdrawn-{time.time_ns()}")
        problem = start_violation(state["lanes"], args.lane, args.reason, args.user_answer)
        if problem:
            die(problem)
        if args.reason != "initial" and not (args.prompt or args.prompt_file):
            die("continuations need --prompt or --prompt-file")
        attempt = lane["attempt"] + 1
        if args.reason == "initial":
            prompt = (
                f"Execute the task brief at {lane['lane_dir']}/TASK.md. Follow its plan in order, keep "
                f"{lane['lane_dir']}/progress.md updated after every checklist item, commit as you go, and "
                "end with the JSON result the brief describes."
            )
        else:
            prompt = args.prompt or open(args.prompt_file, encoding="utf-8").read()
        for suffix in ("started", "exit", "log"):  # leftovers of an earlier failed launch of this number
            old = os.path.join(lane["lane_dir"], f"attempt-{attempt}.{suffix}")
            if os.path.exists(old):
                os.replace(old, f"{old}.stale-{time.time_ns()}")
        meta_path = os.path.join(lane["lane_dir"], f"attempt-{attempt}.json")
        write_json(meta_path, {"reason": args.reason, "after_minutes": args.after, "prompt": prompt,
                               "user_answer": args.user_answer, "requested_at": now(), "launch": launch})
        lane["launching"] = {"attempt": attempt, "launch": launch, "at_epoch": time.time()}
        pane = lane["pane_id"]
    runner = interactive_runner(lane)  # a live agent TUI owns the pane: its runner picks the attempt up
    # the pane's shell does not inherit our environment: carry a non-default state home explicitly
    home = [f"LANE_DISPATCH_HOME={os.environ['LANE_DISPATCH_HOME']}"] if os.environ.get("LANE_DISPATCH_HOME") else []
    command = shlex.join(["env", *home, "python3", SELF, "run", args.run, args.lane, str(attempt), "--launch", launch])
    started = os.path.join(lane["lane_dir"], f"attempt-{attempt}.started")
    why, sent = None, bool(runner)
    found = None if runner else herdr_lookup(["agent", "get", pane], "agent_not_found")
    if found not in (None, "gone"):  # only herdr's explicit agent_not_found proves the pane is a bare shell
        why = ("the lane pane still runs an agent whose runner is gone; exit that agent in the pane, then start again"
               if isinstance(found, dict) else "cannot tell whether the lane pane runs an agent (herdr query failed)")
    elif not runner and (orphan := stop_orphan(lane["lane_dir"], kill=False)):
        why = f"{orphan}, then start again"  # herdr loses a TUI whose runner was SIGKILLed: never type into it
    elif not runner:  # never type a shell command into an agent's TUI
        # a dirty prompt line (stray keys) would corrupt the command: clear it first
        sh(["herdr", "pane", "send-keys", pane, "ctrl+u"], check=False)
        launched = sh(["herdr", "pane", "run", pane, command], check=False)
        sent = True
        if launched.returncode != 0:
            why = launched.stderr.strip() or launched.stdout.strip() or f"herdr pane run exit {launched.returncode}"
    # wait even after a send error: only the marker is authoritative (nothing sent → nothing to wait for)
    deadline = time.time() + (START_CONFIRM_SECONDS if sent else 0)
    while not marker_matches(started, launch) and time.time() < deadline:
        time.sleep(0.5)
    with locked_state(args.run) as state:
        lane = get_lane(state, args.lane)
        if (lane.get("launching") or {}).get("launch") != launch:
            # superseded (a later start took over our stale reservation): its metadata is not ours to touch
            why = why or "this launch was superseded by another start"
        else:
            lane.pop("launching")
            if not marker_matches(started, launch):
                os.replace(meta_path, f"{meta_path}.withdrawn-{time.time_ns()}")  # a late runner now aborts
                why = why or f"runner did not report start within {START_CONFIRM_SECONDS}s (check the pane)"
            else:
                why = None  # the runner confirmed with our token: it is running, whatever herdr returned
                commit_launch(lane, attempt, args.reason, args.user_answer)
    if why:
        die(f"launch failed; lane unchanged: {why}")
    emit({"lane": args.lane, "attempt": attempt, "pane": pane})


def marker_matches(path: str, launch: str) -> bool:
    try:
        return read_json(path).get("launch") == launch
    except (OSError, json.JSONDecodeError):
        return False


def cmd_run(args: argparse.Namespace) -> None:
    """Executes inside the lane pane. Never writes lane state: the exit marker is the event."""
    with locked_state(args.run) as state:  # same lock as start's decision: confirm, or find it withdrawn
        lane = get_lane(state, args.lane)
        attempt = args.attempt
        meta_path = os.path.join(lane["lane_dir"], f"attempt-{attempt}.json")
        live = os.path.exists(meta_path) and read_json(meta_path).get("launch") == args.launch
        if live:
            write_json(os.path.join(lane["lane_dir"], f"attempt-{attempt}.started"),
                       {**identity(os.getpid()), "at": now(), "launch": args.launch})
    if not live:
        print(f"[lane-dispatch] attempt {attempt} was withdrawn by start; not running", flush=True)
        return
    meta = read_json(meta_path)
    if executor_mode(lane["executor"]) == "interactive":
        InteractiveRunner(args.run, lane, attempt, meta).main()
    else:
        run_headless(args.run, lane, attempt, meta)


def run_headless(run: str, lane: dict[str, Any], attempt: int, meta: dict[str, Any]) -> None:
    for sig in (signal.SIGTERM, signal.SIGHUP):  # pane closed / killed: still write the exit marker
        signal.signal(sig, _exit_on_signal)
    exit_path = os.path.join(lane["lane_dir"], f"attempt-{attempt}.exit")
    session_id, rc, proc = lane.get("session_id"), None, None
    log_path = os.path.join(lane["lane_dir"], f"attempt-{attempt}.log")
    try:
        if meta.get("after_minutes"):
            print(f"[lane-dispatch] waiting {meta['after_minutes']} min before attempt {attempt}", flush=True)
            time.sleep(60 * float(meta["after_minutes"]))
        cmd = build_exec_cmd(lane, attempt, meta["prompt"], SCHEMA)
        claude = lane["executor"]["provider"] == "claude"
        with open(log_path, "w", encoding="utf-8") as log:
            log.write(f"$ {' '.join(cmd[:2])} <prompt> {shlex.join(cmd[3:] if claude else cmd[2:-1])}\n")
            proc = spawn_executor(cmd, lane["lane_dir"], cwd=lane["worktree"], stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            assert proc.stdout is not None
            for line in proc.stdout:
                log.write(line)
                if not claude:
                    sys.stdout.write(line)
                    if line.startswith("session id:"):
                        session_id = line.split(":", 1)[1].strip()
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    sys.stdout.write(line)
                    continue
                shown = claude_display(event)
                if shown:
                    print(shown, flush=True)
                session_id = event.get("session_id") or session_id
                if event.get("type") == "result" and isinstance(event.get("structured_output"), dict):
                    write_json(os.path.join(lane["lane_dir"], f"result-{attempt}.json"), event["structured_output"])
            rc = proc.wait()
    except KeyboardInterrupt:
        rc = 130
    finally:
        stop_child(proc, lane["lane_dir"])
        write_json(exit_path, {"attempt": attempt, "exit": rc if rc is not None else -1,
                               "session_id": session_id, "ended_at": now()})
        ring(run, lane["name"], f"attempt {attempt} exited ({rc})")
        print(f"__LANE_EXIT__ {lane['name']} {attempt} {rc}", flush=True)


def agent_status(pane: str) -> str | None:
    info = herdr_json(["agent", "get", pane]) or {}
    return info.get("result", {}).get("agent", {}).get("agent_status")


def agent_session(pane: str, agent: str) -> str | None:
    """The session id `agent` reported to herdr for this pane (codex: its SessionStart hook), if any yet;
    a session another agent left on the pane never counts."""
    info = herdr_json(["agent", "get", pane]) or {}
    session = info.get("result", {}).get("agent", {}).get("agent_session") or {}
    return (session.get("value") or None) if session.get("agent") == agent else None


def agent_screen(pane: str) -> str:
    proc = subprocess.run(["herdr", "pane", "read", pane, "--source", "visible", "--lines", "40"],
                          capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return tail(proc.stdout, 40) if proc.returncode == 0 else ""


def claude_trusts(path: str) -> bool:
    """Whether the user already accepted Claude Code's folder-trust dialog for this path (read-only)."""
    try:
        projects = read_json(os.path.expanduser("~/.claude.json")).get("projects", {})
    except (OSError, json.JSONDecodeError, AttributeError):
        return False
    return bool((projects.get(os.path.realpath(path)) or {}).get("hasTrustDialogAccepted"))


def interactive_runner(lane: dict[str, Any]) -> dict[str, Any] | None:
    """The live runner whose agent TUI holds this lane's pane, if any. Unknown liveness counts as alive:
    a shell command must never be typed into a TUI we cannot rule out."""
    try:
        info = read_json(os.path.join(lane["lane_dir"], "agent.json"))
    except (OSError, json.JSONDecodeError):
        return None
    return info if owner_alive(info) else None


def _exit_on_signal(signum: int, _frame: Any) -> None:
    raise SystemExit(128 + signum)  # run the runner's `finally`: the attempt still gets its exit marker


def stop_child(child: subprocess.Popen[Any] | None, lane_dir: str) -> bool:
    """Never leave an executor running without its runner (nothing would mark its attempt's end).
    Returns whether it had to be stopped (a TUI killed this way may leave its terminal modes behind)."""
    stopped = bool(child and child.poll() is None)
    if child and stopped:
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
    with contextlib.suppress(OSError):
        os.remove(os.path.join(lane_dir, "executor.json"))
    return stopped


def spawn_executor(cmd: list[str], lane_dir: str, tty: str | None = None, **popen: Any) -> subprocess.Popen[Any]:
    """Start the executor and record its identity (and, for a TUI, the pane's tty), so a runner that dies
    hard (SIGKILL) leaves enough behind for `wait` to stop the orphan and repair the pane (reap_lost)."""
    child = subprocess.Popen(cmd, **popen)
    write_json(os.path.join(lane_dir, "executor.json"), {**identity(child.pid), "tty": tty})
    return child


# pop kitty keyboard modes on the alternate and the main screen (each keeps its own stack), leave the
# alternate screen without restoring a saved cursor (1047, not 1049), bracketed paste off, cursor on
TERMINAL_RESET = b"\033[<10u\033[?1047l\033[<10u\033[?2004l\033[?25h"


def reset_terminal(tty: str | None) -> None:
    """A TUI killed from outside skips its own cleanup: the pane keeps raw mode and the TUI's keyboard
    protocol, so the shell (and the next `pane run`) receives garbled keys. Restore the line discipline
    and the emulator modes the agent TUIs set."""
    if not tty:
        return
    with contextlib.suppress(OSError), open(tty, "r+b", buffering=0) as fh:
        subprocess.run(["stty", "sane"], stdin=fh, capture_output=True)
        fh.write(TERMINAL_RESET)


def stop_orphan(lane_dir: str, kill: bool = True) -> str | None:
    """Terminate an executor whose runner is gone (SIGTERM, then SIGKILL after 5 s) and repair the pane's
    terminal. Only a process whose recorded start time still matches is signalled (PID reuse).
    Returns what may still be running, or None when nothing is. `kill=False` only checks (and forgets
    a record whose process is gone)."""
    path = os.path.join(lane_dir, "executor.json")
    try:
        rec = read_json(path)
    except (OSError, json.JSONDecodeError):
        return None
    pid = rec.get("pid")

    def alive() -> bool | None:  # the recorded executor is still running; None = cannot tell
        try:
            current = process_identity(pid)
        except ProcessCheckUnavailable:
            return None
        if current is None:
            return False
        return rec.get("pid_start") is None or current == rec["pid_start"]

    for sig in (signal.SIGTERM, signal.SIGKILL):
        state = alive()
        if state is False:
            break
        if state is None or rec.get("pid_start") is None:  # never signal a process we cannot identify
            return f"executor pid {pid} may still run (not verifiable, not signalled); stop it in the pane"
        if not kill:
            return f"executor pid {pid} from an earlier attempt still runs; stop it in the pane"
        with contextlib.suppress(ProcessLookupError):
            os.kill(int(pid), sig)
        for _ in range(20):
            time.sleep(0.25)
            if alive() is not True:
                break
    if alive() is not False:
        return f"executor pid {pid} may still run; stop it in the pane"
    if kill:  # reap path, right after the runner died: the tty is still this lane's pane
        reset_terminal(rec.get("tty"))  # however the TUI ended (our signal or its own crash), it skipped cleanup
    os.remove(path)
    return None


def identity(pid: int) -> dict[str, Any]:
    """pid + start time for liveness checks by other processes (start time guards against PID reuse)."""
    try:
        pid_start = process_identity(pid)
    except ProcessCheckUnavailable:
        pid_start = None
    return {"pid": pid, "pid_start": pid_start}


class InteractiveRunner:
    """Owns the lane's agent TUI in the pane and serves consecutive attempts while the agent lives.
    An attempt ends when herdr reports the agent idle/done after it worked (or the agent exits); a
    blocked agent (permission dialog) raises an attention event without ending the attempt. The next
    attempt's metadata is confirmed under the state lock, exactly like a fresh runner, then prompted."""

    def __init__(self, run: str, lane: dict[str, Any], attempt: int, meta: dict[str, Any]) -> None:
        self.run, self.lane, self.attempt, self.meta = run, lane, attempt, meta
        self.pane = os.environ.get("HERDR_PANE_ID") or lane["pane_id"]
        self.dir = lane["lane_dir"]
        self.resume = bool(lane.get("session_id"))
        self.session = lane.get("session_id") or new_session(lane)
        self.ended = self.seen_working = self.blocked = self.trust_answered = False
        self.stop = False  # set when this agent must not serve another attempt (its prompt may still run)
        self.idle_ticks = 0
        self.deliver_at: float | None = None  # a picked-up attempt waiting for its --after delay
        self.prompted_at = time.time()  # when the attempt's prompt reached the agent (launch or delivery)
        self.tty = os.ttyname(0) if os.isatty(0) else None  # the pane's terminal, repaired if we kill the TUI

    def path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def pointer(self) -> str:
        """Write the attempt prompt to a file and hand the agent one line: TUIs differ on multi-line input."""
        n, draft = self.attempt, self.path(f"result-draft-{self.attempt}.json")
        with open(self.path(f"attempt-{n}.prompt.md"), "w", encoding="utf-8") as fh:
            fh.write(f"{self.meta['prompt'].strip()}\n\n---\n"
                     f"lane-dispatch attempt {n}. When you stop for any reason (done, partial or blocked), first "
                     f"record your result: write it as JSON to `{draft}` and run\n"
                     f"`python3 {SELF} report {self.run} {self.lane['name']} --attempt {n} --file {draft}`.\n"
                     f"It must match `{SCHEMA}` (status done|partial|blocked, summary, checklist, checks, commits, "
                     "blockers, deviations). If report prints an error, fix the JSON and run it again. Do not wait "
                     "for a human in this terminal: open questions go into blockers.\n")
        return f"Read {self.path(f'attempt-{n}.prompt.md')} and carry out its instructions (lane-dispatch attempt {n})."

    def main(self) -> None:
        signal.signal(signal.SIGINT, lambda *_: None)  # Ctrl+C belongs to the agent's TUI, not the runner
        for sig in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, _exit_on_signal)
        child: subprocess.Popen[bytes] | None = None
        rc: int | None = None
        try:
            if self.meta.get("after_minutes"):
                print(f"[lane-dispatch] waiting {self.meta['after_minutes']} min before attempt {self.attempt}", flush=True)
                time.sleep(60 * float(self.meta["after_minutes"]))
            cmd = build_interactive_cmd(self.lane, self.pointer(), self.session, self.resume)
            self.write_agent_file()
            child = spawn_executor(cmd, self.dir, self.tty, cwd=self.lane["worktree"])  # inherits the pane's terminal
            self.prompted_at = time.time()
            while (rc := child.poll()) is None and not self.stop:
                self.tick()
                time.sleep(INTERACTIVE_TICK)
        finally:
            screen = None if self.ended else agent_screen(self.pane)  # before the reset leaves the TUI's screen
            if stop_child(child, self.dir):
                reset_terminal(self.tty)
            if not self.ended:
                self.end(rc if rc is not None else -1, "exited", screen=screen)
            with contextlib.suppress(OSError):
                os.remove(self.path("agent.json"))
            print(f"__LANE_EXIT__ {self.lane['name']} {self.attempt} {rc}", flush=True)

    def write_agent_file(self) -> None:
        write_json(self.path("agent.json"), {**identity(os.getpid()), "attempt": self.attempt,
                                             "executor": self.lane["executor"], "session": self.session})

    def tick(self) -> None:
        if self.ended:
            self.pickup()
            return
        if self.deliver_at is not None:
            if time.time() >= self.deliver_at:
                self.deliver()
            return
        if self.session is None:  # codex names its thread itself; learn it while the TUI is up (resume needs it)
            self.session = agent_session(self.pane, self.lane["executor"]["provider"])
            if self.session:
                self.write_agent_file()  # survives a hard kill of this runner: reap_lost resumes from it
        status = agent_status(self.pane)
        if status == "working":
            self.seen_working, self.idle_ticks, self.blocked = True, 0, False
        elif status == "blocked":
            self.idle_ticks = 0
            if not self.blocked:
                self.blocked = True
                screen = agent_screen(self.pane)
                if self.accept_trust(screen):
                    self.blocked = False
                else:
                    self.attention(screen)
        elif status in ("idle", "done") and (self.seen_working or os.path.exists(self.result_path())):
            self.idle_ticks += 1
            if self.idle_ticks >= 2:  # two ticks: a stop between tool calls is not the end of the turn
                self.end(0, "report" if os.path.exists(self.result_path()) else "stopped")
        if not (self.ended or self.seen_working or self.blocked) and time.time() - self.prompted_at > START_GRACE:
            self.end(-1, "never_started")  # the screen in the marker shows why (e.g. a login prompt)
            self.stop = True  # a late start (login done) must not interleave with the next attempt's prompt

    def result_path(self) -> str:
        return self.path(f"result-{self.attempt}.json")

    def end(self, rc: int, via: str, error: str | None = None, screen: str | None = None) -> None:
        marker: dict[str, Any] = {"attempt": self.attempt, "exit": rc, "session_id": self.session,
                                  "ended_at": now(), "via": via, **({"error": error} if error else {})}
        if via != "report":
            marker["screen"] = agent_screen(self.pane) if screen is None else screen
        write_json(self.path(f"attempt-{self.attempt}.exit"), marker)
        self.ended = True
        ring(self.run, self.lane["name"], f"attempt {self.attempt} ended ({via})")

    def accept_trust(self, screen: str) -> bool:
        """Each new worktree makes interactive Claude ask for folder trust. Answer it only when the user
        already trusts the lane's main checkout (the worktree is that same repo), and only once."""
        if (self.trust_answered or self.lane["executor"]["provider"] != "claude"
                or "trust this folder" not in screen or not claude_trusts(self.lane["repo"])):
            return False
        self.trust_answered = True
        sh(["herdr", "pane", "send-keys", self.pane, "down", "enter"], check=False)  # "Yes, I trust this folder"
        return True

    def attention(self, screen: str) -> None:
        write_json(self.path("attention.json"), {"attempt": self.attempt, "seq": time.time_ns(), "status": "blocked",
                                                 "screen": screen, "at": now()})
        ring(self.run, self.lane["name"], f"attempt {self.attempt} is blocked and needs attention")

    def pickup(self) -> None:
        """Confirm the next attempt under the state lock, with the same token check a fresh runner makes."""
        n = self.attempt + 1
        meta_path, started = self.path(f"attempt-{n}.json"), self.path(f"attempt-{n}.started")
        if not os.path.exists(meta_path) or os.path.exists(started):
            return
        with locked_state(self.run) as state:
            lane = get_lane(state, self.lane["name"])
            try:
                meta = read_json(meta_path)
            except (OSError, json.JSONDecodeError):
                return
            pending = lane.get("launching") or {}
            if (pending.get("attempt") != n or pending.get("launch") != meta.get("launch")
                    or lane["executor"] != self.lane["executor"] or os.path.exists(started)):
                return
            write_json(started, {**identity(os.getpid()), "at": now(), "launch": meta["launch"]})
        self.attempt, self.meta = n, meta
        self.ended = self.seen_working = self.blocked = False
        self.idle_ticks = 0
        self.deliver_at = time.time() + 60 * float(meta.get("after_minutes") or 0)
        self.write_agent_file()

    def deliver(self) -> None:
        self.deliver_at, self.prompted_at = None, time.time()
        sent = sh(["herdr", "agent", "prompt", self.pane, self.pointer()], check=False)
        if sent.returncode != 0:
            self.end(-1, "prompt_failed", tail(sent.stderr or sent.stdout, 5))
            self.stop = True  # unknown what reached the TUI: the next attempt gets a fresh runner (resumed session)


def ring(run: str, lane: str, what: str) -> None:
    """Doorbell for when no waiter is listening (e.g. the orchestrator session restarted)."""
    if pid_alive(os.path.join(run_dir(run), "waiter.pid")):
        return
    pane = load_state(run)["orchestrator"].get("pane")
    if not pane or agent_status(pane) not in ("idle", "done"):
        return
    msg = f"[lane-dispatch] run {run}: lane {lane} {what}. Resume lane-dispatch run {run}."
    subprocess.run(["herdr", "agent", "prompt", pane, msg], capture_output=True, stdin=subprocess.DEVNULL)


def lane_event(lane: dict[str, Any], marker: dict[str, Any]) -> dict[str, Any]:
    attempt = marker["attempt"]
    result_path = os.path.join(lane["lane_dir"], f"result-{attempt}.json")
    event: dict[str, Any] = {"lane": lane["name"], "attempt": attempt, "exit": marker["exit"],
                             **{k: marker[k] for k in ("via", "error", "orphan") if k in marker}}
    if os.path.exists(result_path):
        try:
            event["result"] = read_json(result_path)
        except json.JSONDecodeError:
            event["result_raw"] = open(result_path, encoding="utf-8").read()[-2000:]
    elif "screen" in marker:  # interactive: what the agent's TUI showed when the attempt ended
        event["screen_tail"] = marker["screen"]
    elif os.path.exists(log := os.path.join(lane["lane_dir"], f"attempt-{attempt}.log")):
        with open(log, encoding="utf-8", errors="replace") as fh:
            event["log_tail"] = tail(fh.read(), 40)
    return event


def reap_lost(lane: dict[str, Any]) -> None:
    """A running attempt whose runner died without writing its exit marker (SIGKILL, OOM, herdr crash)
    would keep `wait` blocked forever: write the marker on its behalf, `via: lost`. Only a runner that is
    provably gone counts (unknown liveness = alive); the exit check after the liveness check closes the
    window where the runner writes its own marker and then exits."""
    n = lane["attempt"]
    exit_path = os.path.join(lane["lane_dir"], f"attempt-{n}.exit")
    try:
        started = read_json(os.path.join(lane["lane_dir"], f"attempt-{n}.started"))
    except (OSError, json.JSONDecodeError):
        return
    if os.path.exists(exit_path) or owner_alive(started) or os.path.exists(exit_path):
        return
    session = lane.get("session_id")
    with contextlib.suppress(OSError, json.JSONDecodeError):  # a first attempt's session lives only there
        agent = read_json(os.path.join(lane["lane_dir"], "agent.json"))
        if agent.get("executor") == lane["executor"]:
            session = session or agent.get("session")
    marker = {"attempt": n, "exit": -1, "session_id": session, "ended_at": now(), "via": "lost",
              "error": f"runner pid {started.get('pid')} ended without writing an exit marker"}
    if executor_mode(lane["executor"]) == "interactive" and lane.get("pane_id"):
        marker["screen"] = agent_screen(lane["pane_id"])  # what the orphaned TUI showed
    # the executor outlived its runner: stop it, so the attempt's end is true and the pane is a shell again
    if (orphan := stop_orphan(lane["lane_dir"])):
        marker["orphan"] = orphan
    write_json(exit_path, marker)


def new_attention(lane: dict[str, Any]) -> dict[str, Any] | None:
    try:
        att = read_json(os.path.join(lane["lane_dir"], "attention.json"))
    except (OSError, json.JSONDecodeError):
        return None
    return att if att.get("attempt") == lane["attempt"] and att.get("seq") != lane.get("attention_seen") else None


def schema_errors(value: Any, schema: dict[str, Any], where: str = "$") -> list[str]:
    """The JSON Schema subset lane-result.schema.json uses (type/enum/required/properties/items/
    additionalProperties:false) — enough to make `report` reject what `--json-schema` would."""
    kind = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "boolean": bool, "integer": int}
    if kind in types and (not isinstance(value, types[kind]) or (kind == "integer" and isinstance(value, bool))):
        return [f"{where}: expected {kind}"]
    errors = [f"{where}: must be one of {schema['enum']}"] if "enum" in schema and value not in schema["enum"] else []
    if kind == "object":
        props = schema.get("properties", {})
        errors += [f"{where}.{k}: missing" for k in schema.get("required", []) if k not in value]
        if schema.get("additionalProperties") is False:
            errors += [f"{where}.{k}: not allowed" for k in value if k not in props]
        for key, sub in props.items():
            if key in value:
                errors += schema_errors(value[key], sub, f"{where}.{key}")
    if kind == "array" and "items" in schema:
        for i, item in enumerate(value):
            errors += schema_errors(item, schema["items"], f"{where}[{i}]")
    return errors


def cmd_report(args: argparse.Namespace) -> None:
    """Called by an interactive executor at the end of an attempt: validate, then record result-N.json."""
    lane = get_lane(load_state(args.run), args.lane)
    if not (lane["phase"] == "running" and lane["attempt"] == args.attempt):
        die(f"attempt {args.attempt} is not lane {args.lane}'s running attempt ({lane['phase']}, attempt {lane['attempt']})")
    try:
        result = read_json(args.file)
    except (OSError, json.JSONDecodeError) as exc:
        die(f"cannot read {args.file}: {exc}")
    errors = schema_errors(result, read_json(SCHEMA))
    if errors:
        die("result does not match the schema: " + "; ".join(errors[:10]))
    path = os.path.join(lane["lane_dir"], f"result-{args.attempt}.json")
    write_json(path, result)
    emit({"lane": args.lane, "attempt": args.attempt, "recorded": path})


def cmd_wait(args: argparse.Namespace) -> None:
    pid_path = os.path.join(run_dir(args.run), "waiter.pid")
    with open(pid_path, "w") as fh:
        fh.write(str(os.getpid()))
    try:
        while True:
            state = load_state(args.run)
            running = [l for l in state["lanes"].values() if l["phase"] == "running"]
            for l in running:
                reap_lost(l)
            if not running:
                emit({"run": args.run, "idle": True, "phases": {n: l["phase"] for n, l in state["lanes"].items()}})
                return
            if any(new_attention(l) or os.path.exists(os.path.join(l["lane_dir"], f"attempt-{l['attempt']}.exit"))
                   for l in running):
                events: list[dict[str, Any]] = []
                with locked_state(args.run) as st:
                    for lane in st["lanes"].values():
                        exit_path = os.path.join(lane["lane_dir"], f"attempt-{lane['attempt']}.exit")
                        if lane["phase"] == "running" and os.path.exists(exit_path):
                            marker = read_json(exit_path)
                            lane["phase"] = "judging"
                            lane["session_id"] = marker.get("session_id") or lane.get("session_id")
                            lane["last_exit"] = marker
                            events.append(lane_event(lane, marker))
                        elif lane["phase"] == "running" and (att := new_attention(lane)):
                            lane["attention_seen"] = att["seq"]  # reported once; the lane keeps running
                            events.append({"lane": lane["name"], "attempt": lane["attempt"],
                                           "attention": att["status"], "screen_tail": att["screen"]})
                emit({"run": args.run, "events": events})
                return
            time.sleep(POLL_SECONDS)
    finally:
        with contextlib.suppress(OSError, ValueError):
            if int(open(pid_path).read().strip()) == os.getpid():
                os.remove(pid_path)


def cmd_verify(args: argparse.Namespace) -> None:
    state = load_state(args.run)
    lane = get_lane(state, args.lane)
    if lane["phase"] not in ("judging", "verify_failed", "verified", "reviewed", "ready", "published"):
        die(f"verify cannot run on a {lane['phase']} lane ({args.lane})")  # re-verify drops back to verified
    wt, report = lane["worktree"], {"lane": args.lane}
    before = tree_state(wt)
    commits = sh(["git", "-C", wt, "log", "--oneline", f"{lane['base_sha']}..HEAD"]).stdout.strip()
    report.update(branch_ok=before["branch"] == lane["branch"], clean=not before["dirty"],
                  commits=commits.splitlines())
    if before["dirty"]:
        report["dirty"] = before["dirty"]
    if lane["has_origin"] and report["clean"] and report["branch_ok"]:
        sh(["git", "-C", wt, "fetch", "origin", lane["pr_base"]])
        fresh = sh(["git", "-C", wt, "merge-base", "--is-ancestor", f"origin/{lane['pr_base']}", "HEAD"], check=False).returncode == 0
        if not fresh:
            merged = sh(["git", "-C", wt, "merge", "--no-edit", f"origin/{lane['pr_base']}"], check=False)
            if merged.returncode != 0:
                sh(["git", "-C", wt, "merge", "--abort"], check=False)
                report["freshness"] = f"merge of origin/{lane['pr_base']} conflicts; lane must resolve"
            else:
                report["freshness"] = f"merged origin/{lane['pr_base']}"
                fresh = True
        report["fresh"] = fresh
    preconditions = bool(report["branch_ok"] and report["clean"] and report.get("fresh", True))
    checked = tree_state(wt)  # exactly what the acceptance commands are about to validate
    checks: list[dict[str, Any]] = []
    if preconditions:
        for item in lane["acceptance"]:
            proc = shell(item["cmd"], wt)
            expect = item.get("expect_exit", 0)
            checks.append({"cmd": item["cmd"], "exit": proc.returncode, "expect_exit": expect,
                           "ok": proc.returncode == expect,
                           **({} if proc.returncode == expect else {"tail": tail(proc.stdout + proc.stderr)})})
        after = tree_state(wt)  # acceptance commands must not write, commit, or switch branches
        report["unchanged_by_checks"] = (not after["dirty"] and after["head"] == checked["head"]
                                         and after["branch"] == lane["branch"])
        if not report["unchanged_by_checks"]:
            report["after_checks"] = {"head": after["head"], "branch": after["branch"], "dirty": after["dirty"]}
    else:
        report["checks_skipped"] = "preconditions failed (branch / clean tree / freshness); acceptance not run"
    report["checks"] = checks
    report["passed"] = bool(preconditions and commits and report.get("unchanged_by_checks")
                            and all(c["ok"] for c in checks))
    report["head"] = checked["head"]
    with locked_state(args.run) as st:
        l = get_lane(st, args.lane)
        l["verify"] = {**report, "at": now()}
        l["phase"] = "verified" if report["passed"] else "verify_failed"
    emit(report)


class ProcessCheckUnavailable(Exception):
    """`ps` cannot run here (e.g. a restricted sandbox): liveness is unknown, not false."""


LSTART_RE = re.compile(r"^[A-Z][a-z]{2} [A-Z][a-z]{2} +\d{1,2} \d{2}:\d{2}:\d{2} \d{4}$")  # `ps -o lstart`


def process_identity(pid: Any) -> str | None:
    """`<start time>` of a live (non-zombie) process, or None if it is gone. Start time guards
    against PID reuse. Raises ProcessCheckUnavailable when the check itself cannot run."""
    try:
        pid = int(pid)
    except (ValueError, TypeError):
        return None
    try:
        proc = subprocess.run(["ps", "-o", "stat=,lstart=", "-p", str(pid)],
                              capture_output=True, text=True, stdin=subprocess.DEVNULL)
    except OSError as exc:
        raise ProcessCheckUnavailable(str(exc)) from exc
    out = proc.stdout.strip()
    if out:
        parts = out.split(None, 1)
        if len(parts) < 2 or not LSTART_RE.match(parts[1]):
            raise ProcessCheckUnavailable(f"unexpected ps output {out!r}")  # never guess, even for Z rows
        if parts[0].startswith("Z"):
            return None  # zombie: exited, only waiting to be reaped
        return parts[1]
    # No row: only a definite "no such process" counts as gone; anything else is unknown.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except OSError as exc:
        raise ProcessCheckUnavailable(str(exc)) from exc
    raise ProcessCheckUnavailable(f"ps exit {proc.returncode} without a row for existing pid {pid}: "
                                  f"{proc.stderr.strip()}")


def owner_alive(pending: dict[str, Any]) -> bool:
    """Unknown liveness counts as alive: a reservation is only reclaimed when its owner is provably gone."""
    try:
        started = process_identity(pending.get("pid"))
    except ProcessCheckUnavailable:
        return True
    if pending.get("pid_start") is None:  # start time could not be recorded: fall back to pid liveness
        return started is not None
    return started is not None and started == pending.get("pid_start")


def release_review(run: str, name: str, token: str) -> None:
    """Drop our reservation; only roll the phase back if nothing else changed it meanwhile."""
    with locked_state(run) as st:
        l = get_lane(st, name)
        if l.get("review_pending", {}).get("token") == token:
            l.pop("review_pending")
            if l["phase"] == "reviewing":
                l["phase"] = "verified"


def cmd_review(args: argparse.Namespace) -> None:
    """Reserve the round atomically (phase `reviewing` + owner token), write to a per-invocation report,
    and let only the owner record the verdict — concurrent or resumed reviews cannot cross wires."""
    token = f"{os.getpid()}-{time.time_ns()}"
    with locked_state(args.run) as st:
        lane = get_lane(st, args.lane)
        pending = lane.get("review_pending") or {}
        if lane["phase"] == "reviewing" and owner_alive(pending):
            die(f"review round {pending.get('round')} is already running (pid {pending.get('pid')})")
        if lane["phase"] not in ("verified", "reviewing"):  # `reviewing` here = owner died: reclaim
            die(f"review needs a verified lane; {args.lane} is {lane['phase']}")
        rnd = lane.get("review", {}).get("round", 0) + 1
        if rnd > MAX_REVIEW_ROUNDS:
            die(f"review round limit {MAX_REVIEW_ROUNDS} reached; escalate (HUMAN_CONFIRMATION_REQUIRED)")
        head = require_bound(lane, lane.get("verify", {}).get("head"), "review")["head"]
        # unknown start time is recorded as None: owner_alive then falls back to pid liveness
        lane["phase"], lane["review_pending"] = "reviewing", {"round": rnd, **identity(os.getpid()), "token": token}
    reviewer = lane["reviewer"]
    # three-dot: lane changes since the merge base, so a freshness merge of the target is not re-reviewed
    target = f"origin/{lane['pr_base']}" if lane["has_origin"] else lane["base_sha"]
    out_path = os.path.join(lane["lane_dir"], f"review-{rnd}-{token}.md")
    prompt = (
        f"使用 local_review skill，以 commit or range 模式 review 范围 {target}...{head}"
        f"（仓库 {lane['worktree']}，分支 {lane['branch']}）。本轮改动：{lane['objective']}。"
        f"这是该 lane 的第 {rnd}/{MAX_REVIEW_ROUNDS} 轮 review（同一 lane 同一 cycle，轮次不重置）。"
        "按 must_fix / should_fix / suggestion / accepted 分级，最后给出建议通过或不建议通过，"
        "并在报告最后单独一行输出 `DECISION: PASS`、`DECISION: BLOCK` 或 `DECISION: UNVERIFIED`。"
        + (f"\n跨仓 / 验证上下文（由编排者提供，请自行核实，不要直接采信）：{args.context}" if args.context else "")
    )
    cmd = ["codex", "exec", "-C", lane["worktree"], "-s", "read-only", "-m", reviewer["model"],
           "-c", f'model_reasoning_effort="{reviewer["effort"]}"', "-o", out_path, prompt]
    started = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    with open(f"{out_path[:-3]}.log", "w", encoding="utf-8") as fh:
        fh.write(proc.stdout + proc.stderr)
    fresh_report = os.path.exists(out_path) and os.path.getmtime(out_path) >= started
    if proc.returncode != 0 or not fresh_report:
        release_review(args.run, args.lane, token)
        emit({"lane": args.lane, "review_failed": True, "round_not_consumed": rnd,
              "command": shlex.join(cmd[:-1]) + " <prompt>", "exit": proc.returncode,
              "tail": tail(proc.stderr or proc.stdout)})
        sys.exit(1)
    decision = parse_decision(open(out_path, encoding="utf-8").read())
    if decision is None:  # no valid verdict: keep the report, do not consume the round, stay retryable
        release_review(args.run, args.lane, token)
        emit({"lane": args.lane, "review_invalid": True, "round_not_consumed": rnd, "report": out_path,
              "reason": "no single `DECISION: PASS|BLOCK|UNVERIFIED` line outside code fences; re-run review"})
        sys.exit(1)
    with locked_state(args.run) as st:
        l = get_lane(st, args.lane)
        if l["phase"] != "reviewing" or l.get("review_pending", {}).get("token") != token:
            die("this review lost its reservation (another review took over); verdict not recorded")
        l.pop("review_pending")
        l["review"] = {"round": rnd, "decision": decision, "path": out_path, "head": head, "at": now(),
                       "at_epoch": time.time(), **({"context": args.context} if args.context else {})}
        l["phase"] = "reviewed"
    emit({"lane": args.lane, "round": rnd, "decision": decision, "report": out_path})


def cmd_set_executor(args: argparse.Namespace) -> None:
    """Switch a lane's executor between attempts (user's choice at dispatch time). Sessions do not
    cross providers, so the next attempt starts a fresh session that reads TASK.md and progress.md."""
    config = load_config()
    try:
        executor = resolve_executor(args.preset, config)
    except ValueError as exc:
        die(str(exc))
    with locked_state(args.run) as state:
        lane = get_lane(state, args.lane)
        if lane["phase"] in ("running", "reviewing") or launch_active(lane):
            die(f"lane {args.lane} is {lane['phase']} or launching; switch executors only between attempts")
        if interactive_runner(lane):
            die(f"lane {args.lane}'s pane still runs its {lane['executor']['provider']} session; exit that agent "
                "in the pane first (its runner then ends), then switch")
        if (problem := sandbox_violation(executor, lane["sandbox"])):
            die(problem)
        withdraw_stale_launch(lane)  # a late runner must not start on the old plan
        if lane["executor"] != executor:
            lane["history"].append({"executor_switch": {"from": lane["executor"], "to": executor}, "at": now()})
            lane["executor"], lane["session_id"] = executor, None
    emit({"lane": args.lane, "executor": executor, "fresh_session": lane["session_id"] is None})


def withdraw_stale_launch(lane: dict[str, Any]) -> None:
    """Drop an abandoned, unconfirmed launch reservation and rename its metadata so a late runner aborts
    (and `start` finds nothing to adopt). Callers hold the state lock and have ruled out a live launch."""
    stale = lane.pop("launching", None)
    if stale:
        old_meta = os.path.join(lane["lane_dir"], f"attempt-{stale['attempt']}.json")
        if os.path.exists(old_meta) and read_json(old_meta).get("launch") == stale["launch"]:
            os.replace(old_meta, f"{old_meta}.withdrawn-{time.time_ns()}")


def cmd_escalate(args: argparse.Namespace) -> None:
    with locked_state(args.run) as state:
        lane = get_lane(state, args.lane)
        if lane["phase"] in TERMINAL:  # a finished or abandoned lane never comes back through escalation
            die(f"lane {args.lane} is {lane['phase']}; nothing to escalate")
        lane["phase"], lane["escalation"] = "escalated", {"reason": args.reason, "at": now()}
    emit({"lane": args.lane, "phase": "escalated"})


def cmd_abandon(args: argparse.Namespace) -> None:
    """The user ended a lane without it passing its gates (work superseded elsewhere, or dropped):
    terminal like `failed`, never `ready`. Refused while an attempt or review is still live."""
    with locked_state(args.run) as state:
        lane = get_lane(state, args.lane)
        if lane["phase"] in TERMINAL:
            die(f"lane {args.lane} is already {lane['phase']}")
        if lane["phase"] in ("running", "reviewing") or launch_active(lane) or interactive_runner(lane):
            die(f"lane {args.lane} is {lane['phase']} or its agent still runs; stop it first")
        withdraw_stale_launch(lane)
        lane["phase"], lane["abandoned"] = "abandoned", {"reason": args.reason, "from": lane["phase"], "at": now()}
    emit({"lane": args.lane, "phase": "abandoned"})


def cmd_ready(args: argparse.Namespace) -> None:
    """Only a PASS review of exactly the verified HEAD becomes ready; nothing else can be accepted here."""
    with locked_state(args.run) as state:
        lane = get_lane(state, args.lane)
        if lane["phase"] != "reviewed":
            die(f"ready needs a reviewed lane; {args.lane} is {lane['phase']}")
        review = lane["review"]
        if review.get("decision") != "PASS":
            die(f"review decision is {review.get('decision')}; only PASS becomes ready — fix or escalate")
        if review.get("head") != lane.get("verify", {}).get("head"):
            die("reviewed HEAD differs from verified HEAD; run verify and review again")
        require_bound(lane, review["head"], "ready")
        lane["phase"] = "ready"
    emit({"lane": args.lane, "phase": "ready", "publish": lane["publish"]})


def default_pr_body(lane: dict[str, Any], run: str) -> str:
    verify = lane.get("verify", {})
    checks = "\n".join(f"- `{c['cmd']}` → exit {c['exit']}" for c in verify.get("checks", []))
    review = lane.get("review", {})
    review_text = open(review["path"], encoding="utf-8").read().strip() if review.get("path") else "n/a"
    progress = open(os.path.join(lane["lane_dir"], "progress.md"), encoding="utf-8").read().strip()
    return (
        f"## Objective\n{lane['objective']}\n\n## Progress\n{progress or 'n/a'}\n\n"
        f"## Acceptance (re-run by the orchestrator)\n{checks or 'n/a'}\n\n"
        f"## local_review (round {review.get('round')}, decision {review.get('decision')})\n\n"
        f"<details><summary>report</summary>\n\n{review_text}\n\n</details>\n\n"
        f"---\nCode written by an agent in lane `{lane['name']}` of run `{run}`; the orchestrator "
        "authored the plan and verified acceptance and review before publishing.\n"
    )


def cmd_publish(args: argparse.Namespace) -> None:
    state = load_state(args.run)
    lane = get_lane(state, args.lane)
    if lane["phase"] != "ready":
        die(f"publish needs a ready lane; {args.lane} is {lane['phase']}")
    if lane["publish"] != "pr":
        die("lane publish mode is local; the task header did not ask for upload")
    if not lane["has_origin"]:
        die("repo has no origin; nothing to push to")
    wt, branch, target = lane["worktree"], lane["branch"], lane["pr_base"]
    head = require_bound(lane, lane.get("review", {}).get("head"), "publish")["head"]
    try:  # the PR repo is derived from the very remote we push to, never from gh's default repo
        repo_slug = github_slug(sh(["git", "-C", wt, "remote", "get-url", "origin"]).stdout.strip())
    except ValueError as exc:
        die(str(exc))
    sh(["git", "-C", wt, "fetch", "origin", target])
    if sh(["git", "-C", wt, "merge-base", "--is-ancestor", f"origin/{target}", head], check=False).returncode != 0:
        die(f"origin/{target} moved since verification; run verify (then review) again")
    base_sha = sh(["git", "-C", wt, "rev-parse", f"origin/{target}"]).stdout.strip()
    body_path = os.path.join(lane["lane_dir"], "pr.md")
    if not os.path.exists(body_path):
        with open(body_path, "w", encoding="utf-8") as fh:
            fh.write(default_pr_body(lane, args.run))
    elif os.path.getmtime(body_path) < lane["review"].get("at_epoch", float("inf")):
        die(f"{body_path} predates review round {lane['review']['round']}; rewrite it with the current "
            "acceptance and review results (or delete it to regenerate the default body)")
    open_prs = json.loads(sh(["gh", "pr", "list", "--repo", repo_slug, "--head", branch, "--state", "open",
                              "--json", "number,url,baseRefName,headRepository,headRepositoryOwner,isCrossRepository"],
                             cwd=wt).stdout or "[]")
    try:
        existing = pick_pr(open_prs, target, repo_slug)  # before pushing: never feed a PR on the wrong base
    except ValueError as exc:
        die(str(exc))
    sh(["git", "-C", wt, "push", "-u", "origin", f"{head}:refs/heads/{branch}"])
    if existing:  # refresh the body: it reports the acceptance and review of this very push
        sh(["gh", "pr", "edit", str(existing["number"]), "--repo", repo_slug, "--body-file", body_path], cwd=wt)
        url = existing["url"]
    else:
        title = lane.get("pr_title") or f"{lane['kind_rule']['commit']}: {lane['objective']}"
        url = sh(["gh", "pr", "create", "--repo", repo_slug, "--base", target, "--head", branch,
                  "--title", title, "--body-file", body_path], cwd=wt).stdout.strip().splitlines()[-1]
    with locked_state(args.run) as st:
        l = get_lane(st, args.lane)
        l["phase"], l["pr_url"], l["published"] = "published", url, {"head": head, "base_sha": base_sha, "at": now()}
    emit({"lane": args.lane, "phase": "published", "pr_url": url})


def cmd_config(_: argparse.Namespace) -> None:
    """Effective config (built-in defaults merged with ~/.agents/lane-dispatch/config.json), plus the
    presets grouped by agent type — the choices offered when the user picks an executor."""
    config = load_config()
    types = {kind: {"presets": sorted(p for p, ex in config["executors"].items() if ex["provider"] == kind),
                    "default": config["defaults"].get(kind), "modes": list(meta["modes"]),
                    "safe_sandbox": meta["sandboxed"], "installed": shutil.which(kind) is not None}
             for kind, meta in AGENT_TYPES.items()}
    emit({"path": CONFIG_PATH, "exists": os.path.exists(CONFIG_PATH), **config, "types": types})


CLOSABLE = {"published", "ready", "failed", "abandoned"}  # done or given up; escalated lanes still wait for the user


def open_workspace(lane: dict[str, Any]) -> bool:
    created = herdr_json(["workspace", "create", "--cwd", lane["worktree"], "--label", lane["name"], "--no-focus"])
    if not created:
        return False
    lane["workspace_id"] = created["result"]["workspace"]["workspace_id"]
    lane["pane_id"] = created["result"]["root_pane"]["pane_id"]
    lane.pop("workspace_closed", None)
    sh(["herdr", "pane", "rename", lane["pane_id"], "impl"], check=False)
    return True


def herdr_lookup(args: list[str], not_found: str) -> dict[str, Any] | str:
    """The `result` object of a successful herdr get; `"gone"` only when herdr exits non-zero with
    the explicit not-found error JSON (herdr writes errors to stderr); `"unknown"` for anything else
    (herdr missing or down, other errors, unreadable or mis-shaped JSON)."""
    try:
        proc = subprocess.run(["herdr", *args], capture_output=True, text=True, stdin=subprocess.DEVNULL)
        info = json.loads(proc.stdout if proc.returncode == 0 else proc.stderr)
    except (OSError, json.JSONDecodeError, TypeError):
        return "unknown"
    if not isinstance(info, dict):
        return "unknown"
    if proc.returncode != 0:
        error = info.get("error")
        return "gone" if isinstance(error, dict) and error.get("code") == not_found else "unknown"
    result = info.get("result")
    return result if isinstance(result, dict) else "unknown"


def workspace_owner(lane: dict[str, Any]) -> str:
    """`ours`, `gone`, `foreign`, or `unknown` for the lane's recorded workspace.
    ours: the recorded pane is in the recorded workspace and its absolute cwd is inside this lane's
    worktree (ids are reused after a herdr restart, so both must match). gone: no workspace is
    recorded (never created, or `close` confirmed it gone and forgot the id), or herdr explicitly
    answers `workspace_not_found`; a missing pane alone proves nothing. foreign: the workspace
    exists but is not provably ours. unknown: a herdr query failed or answered in an unexpected
    shape. Only `ours` may be closed and only `gone` may be reopened."""
    workspace_id = lane.get("workspace_id")
    if not workspace_id:
        return "gone"
    if lane.get("pane_id"):
        found = herdr_lookup(["pane", "get", lane["pane_id"]], "pane_not_found")
        if found == "unknown":
            return "unknown"
        if isinstance(found, dict):
            pane = found.get("pane")
            if not (isinstance(pane, dict) and isinstance(pane.get("cwd"), str) and os.path.isabs(pane["cwd"])
                    and isinstance(pane.get("workspace_id"), str)):
                return "unknown"
            root = os.path.realpath(lane["worktree"])
            cwd = os.path.realpath(pane["cwd"])
            if (cwd == root or cwd.startswith(root + os.sep)) and pane["workspace_id"] == workspace_id:
                return "ours"
    found = herdr_lookup(["workspace", "get", workspace_id], "workspace_not_found")
    if isinstance(found, str):
        return found
    ws = found.get("workspace")
    return "foreign" if isinstance(ws, dict) and ws.get("workspace_id") == workspace_id else "unknown"


def cmd_close(args: argparse.Namespace) -> None:
    """Close the herdr workspaces this run created for finished lanes (the orchestrator's last step).
    Worktrees stay: removing one discards uncommitted work, so that remains the user's call."""
    report = []
    with locked_state(args.run) as state:
        for name, lane in state["lanes"].items():
            if args.lane and name != args.lane:
                continue
            if not lane.get("workspace_id"):
                report.append({"lane": name, "result": "nothing to close"})
                continue
            if lane["phase"] not in CLOSABLE or launch_active(lane):
                report.append({"lane": name, "result": f"kept: lane is {lane['phase']}"})
                continue
            owner = workspace_owner(lane)
            if owner in ("foreign", "unknown"):
                why = "its workspace exists but is not provably this lane's" if owner == "foreign" else "herdr query failed"
                report.append({"lane": name, "result": f"kept: {why}"})
                continue
            if owner == "ours":
                closed = sh(["herdr", "workspace", "close", lane["workspace_id"]], check=False)
                if closed.returncode != 0:
                    report.append({"lane": name, "result": f"close failed: {tail(closed.stderr or closed.stdout, 3)}"})
                    continue
            # the workspace is provably gone now, so forget its id: a later workspace that reuses the
            # id is never ours, and `reopen` must not mistake it for this lane's
            closed_id = lane["workspace_id"]
            lane["workspace_closed"] = {"workspace_id": closed_id, "at": now(),
                                        "how": "closed" if owner == "ours" else "already gone"}
            lane.update(workspace_id=None, pane_id=None)
            report.append({"lane": name, "result": lane["workspace_closed"]["how"], "workspace_id": closed_id})
    emit({"run": args.run, "workspaces": report})


def cmd_reopen(args: argparse.Namespace) -> None:
    """Give a lane a fresh herdr workspace on its existing worktree (e.g. PR feedback after close)."""
    with locked_state(args.run) as state:
        lane = get_lane(state, args.lane)
        if lane["phase"] in ("running", "reviewing") or launch_active(lane):
            die(f"lane {args.lane} is {lane['phase']}; nothing to reopen")
        owner = workspace_owner(lane)
        if owner == "ours":
            die(f"lane {args.lane} still has its workspace {lane['workspace_id']}")
        if owner == "unknown":
            die("cannot confirm the lane's old workspace is gone (herdr query failed); nothing created")
        if owner == "foreign":
            die(f"workspace {lane['workspace_id']} still exists but is not provably this lane's; "
                "check it in herdr and close it yourself if it is, then reopen")
        if not os.path.isdir(lane["worktree"]):
            die(f"worktree {lane['worktree']} no longer exists")
        if not open_workspace(lane):
            die("herdr workspace create failed (run lanectl inside herdr)")
    emit({"lane": args.lane, "workspace_id": lane["workspace_id"], "pane_id": lane["pane_id"]})


def cmd_status(args: argparse.Namespace) -> None:
    state = load_state(args.run)
    rows = []
    for name, l in state["lanes"].items():
        rows.append({
            "lane": name, "repo": os.path.basename(l["repo"]), "branch": l["branch"], "phase": l["phase"],
            "executor": l["executor"]["name"] + ":" + l["executor"]["model"], "mode": executor_mode(l["executor"]),
            "attempt": l["attempt"], "continues": l["continues"], "fixes": l["fixes"],
            "review": l.get("review", {}).get("decision"), "pr": l.get("pr_url"),
            "pane": l.get("pane_id"), "worktree": l["worktree"],
        })
    emit({"run": state["run_id"], "orchestrator": state["orchestrator"],
          "waiter_alive": pid_alive(os.path.join(run_dir(args.run), "waiter.pid")), "lanes": rows})


def cmd_runs(_: argparse.Namespace) -> None:
    out = []
    if os.path.isdir(RUNS_DIR):
        for run in sorted(os.listdir(RUNS_DIR)):
            path = os.path.join(RUNS_DIR, run, "state.json")
            if os.path.exists(path):
                phases = {n: l["phase"] for n, l in read_json(path)["lanes"].items()}
                open_lanes = [n for n, p in phases.items() if p not in TERMINAL]
                out.append({"run": run, "open": open_lanes, "phases": phases})
    emit(out)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="lanectl", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init"); s.add_argument("--label"); s.set_defaults(fn=cmd_init)
    s = sub.add_parser("new"); s.add_argument("run"); s.add_argument("--spec", required=True)
    s.add_argument("--confirm-kind", action="store_true", help="the user confirmed a kind whose rule needs it")
    s.set_defaults(fn=cmd_new)
    s = sub.add_parser("start"); s.add_argument("run"); s.add_argument("lane")
    s.add_argument("--reason", default="initial", choices=sorted(REASONS))
    s.add_argument("--prompt"); s.add_argument("--prompt-file"); s.add_argument("--after", type=float)
    s.add_argument("--user-answer", help="required to restart an escalated lane: quote the user's decision")
    s.set_defaults(fn=cmd_start)
    s = sub.add_parser("run"); s.add_argument("run"); s.add_argument("lane"); s.add_argument("attempt", type=int)
    s.add_argument("--launch", required=True, help="token from start; the runner aborts if start withdrew it")
    s.set_defaults(fn=cmd_run)
    s = sub.add_parser("wait"); s.add_argument("run"); s.set_defaults(fn=cmd_wait)
    for name, fn in (("verify", cmd_verify), ("ready", cmd_ready), ("publish", cmd_publish)):
        s = sub.add_parser(name); s.add_argument("run"); s.add_argument("lane"); s.set_defaults(fn=fn)
    s = sub.add_parser("review"); s.add_argument("run"); s.add_argument("lane")
    s.add_argument("--context", help="cross-repo / verification facts for the reviewer to check (not trust)")
    s.set_defaults(fn=cmd_review)
    s = sub.add_parser("escalate"); s.add_argument("run"); s.add_argument("lane")
    s.add_argument("--reason", required=True); s.set_defaults(fn=cmd_escalate)
    s = sub.add_parser("abandon", help="end a lane by the user's decision (terminal; never ready)")
    s.add_argument("run"); s.add_argument("lane")
    s.add_argument("--reason", required=True, help="quote the user's decision and where the work went")
    s.set_defaults(fn=cmd_abandon)
    s = sub.add_parser("set-executor"); s.add_argument("run"); s.add_argument("lane")
    s.add_argument("preset", help="a preset name or an agent type (→ its default preset)")
    s.set_defaults(fn=cmd_set_executor)
    s = sub.add_parser("report", help="interactive executors: record this attempt's result JSON")
    s.add_argument("run"); s.add_argument("lane"); s.add_argument("--attempt", type=int, required=True)
    s.add_argument("--file", required=True, help="JSON file matching schema/lane-result.schema.json")
    s.set_defaults(fn=cmd_report)
    s = sub.add_parser("status"); s.add_argument("run"); s.set_defaults(fn=cmd_status)
    s = sub.add_parser("runs"); s.set_defaults(fn=cmd_runs)
    s = sub.add_parser("config"); s.set_defaults(fn=cmd_config)
    s = sub.add_parser("close"); s.add_argument("run"); s.add_argument("--lane"); s.set_defaults(fn=cmd_close)
    s = sub.add_parser("reopen"); s.add_argument("run"); s.add_argument("lane"); s.set_defaults(fn=cmd_reopen)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
