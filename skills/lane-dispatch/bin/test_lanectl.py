"""Checks for the parts of lanectl whose mistakes would ship silently: branch policy, limits,
codex command shape, review decision parsing, verify/publish gates, PR declaration.
Run (repo root): python3 -m unittest skills/lane-dispatch/bin/test_lanectl.py"""

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import lanectl as lc  # noqa: E402


def lane(**over):
    base = {"phase": "planned", "attempt": 0, "continues": 0, "fixes": 0}
    base.update(over)
    return base


GENERIC = lc.merge_config({})
DEVELOP = {"target": "develop", "prefix": "feature/", "commit": "feat"}


class BranchPolicy(unittest.TestCase):
    def test_base_comes_from_kind_not_current_branch(self):
        self.assertEqual(lc.expected_base(lc.kind_rule(GENERIC, "feature"), True), ("origin/main", "main"))
        self.assertEqual(lc.expected_base(lc.kind_rule(GENERIC, "fix"), False), ("main", "main"))
        self.assertEqual(lc.expected_base(DEVELOP, True), ("origin/develop", "develop"))
        with self.assertRaises(ValueError):
            lc.kind_rule(GENERIC, "hotfix")  # only configured kinds exist

    def test_override_must_agree_with_kind(self):
        rule = DEVELOP
        lc.check_base_override("feature", rule, True, "origin/develop")
        with self.assertRaises(ValueError):
            lc.check_base_override("feature", rule, True, "origin/staging")

    def test_branch_prefix_and_collision_suffix(self):
        fix = lc.kind_rule(GENERIC, "fix")
        self.assertEqual(lc.branch_name(fix, "fi-copy", "20260928-120000-x", False), "fix/fi-copy")
        feature = lc.kind_rule(GENERIC, "feature")
        self.assertEqual(lc.branch_name(feature, "fi-copy", "20260928-120000-abc", True), "feature/fi-copy-abc")
        with self.assertRaises(ValueError):
            lc.branch_name(feature, "Bad_Slug", "r", False)

    def test_config_kinds_extend_the_defaults_and_are_validated(self):
        mine = lc.merge_config({"kinds": {"feature": {"target": "trunk", "prefix": "feat/", "commit": "feat"},
                                          "docs": {"target": "trunk", "prefix": "docs/", "commit": "docs"}}})
        self.assertEqual(lc.expected_base(lc.kind_rule(mine, "feature"), True), ("origin/trunk", "trunk"))
        self.assertEqual(sorted(mine["kinds"]), ["docs", "feature", "fix"])
        for bad in ({"kinds": ["feature"]},
                    {"kinds": {"x": {"target": "main", "prefix": "x/"}}},  # commit missing
                    {"kinds": {"x": {"target": "main", "prefix": "x/", "commit": "x", "local": True}}},  # unknown field
                    {"kinds": {"x": {"target": "main", "prefix": "bad.. /", "commit": "x"}}},  # invalid branch
                    {"kinds": {"x": {"target": "ma in", "prefix": "x/", "commit": "x"}}}):  # invalid target
            with self.assertRaises(ValueError, msg=str(bad)):
                lc.merge_config(bad)

    def test_confirm_and_local_only_kinds_are_enforced_before_any_git_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, spec = os.path.join(tmp, "config.json"), os.path.join(tmp, "spec.json")
            lc.write_json(cfg, {"kinds": {"hotfix": {"target": "main", "prefix": "hotfix/", "commit": "fix",
                                                    "confirm": True, "local_only": True}}})
            base = {"lane": "h", "repo": "/nonexistent", "slug": "s", "objective": "o", "plan": "p",
                    "checklist": ["c"], "acceptance": [{"cmd": "true"}], "kind": "hotfix"}
            for over, args, needle in (({}, [], "--confirm-kind"),
                                       ({"publish": "pr"}, ["--confirm-kind"], "local-only")):
                lc.write_json(spec, {**base, **over})
                err = io.StringIO()
                with mock.patch.object(lc, "CONFIG_PATH", cfg), contextlib.redirect_stderr(err), \
                        self.assertRaises(SystemExit):
                    lc.main(["new", "r", "--spec", spec, *args])
                self.assertIn(needle, err.getvalue())


class StartLimits(unittest.TestCase):
    def test_concurrency_cap(self):
        lanes = {f"l{i}": lane(phase="running", attempt=1) for i in range(4)}
        lanes["new"] = lane()
        self.assertIn("concurrency", lc.start_violation(lanes, "new", "initial"))
        lanes["l0"]["phase"] = "judging"
        self.assertIsNone(lc.start_violation(lanes, "new", "initial"))

    def test_continuation_cap_counts_verify_retries(self):
        judging = {"a": lane(phase="judging", attempt=4, continues=3)}
        self.assertIn("limit", lc.start_violation(judging, "a", "continue"))
        failed = {"a": lane(phase="verify_failed", attempt=4, continues=3)}
        self.assertIn("limit", lc.start_violation(failed, "a", "verify"))
        self.assertIsNone(lc.start_violation(judging, "a", "answer"))

    def test_fix_only_after_non_pass_review_and_once(self):
        passed = {"a": lane(phase="reviewed", attempt=1, review={"decision": "PASS"})}
        self.assertIsNotNone(lc.start_violation(passed, "a", "fix"))
        blocked = {"a": lane(phase="reviewed", attempt=1, review={"decision": "BLOCK"})}
        self.assertIsNone(lc.start_violation(blocked, "a", "fix"))
        blocked["a"]["fixes"] = lc.MAX_REVIEW_ROUNDS - 2
        self.assertIsNone(lc.start_violation(blocked, "a", "fix"))  # round 6 fix -> review 7/7
        blocked["a"]["fixes"] = lc.MAX_REVIEW_ROUNDS - 1
        self.assertIn("limit", lc.start_violation(blocked, "a", "fix"))

    def test_reviewed_lane_cannot_dodge_fix_limit_with_other_reasons(self):
        spent = {"a": lane(phase="reviewed", attempt=3, fixes=6, continues=3, review={"decision": "BLOCK"})}
        for reason in ("answer", "ratelimit", "continue", "verify"):
            self.assertIsNotNone(lc.start_violation(spent, "a", reason), reason)

    def test_escalated_lane_needs_quoted_user_answer(self):
        esc = {"a": lane(phase="escalated", attempt=2)}
        self.assertIn("--user-answer", lc.start_violation(esc, "a", "answer"))
        self.assertIsNone(lc.start_violation(esc, "a", "answer", "用户：按方案 B 继续"))

    def test_attempt_ceiling(self):
        self.assertIn("attempt limit", lc.start_violation({"a": lane(phase="judging", attempt=lc.MAX_ATTEMPTS)}, "a", "answer"))

    def test_running_lane_cannot_restart(self):
        self.assertIsNotNone(lc.start_violation({"a": lane(phase="running", attempt=1)}, "a", "continue"))


SCHEMA_FILE = lc.SCHEMA


class ExecutorCommand(unittest.TestCase):
    BASE = {"lane_dir": "/runs/r/lanes/a", "git_common_dir": "/repo/.git", "worktree": "/wt/a",
            "sandbox": "safe", "writable": [], "session_id": "sid"}
    SOL = {"name": "sol", "provider": "codex", "model": "gpt-6-sol", "effort": "xhigh"}
    OPUS = {"name": "opus", "provider": "claude", "model": "claude-opus-5-5", "effort": "xhigh"}

    def lane(self, executor, **over):
        return {**self.BASE, "executor": executor, **over}

    def test_codex_first_attempt_is_sandboxed_exec_in_worktree(self):
        cmd = lc.build_exec_cmd(self.lane(self.SOL), 1, "go", SCHEMA_FILE)
        self.assertEqual(cmd[:2], ["codex", "exec"])
        self.assertNotIn("resume", cmd)
        self.assertIn('sandbox_mode="workspace-write"', cmd)
        self.assertIn('sandbox_workspace_write.writable_roots=["/runs/r/lanes/a", "/repo/.git"]', cmd)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", cmd)
        self.assertEqual(cmd[cmd.index("-C") + 1], "/wt/a")
        self.assertEqual(cmd[-1], "go")

    def test_codex_later_attempt_resumes_same_session_keeping_sandbox(self):
        cmd = lc.build_exec_cmd(self.lane(self.SOL), 2, "fix", SCHEMA_FILE)
        self.assertEqual(cmd[:4], ["codex", "exec", "resume", "sid"])
        self.assertNotIn("-C", cmd)  # resume has no -C; runner sets cwd
        self.assertIn('sandbox_mode="workspace-write"', cmd)

    def test_claude_is_sandboxed_headless_with_schema_and_extra_roots(self):
        cmd = lc.build_exec_cmd(self.lane(self.OPUS), 1, "go", SCHEMA_FILE)
        self.assertEqual(cmd[:3], ["claude", "-p", "go"])
        self.assertEqual(cmd[cmd.index("--model") + 1], "claude-opus-5-5")
        self.assertEqual(json.loads(cmd[cmd.index("--settings") + 1])["sandbox"]["enabled"], True)
        self.assertEqual(json.loads(cmd[cmd.index("--json-schema") + 1]), json.load(open(SCHEMA_FILE)))
        self.assertEqual(cmd[cmd.index("--add-dir") + 1:], ["/runs/r/lanes/a", "/repo/.git"])  # variadic, last
        self.assertNotIn("--resume", cmd)
        self.assertNotIn("--dangerously-skip-permissions", cmd)

    def test_claude_later_attempt_resumes_session(self):
        cmd = lc.build_exec_cmd(self.lane(self.OPUS), 2, "fix", SCHEMA_FILE)
        self.assertEqual(cmd[cmd.index("--resume") + 1], "sid")

    def test_yolo_only_when_lane_opts_in(self):
        codex = lc.build_exec_cmd(self.lane(self.SOL, sandbox="yolo"), 1, "go", SCHEMA_FILE)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", codex)
        self.assertFalse(any("sandbox_mode" in part for part in codex))
        claude = lc.build_exec_cmd(self.lane(self.OPUS, sandbox="yolo"), 1, "go", SCHEMA_FILE)
        self.assertIn("--dangerously-skip-permissions", claude)
        self.assertNotIn("--settings", claude)

    def test_claude_stream_events_render_for_the_pane(self):
        tool = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash",
                                                              "input": {"command": "uv run pytest"}}]}}
        self.assertEqual(lc.claude_display(tool), "→ Bash uv run pytest")
        self.assertIsNone(lc.claude_display({"type": "system"}))


class LaunchConfirmation(unittest.TestCase):
    """`start` must not record `running` unless the runner in the pane actually came up."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = (lc.RUNS_DIR, lc.START_CONFIRM_SECONDS, os.environ["PATH"])
        lc.RUNS_DIR, lc.START_CONFIRM_SECONDS = os.path.join(self.tmp.name, "runs"), 1
        self.lane_dir = os.path.join(lc.RUNS_DIR, "r", "lanes", "a")
        os.makedirs(self.lane_dir)
        bindir = os.path.join(self.tmp.name, "bin")
        os.makedirs(bindir)
        fake = os.path.join(bindir, "herdr")  # `pane run` "starts" the runner (writes its marker) only if FAKE_STARTED
        open(fake, "w").write(  # a bare shell pane: `agent get` answers herdr's explicit agent_not_found
            '#!/bin/sh\n[ "$1 $2" = "agent get" ] && echo \'{"error": {"code": "agent_not_found"}}\' >&2 && exit 1\n'
            '[ "$2" = run ] && [ -n "$FAKE_DURING_RUN" ] && python3 "$FAKE_DURING_RUN"\n'
            '[ "$2" = run ] && [ -n "$FAKE_STARTED" ] && '
            'tok=$(printf %s "$4" | sed -n "s/.*--launch \\([^ ]*\\).*/\\1/p") && '
            'printf \'{"launch": "%s"}\' "$tok" > "$FAKE_STARTED"\nexit ${FAKE_EXIT:-0}\n')
        os.chmod(fake, 0o755)
        os.environ["PATH"] = f"{bindir}:{os.environ['PATH']}"
        lc.write_json(os.path.join(lc.RUNS_DIR, "r", "state.json"), {"run_id": "r", "orchestrator": {}, "lanes": {"a": {
            "name": "a", "phase": "planned", "attempt": 0, "continues": 0, "fixes": 0, "lane_dir": self.lane_dir,
            "pane_id": "w9:p1", "history": [], "executor": {"name": "opus"}, "sandbox": "safe"}}})

    def tearDown(self):
        lc.RUNS_DIR, lc.START_CONFIRM_SECONDS, os.environ["PATH"] = self.old
        os.environ.pop("FAKE_STARTED", None)
        self.tmp.cleanup()

    def start(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                lc.main(["start", "r", "a"])
                code = 0
            except SystemExit as exc:
                code = exc.code
        return code, lc.read_json(os.path.join(lc.RUNS_DIR, "r", "state.json"))["lanes"]["a"]

    def test_runner_that_never_starts_leaves_the_lane_unchanged(self):
        code, lane = self.start()
        self.assertNotEqual(code, 0)
        self.assertEqual((lane["phase"], lane["attempt"]), ("planned", 0))
        self.assertFalse(os.path.exists(os.path.join(self.lane_dir, "attempt-1.json")))

    def test_confirmed_start_records_running(self):
        os.environ["FAKE_STARTED"] = os.path.join(self.lane_dir, "attempt-1.started")
        code, lane = self.start()
        self.assertEqual((code, lane["phase"], lane["attempt"]), (0, "running", 1))

    def test_stale_marker_from_an_earlier_launch_does_not_confirm(self):
        marker = os.path.join(self.lane_dir, "attempt-1.started")
        lc.write_json(marker, {"launch": "an-earlier-launch"})  # late write by a previously timed-out runner
        code, lane = self.start()
        self.assertNotEqual(code, 0)
        self.assertEqual((lane["phase"], lane["attempt"]), ("planned", 0))

    def test_runner_arriving_after_withdrawal_does_not_run(self):
        code, _ = self.start()  # times out → metadata withdrawn
        self.assertNotEqual(code, 0)
        withdrawn = [f for f in os.listdir(self.lane_dir) if f.startswith("attempt-1.json.withdrawn")]
        token = lc.read_json(os.path.join(self.lane_dir, withdrawn[0]))["launch"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            lc.main(["run", "r", "a", "1", "--launch", token])
        self.assertIn("withdrawn", out.getvalue())
        self.assertFalse(os.path.exists(os.path.join(self.lane_dir, "attempt-1.started")))


class LaunchRaces(LaunchConfirmation):
    def state(self):
        return lc.read_json(os.path.join(lc.RUNS_DIR, "r", "state.json"))

    def put(self, **lane_over):
        st = self.state()
        st["lanes"]["a"].update(lane_over)
        lc.write_json(os.path.join(lc.RUNS_DIR, "r", "state.json"), st)

    def test_superseded_old_start_does_not_touch_the_new_launch(self):
        # while the old start waits unlocked, a newer start takes over the (same-numbered) reservation
        meta = os.path.join(self.lane_dir, "attempt-1.json")
        state = os.path.join(lc.RUNS_DIR, "r", "state.json")
        hook = os.path.join(self.tmp.name, "takeover.py")
        open(hook, "w").write(
            "import json\n"
            f"s = json.load(open({state!r})); s['lanes']['a']['launching'] = {{'attempt': 1, 'launch': 'newer', 'at_epoch': 0}}\n"
            f"json.dump(s, open({state!r}, 'w')); json.dump({{'launch': 'newer', 'reason': 'initial'}}, open({meta!r}, 'w'))\n")
        os.environ["FAKE_DURING_RUN"] = hook
        try:
            code, lane = self.start()
        finally:
            os.environ.pop("FAKE_DURING_RUN")
        self.assertNotEqual(code, 0)
        self.assertEqual(lc.read_json(meta)["launch"], "newer")  # not withdrawn by the superseded start
        self.assertEqual(lane["launching"]["launch"], "newer")
        self.assertEqual(lane["phase"], "planned")

    def test_abandoned_reservation_with_a_confirmed_runner_is_adopted_not_relaunched(self):
        lc.write_json(os.path.join(self.lane_dir, "attempt-1.json"), {"launch": "orphan", "reason": "initial"})
        lc.write_json(os.path.join(self.lane_dir, "attempt-1.started"), {"launch": "orphan"})
        self.put(launching={"attempt": 1, "launch": "orphan", "at_epoch": time.time() - 3600})
        code, lane = self.start()
        self.assertEqual((code, lane["phase"], lane["attempt"]), (0, "running", 1))
        self.assertNotIn("launching", lane)
        self.assertFalse(os.path.exists(os.path.join(self.lane_dir, "attempt-2.json")))  # no second launch

    def test_abandoned_unconfirmed_reservation_is_withdrawn_then_relaunched(self):
        lc.write_json(os.path.join(self.lane_dir, "attempt-1.json"), {"launch": "orphan", "reason": "initial"})
        self.put(launching={"attempt": 1, "launch": "orphan", "at_epoch": time.time() - 3600})
        os.environ["FAKE_STARTED"] = os.path.join(self.lane_dir, "attempt-1.started")
        code, lane = self.start()
        self.assertEqual((code, lane["phase"], lane["attempt"]), (0, "running", 1))
        self.assertNotEqual(lc.read_json(os.path.join(self.lane_dir, "attempt-1.json"))["launch"], "orphan")

    def test_fresh_launch_reservations_count_toward_the_cap_and_block_executor_switch(self):
        lanes = {f"l{i}": {"phase": "planned", "attempt": 0, "continues": 0, "fixes": 0,
                           "launching": {"attempt": 1, "launch": "x", "at_epoch": time.time()}} for i in range(4)}
        lanes["new"] = {"phase": "planned", "attempt": 0, "continues": 0, "fixes": 0}
        self.assertIn("concurrency", lc.start_violation(lanes, "new", "initial"))
        for l in lanes.values():
            l.get("launching", {})["at_epoch"] = time.time() - 3600  # stale reservations do not count
        self.assertIsNone(lc.start_violation(lanes, "new", "initial"))
        self.put(launching={"attempt": 1, "launch": "x", "at_epoch": time.time()},
                 executor={"name": "opus", "provider": "claude", "model": "claude-opus-5-5", "effort": "xhigh"})
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                lc.main(["set-executor", "r", "a", "sol"])
        self.assertEqual(self.state()["lanes"]["a"]["executor"]["name"], "opus")


    def test_confirmed_launch_survives_a_failing_pane_run(self):
        os.environ["FAKE_STARTED"] = os.path.join(self.lane_dir, "attempt-1.started")
        os.environ["FAKE_EXIT"] = "1"  # herdr reports an error, but the runner did come up with our token
        try:
            code, lane = self.start()
        finally:
            os.environ.pop("FAKE_EXIT")
        self.assertEqual((code, lane["phase"], lane["attempt"]), (0, "running", 1))
        self.assertTrue(os.path.exists(os.path.join(self.lane_dir, "attempt-1.json")))  # not withdrawn

    def test_stale_but_confirmed_reservation_counts_and_blocks_executor_switch(self):
        stale = {"attempt": 1, "launch": "orphan", "at_epoch": time.time() - 3600}
        lc.write_json(os.path.join(self.lane_dir, "attempt-1.started"), {"launch": "orphan"})
        lanes = {f"l{i}": {"phase": "planned", "attempt": 0, "continues": 0, "fixes": 0,
                           "lane_dir": self.lane_dir, "launching": dict(stale)} for i in range(4)}
        lanes["new"] = {"phase": "planned", "attempt": 0, "continues": 0, "fixes": 0}
        self.assertIn("concurrency", lc.start_violation(lanes, "new", "initial"))
        self.put(launching=dict(stale),
                 executor={"name": "opus", "provider": "claude", "model": "claude-opus-5-5", "effort": "xhigh"})
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                lc.main(["set-executor", "r", "a", "sol"])
        self.assertEqual(self.state()["lanes"]["a"]["executor"]["name"], "opus")


    def test_lane_without_a_pane_cannot_start(self):
        self.put(pane_id=None)
        code, lane = self.start()
        self.assertNotEqual(code, 0)
        self.assertEqual((lane["phase"], lane["attempt"]), ("planned", 0))
        self.assertFalse(os.path.exists(os.path.join(self.lane_dir, "attempt-1.json")))

    def test_lane_without_a_pane_is_refused_before_touching_stale_reservations(self):
        for confirmed in (False, True):
            meta = os.path.join(self.lane_dir, "attempt-1.json")
            lc.write_json(meta, {"launch": "orphan", "reason": "initial"})
            marker = os.path.join(self.lane_dir, "attempt-1.started")
            if confirmed:
                lc.write_json(marker, {"launch": "orphan"})
            self.put(pane_id=None, phase="planned", attempt=0,
                     launching={"attempt": 1, "launch": "orphan", "at_epoch": time.time() - 3600})
            code, lane = self.start()
            self.assertNotEqual(code, 0, confirmed)
            self.assertEqual((lane["phase"], lane["launching"]["launch"]), ("planned", "orphan"), confirmed)
            self.assertEqual(lc.read_json(meta)["launch"], "orphan", confirmed)  # untouched
            if confirmed:
                os.remove(marker)

    def test_executor_switch_withdraws_a_stale_unconfirmed_launch(self):
        lc.write_json(os.path.join(self.lane_dir, "attempt-1.json"), {"launch": "orphan", "reason": "initial"})
        self.put(launching={"attempt": 1, "launch": "orphan", "at_epoch": time.time() - 3600},
                 executor={"name": "opus", "provider": "claude", "model": "claude-opus-5-5", "effort": "xhigh"})
        with contextlib.redirect_stdout(io.StringIO()):
            lc.main(["set-executor", "r", "a", "sol"])
        lane = self.state()["lanes"]["a"]
        self.assertEqual(lane["executor"]["name"], "sol")
        self.assertNotIn("launching", lane)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            lc.main(["run", "r", "a", "1", "--launch", "orphan"])  # the late runner of the abandoned launch
        self.assertIn("withdrawn", out.getvalue())
        self.assertFalse(os.path.exists(os.path.join(self.lane_dir, "attempt-1.started")))


class WorkspaceCleanup(unittest.TestCase):
    """close/reopen talk to herdr through a scripted fake: `pane get` answers from FAKE_PANES (json map),
    `workspace close|create` are logged, so tests see exactly which workspaces would be touched."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.old = (lc.RUNS_DIR, os.environ["PATH"])
        lc.RUNS_DIR = os.path.join(root, "runs")
        os.makedirs(os.path.join(lc.RUNS_DIR, "r", "lanes"))
        self.log = os.path.join(root, "herdr.log")
        bindir = os.path.join(root, "bin")
        os.makedirs(bindir)
        fake = os.path.join(bindir, "herdr")
        open(fake, "w").write(
            "#!/usr/bin/env python3\nimport json, os, sys\na = sys.argv[1:]\n"
            f"open({self.log!r}, 'a').write(' '.join(a) + '\\n')\n"
            "if a[:2] == ['pane', 'get'] and os.environ.get('FAKE_PANE_FAIL'):\n"
            "    sys.stderr.write('socket unavailable'); sys.exit(1)\n"
            "if a[:2] == ['workspace', 'close'] and os.environ.get('FAKE_CLOSE_FAIL'):\n"
            "    sys.stderr.write('close refused'); sys.exit(1)\n"
            "if a[:2] == ['pane', 'get'] and 'FAKE_PANE_RAW' in os.environ:\n"
            "    print(os.environ['FAKE_PANE_RAW']); sys.exit(0)\n"
            "if a[:2] == ['workspace', 'get'] and 'FAKE_WS_RAW' in os.environ:\n"
            "    print(os.environ['FAKE_WS_RAW']); sys.exit(0)\n"
            "if a[:2] == ['workspace', 'get']:\n"
            "    found = a[2] in os.environ.get('FAKE_WORKSPACES', '').split(',')\n"
            "    if not found:  # like real herdr: error JSON on stderr, exit 1\n"
            "        sys.stderr.write(json.dumps({'error': {'code': 'workspace_not_found'}})); sys.exit(1)\n"
            "    print(json.dumps({'result': {'workspace': {'workspace_id': a[2]}}})); sys.exit(0)\n"
            "if a[:2] == ['pane', 'get']:\n"
            "    pane = json.loads(os.environ.get('FAKE_PANES', '{}')).get(a[2])\n"
            "    if not pane:\n"
            "        sys.stderr.write(json.dumps({'error': {'code': 'pane_not_found'}})); sys.exit(1)\n"
            "    print(json.dumps({'result': {'pane': pane}}))\n"
            "elif a[:2] == ['workspace', 'create']:\n"
            "    print(json.dumps({'result': {'workspace': {'workspace_id': 'wNEW'}, 'root_pane': {'pane_id': 'wNEW:p1'}}}))\n")
        os.chmod(fake, 0o755)
        os.environ["PATH"] = f"{bindir}:{os.environ['PATH']}"
        self.wts = {}
        lanes = {}
        for name, phase, ws in (("done", "published", "wA"), ("busy", "running", "wB"), ("gone", "ready", "wC"),
                                ("moved", "published", "wD")):
            wt = os.path.join(root, f"wt-{name}")
            os.makedirs(wt)
            self.wts[name] = wt
            lanes[name] = {"name": name, "phase": phase, "worktree": wt, "workspace_id": ws, "pane_id": f"{ws}:p1",
                           "lane_dir": os.path.join(lc.RUNS_DIR, "r", "lanes", name), "attempt": 1, "history": []}
        lc.write_json(os.path.join(lc.RUNS_DIR, "r", "state.json"), {"run_id": "r", "orchestrator": {}, "lanes": lanes})
        os.environ["FAKE_PANES"] = json.dumps({
            "wA:p1": {"workspace_id": "wA", "cwd": self.wts["done"]},
            "wB:p1": {"workspace_id": "wB", "cwd": self.wts["busy"]},
            "wD:p1": {"workspace_id": "wD", "cwd": "/somewhere/else"},  # id reused by an unrelated pane
        })
        os.environ["FAKE_WORKSPACES"] = "wA,wB,wD"  # wC is gone; wD's id now belongs to someone else

    def tearDown(self):
        lc.RUNS_DIR, os.environ["PATH"] = self.old
        os.environ.pop("FAKE_PANES", None)
        os.environ.pop("FAKE_PANE_FAIL", None)
        os.environ.pop("FAKE_CLOSE_FAIL", None)
        os.environ.pop("FAKE_PANE_RAW", None)
        os.environ.pop("FAKE_WORKSPACES", None)
        os.environ.pop("FAKE_WS_RAW", None)
        self.tmp.cleanup()

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                lc.main(list(argv))
                code = 0
            except SystemExit as exc:
                code = exc.code
        return code, lc.read_json(os.path.join(lc.RUNS_DIR, "r", "state.json"))["lanes"]

    def closed_ids(self):
        return [l.split()[2] for l in open(self.log).read().splitlines() if l.startswith("workspace close")]

    def test_close_touches_only_our_finished_workspaces(self):
        code, lanes = self.run_cli("close", "r")
        self.assertEqual(code, 0)
        self.assertEqual(self.closed_ids(), ["wA"])  # busy kept, gone not re-closed, moved not ours
        self.assertEqual(lanes["done"]["workspace_closed"]["how"], "closed")
        self.assertIsNone(lanes["done"]["pane_id"])
        self.assertEqual(lanes["gone"]["workspace_closed"]["how"], "already gone")
        self.assertNotIn("workspace_closed", lanes["busy"])
        self.assertNotIn("workspace_closed", lanes["moved"])
        self.assertEqual(lanes["moved"]["pane_id"], "wD:p1")

    def test_failed_herdr_query_or_close_changes_nothing(self):
        for env in ("FAKE_PANE_FAIL", "FAKE_CLOSE_FAIL"):
            os.environ[env] = "1"
            try:
                code, lanes = self.run_cli("close", "r", "--lane", "done")
            finally:
                os.environ.pop(env)
            self.assertEqual(code, 0)
            self.assertNotIn("workspace_closed", lanes["done"], env)
            self.assertEqual(lanes["done"]["pane_id"], "wA:p1", env)

    def test_reopen_refuses_while_the_old_workspace_is_ours_or_unconfirmable(self):
        code, lanes = self.run_cli("reopen", "r", "done")
        self.assertNotEqual(code, 0)
        self.assertEqual(lanes["done"]["workspace_id"], "wA")
        os.environ["FAKE_PANE_FAIL"] = "1"
        code, lanes = self.run_cli("reopen", "r", "gone")  # its pane may still exist: herdr did not say
        self.assertNotEqual(code, 0)
        self.assertEqual(lanes["gone"]["workspace_id"], "wC")
        self.assertNotIn("workspace create", open(self.log).read())

    def test_unprovable_ownership_keeps_state_and_creates_nothing(self):
        # mis-shaped herdr answers for the pane of lane `done`
        for raw in ("[]", '{"result": []}', '{"result": {"pane": {"workspace_id": "wA"}}}',
                    '{"result": {"pane": {"workspace_id": "wA", "cwd": ""}}}',
                    '{"result": {"pane": {"workspace_id": "wA", "cwd": "."}}}'):
            os.environ["FAKE_PANE_RAW"] = raw
            code, lanes = self.run_cli("close", "r", "--lane", "done")
            self.assertEqual(code, 0, raw)
            self.assertNotIn("workspace_closed", lanes["done"], raw)
            code, _ = self.run_cli("reopen", "r", "done")
            self.assertNotEqual(code, 0, raw)
        os.environ.pop("FAKE_PANE_RAW")
        self.assertNotIn("workspace create", open(self.log).read())

    def test_workspace_recorded_without_pane_is_checked_not_assumed_gone(self):
        with lc.locked_state("r") as st:
            st["lanes"]["done"]["pane_id"] = None
        os.environ["FAKE_WORKSPACES"] = "wA"  # still exists: cannot prove it is ours, so leave it alone
        code, lanes = self.run_cli("close", "r", "--lane", "done")
        self.assertNotIn("workspace_closed", lanes["done"])
        code, _ = self.run_cli("reopen", "r", "done")
        self.assertNotEqual(code, 0)
        self.assertEqual(self.closed_ids(), [])
        self.assertNotIn("workspace create", open(self.log).read())
        os.environ["FAKE_WORKSPACES"] = ""  # herdr says it no longer exists
        code, lanes = self.run_cli("close", "r", "--lane", "done")
        self.assertEqual(lanes["done"]["workspace_closed"]["how"], "already gone")

    def test_missing_or_moved_pane_does_not_prove_the_workspace_gone(self):
        with lc.locked_state("r") as st:
            st["lanes"]["gone"]["phase"] = "published"
        os.environ["FAKE_WORKSPACES"] = "wA,wB,wC,wD"  # wC's pane closed, workspace still open
        code, lanes = self.run_cli("close", "r")
        self.assertNotIn("workspace_closed", lanes["gone"])
        self.assertNotIn("workspace_closed", lanes["moved"])
        for name, ws in (("gone", "wC"), ("moved", "wD")):
            code, lanes = self.run_cli("reopen", "r", name)
            self.assertNotEqual(code, 0, name)
            self.assertEqual(lanes[name]["workspace_id"], ws, name)
        self.assertEqual(self.closed_ids(), ["wA"])
        self.assertNotIn("workspace create", open(self.log).read())

    def test_misshaped_workspace_answer_is_unknown_not_foreign(self):
        os.environ["FAKE_PANES"] = "{}"  # pane gone, so ownership rests on `workspace get`
        with lc.locked_state("r") as st:
            st["lanes"]["gone"]["phase"] = "published"
        for raw in ('{"result": {}}', '{"result": {"workspace": {"workspace_id": "other"}}}'):
            os.environ["FAKE_WS_RAW"] = raw
            code, lanes = self.run_cli("close", "r", "--lane", "gone")
            self.assertNotIn("workspace_closed", lanes["gone"], raw)
            self.assertEqual(lc.workspace_owner(lanes["gone"]), "unknown", raw)

    def test_after_close_a_reused_id_is_never_treated_as_ours(self):
        code, lanes = self.run_cli("close", "r", "--lane", "done")
        self.assertIsNone(lanes["done"]["workspace_id"])
        self.assertEqual(lanes["done"]["workspace_closed"]["workspace_id"], "wA")
        # herdr hands wA to an unrelated workspace later: close must not touch it again
        code, lanes = self.run_cli("close", "r", "--lane", "done")
        self.assertEqual(self.closed_ids(), ["wA"])

    def test_close_is_idempotent_and_reopen_gives_a_fresh_workspace(self):
        self.run_cli("close", "r", "--lane", "done")
        self.run_cli("close", "r", "--lane", "done")
        self.assertEqual(self.closed_ids(), ["wA"])
        code, lanes = self.run_cli("reopen", "r", "done")
        self.assertEqual(code, 0)
        self.assertEqual((lanes["done"]["workspace_id"], lanes["done"]["pane_id"]), ("wNEW", "wNEW:p1"))
        self.assertNotIn("workspace_closed", lanes["done"])
        code, _ = self.run_cli("reopen", "r", "busy")
        self.assertNotEqual(code, 0)  # a running lane is never reopened


class ExecutorConfig(unittest.TestCase):
    def test_default_is_opus_and_sol_is_a_preset(self):
        config = lc.merge_config({})
        self.assertEqual(lc.resolve_executor(None, config)["model"], "claude-opus-5-5")
        self.assertEqual(lc.resolve_executor("sol", config)["provider"], "codex")
        with self.assertRaises(ValueError):
            lc.resolve_executor("gpt-7", config)

    def test_user_file_can_switch_default_and_add_presets_but_not_the_reviewer(self):
        config = lc.merge_config({"executor": "sol", "executors": {"fable": {"provider": "claude", "model": "claude-fable-5-1"}}})
        self.assertEqual(lc.resolve_executor(None, config)["name"], "sol")
        self.assertIn("opus", config["executors"])  # built-in presets survive a partial override
        self.assertEqual(config["executors"]["fable"]["effort"], "xhigh")  # omitted effort defaults
        cmd = lc.build_exec_cmd({**ExecutorCommand.BASE, "executor": lc.resolve_executor("fable", config)}, 1, "go", SCHEMA_FILE)
        self.assertEqual(cmd[cmd.index("--effort") + 1], "xhigh")
        with self.assertRaises(ValueError):
            lc.merge_config({"executors": {"bad": {"provider": "claude", "model": "m", "effort": "turbo"}}})
        with self.assertRaises(ValueError):
            lc.merge_config({"executors": {"bad": {"provider": "claude", "model": "m", "effort": ["high"]}}})
        with self.assertRaises(ValueError):
            lc.merge_config({"reviewer": {"provider": "claude", "model": "x"}})
        with self.assertRaises(ValueError):
            lc.merge_config({"executor": "missing"})


class ReviewDecision(unittest.TestCase):
    def test_only_exact_decision_lines_count(self):
        self.assertEqual(lc.parse_decision("例: DECISION: PASS 仅作说明\n...\nDECISION: `BLOCK`\n"), "BLOCK")
        self.assertEqual(lc.parse_decision("DECISION: BLOCK\n\n示例：DECISION: PASS\n"), "BLOCK")
        self.assertEqual(lc.parse_decision("...\nDECISION: PASS\n\n<oai-mem-citation>x</oai-mem-citation>\n"), "PASS")
        self.assertIsNone(lc.parse_decision("DECISION: PASS\nDECISION: BLOCK\n"))
        self.assertIsNone(lc.parse_decision("建议通过"))
        self.assertIsNone(lc.parse_decision("格式示例：\n```\nDECISION: PASS\n```\n"))
        self.assertEqual(lc.parse_decision("```text\nDECISION: PASS\n```\n正文\nDECISION: BLOCK\n"), "BLOCK")
        self.assertIsNone(lc.parse_decision("```text\n~~~\nDECISION: PASS\n~~~\n```\n"))  # mixed fences
        self.assertIsNone(lc.parse_decision("````\n```\nDECISION: PASS\n```\n````\n"))  # longer outer fence
        self.assertIsNone(lc.parse_decision("```text\n    ```\nDECISION: PASS\n```\n"))  # 4-space indent is content
        self.assertEqual(lc.parse_decision("   ```\nx\n   ```\nDECISION: PASS\n"), "PASS")  # ≤3 spaces is a fence


def own_pr(base="develop", owner="o", repo="r", cross=False, url="u"):
    return {"url": url, "number": 1, "baseRefName": base, "isCrossRepository": cross,
            "headRepositoryOwner": {"login": owner}, "headRepository": {"name": repo}}


class PullRequestReuse(unittest.TestCase):
    def test_reuses_own_open_pr_on_same_base_only(self):
        self.assertIsNone(lc.pick_pr([], "develop", "o/r"))
        pr = own_pr()
        self.assertEqual(lc.pick_pr([pr], "develop", "o/r"), pr)
        with self.assertRaises(ValueError):
            lc.pick_pr([own_pr(base="staging")], "develop", "o/r")

    def test_fork_pr_with_same_branch_name_is_never_reused(self):
        self.assertIsNone(lc.pick_pr([own_pr(owner="fork", cross=True)], "develop", "o/r"))
        self.assertIsNone(lc.pick_pr([own_pr(owner="fork", base="main")], "develop", "o/r"))

    def test_two_own_open_prs_are_ambiguous(self):
        with self.assertRaises(ValueError):
            lc.pick_pr([own_pr(url="a"), own_pr(url="b")], "develop", "o/r")


class ReviewRoundLimit(unittest.TestCase):
    def test_round_seven_non_pass_cannot_start_another_fix(self):
        lanes = {"a": lane(phase="reviewed", attempt=3, fixes=2, review={"decision": "BLOCK", "round": 7})}
        self.assertIn("HUMAN_CONFIRMATION_REQUIRED", lc.start_violation(lanes, "a", "fix"))


class GithubRemote(unittest.TestCase):
    def test_pr_repo_comes_from_github_origin(self):
        self.assertEqual(lc.github_slug("git@github.com:acme/my-service.git"), "acme/my-service")
        self.assertEqual(lc.github_slug("https://github.com/o/r"), "o/r")
        with self.assertRaises(ValueError):
            lc.github_slug("/tmp/bare.git")


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True).stdout.strip()


class SetExecutorFixture:  # mixed into GateFixture below (needs its repo/run scaffolding)
    def test_switch_starts_a_fresh_session_and_is_refused_mid_attempt(self):
        sol = lc.resolve_executor("sol", lc.merge_config({}))
        self.save(phase="reviewed", executor=sol, session_id="codex-thread")
        code, _, state = self.lanectl("set-executor", "r", "a", "opus")
        self.assertEqual((code, state["executor"]["name"], state["session_id"]), (0, "opus", None))
        self.save(phase="running", executor=sol)
        code, _, state = self.lanectl("set-executor", "r", "a", "opus")
        self.assertNotEqual(code, 0)
        self.assertEqual(state["executor"]["name"], "sol")


class GateFixture(unittest.TestCase):
    """A real repo on a lane branch plus a run directory, with lanectl pointed at it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.repo = os.path.join(root, "repo")
        os.makedirs(self.repo)
        git(self.repo, "init", "-q", "-b", "develop")
        git(self.repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "base")
        self.base = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "switch", "-q", "-c", "feature/x")
        git(self.repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "work")
        self.head = git(self.repo, "rev-parse", "HEAD")
        self.old_runs, lc.RUNS_DIR = lc.RUNS_DIR, os.path.join(root, "runs")
        self.lane_dir = os.path.join(lc.RUNS_DIR, "r", "lanes", "a")
        os.makedirs(self.lane_dir)
        open(os.path.join(self.lane_dir, "progress.md"), "w").close()  # `new` creates it for real lanes
        self.lane = {"name": "a", "repo": self.repo, "kind": "feature", "kind_rule": DEVELOP,
                     "branch": "feature/x", "base_ref": "develop",
                     "base_sha": self.base, "pr_base": "develop", "has_origin": False, "worktree": self.repo,
                     "lane_dir": self.lane_dir, "publish": "pr", "phase": "judging", "attempt": 1, "sandbox": "safe",
                     "continues": 0, "fixes": 0, "objective": "o", "history": [],
                     "executor": lc.resolve_executor(None, lc.merge_config({})),
                     "reviewer": lc.DEFAULT_CONFIG["reviewer"]}

    def tearDown(self):
        lc.RUNS_DIR = self.old_runs
        self.tmp.cleanup()

    def save(self, **over):
        self.lane.update(over)
        lc.write_json(os.path.join(lc.RUNS_DIR, "r", "state.json"),
                      {"run_id": "r", "orchestrator": {"pane": None}, "lanes": {"a": self.lane}})

    def lanectl(self, *argv):
        out, code = io.StringIO(), 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                lc.main(list(argv))
            except SystemExit as exc:
                code = exc.code
        state = lc.read_json(os.path.join(lc.RUNS_DIR, "r", "state.json"))["lanes"]["a"]
        return code, out.getvalue(), state


class SetExecutor(SetExecutorFixture, GateFixture):
    pass


class Abandon(GateFixture):
    def test_only_a_quiet_lane_is_abandoned_and_it_then_counts_as_finished(self):
        self.save(phase="running")
        code, _, state = self.lanectl("abandon", "r", "a", "--reason", "user: merged into other-repo#42")
        self.assertNotEqual(code, 0)
        self.assertEqual(state["phase"], "running")
        self.save(phase="escalated")
        code, _, state = self.lanectl("abandon", "r", "a", "--reason", "user: merged into other-repo#42")
        self.assertEqual((code, state["phase"], state["abandoned"]["from"]), (0, "abandoned", "escalated"))
        code, out, _ = self.lanectl("runs")
        self.assertEqual(json.loads(out)[0]["open"], [])
        code, _, _ = self.lanectl("abandon", "r", "a", "--reason", "again")
        self.assertNotEqual(code, 0)
        code, _, state = self.lanectl("escalate", "r", "a", "--reason", "reopen?")
        self.assertEqual((code != 0, state["phase"]), (True, "abandoned"))  # no way back through escalate
        self.assertIn("start accepts nothing", lc.start_violation({"a": state}, "a", "answer", "x"))

    def test_a_stale_launch_is_withdrawn_so_a_late_runner_cannot_start(self):
        meta = os.path.join(self.lane_dir, "attempt-2.json")
        lc.write_json(meta, {"prompt": "p", "launch": "L2"})
        self.save(phase="judging", launching={"attempt": 2, "launch": "L2", "at_epoch": 0})  # expired, unconfirmed
        code, _, state = self.lanectl("abandon", "r", "a", "--reason", "user dropped it")
        self.assertEqual((code, state["phase"]), (0, "abandoned"))
        self.assertNotIn("launching", state)
        self.assertFalse(os.path.exists(meta))  # renamed: `run --launch L2` now aborts, `start` has nothing to adopt


class VerifyGates(GateFixture):
    def test_acceptance_is_not_run_when_preconditions_fail(self):
        git(self.repo, "switch", "-q", "develop")
        self.save(acceptance=[{"cmd": "touch ran.txt"}])
        _, _, state = self.lanectl("verify", "r", "a")
        self.assertFalse(state["verify"]["passed"])
        self.assertFalse(os.path.exists(os.path.join(self.repo, "ran.txt")))

    def test_acceptance_that_moves_head_or_writes_fails(self):
        for cmd in ("git -c user.email=t@t -c user.name=t commit -q --allow-empty -m sneaky", "touch stray.txt"):
            self.save(phase="judging", acceptance=[{"cmd": cmd}])
            _, _, state = self.lanectl("verify", "r", "a")
            self.assertFalse(state["verify"]["passed"], cmd)
            self.assertEqual(state["verify"]["head"], self.head)
            git(self.repo, "reset", "-q", "--hard", self.head)
            git(self.repo, "clean", "-qfd")

    def test_clean_lane_passes_and_records_head(self):
        self.save(acceptance=[{"cmd": "true"}])
        _, _, state = self.lanectl("verify", "r", "a")
        self.assertTrue(state["verify"]["passed"])
        self.assertEqual((state["phase"], state["verify"]["head"]), ("verified", self.head))


class ReviewRetry(GateFixture):
    def run_review(self, report, keep_state=False):
        bindir = os.path.join(self.tmp.name, "bin")
        os.makedirs(bindir, exist_ok=True)
        fake = os.path.join(bindir, "codex")  # writes the -o report like codex exec does (None: writes nothing)
        write = "" if report is None else "open(a[a.index('-o')+1],'w').write(%r)\n" % report
        open(fake, "w").write("#!/usr/bin/env python3\nimport sys\na=sys.argv\n" + write)
        os.chmod(fake, 0o755)
        old = os.environ["PATH"]
        os.environ["PATH"] = f"{bindir}:{old}"
        try:
            if not keep_state:
                self.save(phase="verified", verify={"head": self.head})
            return self.lanectl("review", "r", "a")
        finally:
            os.environ["PATH"] = old

    def test_report_without_decision_does_not_consume_a_round(self):
        code, _, state = self.run_review("looks fine")
        self.assertEqual((code, state["phase"]), (1, "verified"))
        self.assertNotIn("review", state)

    def test_report_left_by_an_earlier_attempt_is_never_reused(self):
        open(os.path.join(self.lane_dir, "review-1.md"), "w").write("old\nDECISION: PASS\n")
        code, _, state = self.run_review(None)
        self.assertEqual((code, state["phase"]), (1, "verified"))
        self.assertNotIn("review", state)

    def test_review_context_reaches_the_prompt_and_the_record(self):
        dump = os.path.join(self.tmp.name, "prompt.txt")
        bindir = os.path.join(self.tmp.name, "bin")
        os.makedirs(bindir, exist_ok=True)
        fake = os.path.join(bindir, "codex")
        open(fake, "w").write("#!/usr/bin/env python3\nimport sys\na=sys.argv\n"
                              f"open({dump!r},'w').write(a[-1])\n"
                              "open(a[a.index('-o')+1],'w').write('ok\\nDECISION: PASS\\n')\n")
        os.chmod(fake, 0o755)
        old = os.environ["PATH"]
        os.environ["PATH"] = f"{bindir}:{old}"
        try:
            self.save(phase="verified", verify={"head": self.head})
            code, _, state = self.lanectl("review", "r", "a", "--context", "pro lane seeds dict.language fi")
        finally:
            os.environ["PATH"] = old
        self.assertEqual(code, 0)
        prompt = open(dump).read()
        self.assertIn("pro lane seeds dict.language fi", prompt)
        self.assertIn("请自行核实", prompt)
        self.assertEqual(state["review"]["context"], "pro lane seeds dict.language fi")

    def test_valid_decision_records_round_and_head(self):
        code, _, state = self.run_review("report\nDECISION: PASS\n")
        self.assertEqual((code, state["phase"]), (0, "reviewed"))
        self.assertEqual((state["review"]["round"], state["review"]["head"]), (1, self.head))

    def ps_says(self, stdout, rc=0, stderr="", pid_exists=True):
        """Patch only `ps` (codex fakes still run for real) and os.kill's existence answer."""
        real_run = subprocess.run

        def run(args, *a, **kw):
            if args and args[0] == "ps":
                return subprocess.CompletedProcess(args, rc, stdout, stderr)
            return real_run(args, *a, **kw)

        def kill(pid, sig):
            if not pid_exists:
                raise ProcessLookupError(pid)

        return mock.patch.multiple(lc, subprocess=mock.Mock(run=run, CompletedProcess=subprocess.CompletedProcess,
                                                            DEVNULL=subprocess.DEVNULL, PIPE=subprocess.PIPE,
                                                            STDOUT=subprocess.STDOUT, Popen=subprocess.Popen),
                                   os=mock.Mock(wraps=os, kill=kill, path=os.path, environ=os.environ))

    def test_a_live_concurrent_review_blocks_a_second_one(self):
        self.save(phase="reviewing", verify={"head": self.head},
                  review_pending={"round": 1, "pid": 4242, "token": "other", "pid_start": "Mon Sep 28 10:00:00 2026"})
        with self.ps_says("S    Mon Sep 28 10:00:00 2026\n"):
            code, _, state = self.run_review("must not run\nDECISION: PASS\n", keep_state=True)
        self.assertNotEqual(code, 0)
        self.assertEqual(state["review_pending"]["token"], "other")

    def test_owner_liveness_matrix(self):
        start = "Mon Sep 28 10:00:00 2026"
        owner = {"pid": 4242, "pid_start": start}
        cases = [  # (ps stdout, ps rc, pid exists per kill, expected alive)
            (f"S    {start}", 0, True, True),                   # same process
            ("Z    " + start, 0, True, False),                  # zombie: exited
            ("S    Tue Sep 29 09:00:00 2026", 0, True, False),  # PID reused by another process
            ("S    malformed", 0, True, True),                  # unparseable start time: unknown → alive
            ("Z    malformed", 0, True, True),                  # even a Z row needs a parseable time
            ("", 1, False, False),                              # no such process
            ("", 1, True, True),                                # ps silent but pid exists: unknown → alive
        ]
        for stdout, rc, exists, alive in cases:
            with self.ps_says(stdout, rc, pid_exists=exists):
                self.assertEqual(lc.owner_alive(owner), alive, (stdout, rc, exists))
        with mock.patch.object(lc.subprocess, "run", side_effect=PermissionError("ps blocked")):
            self.assertTrue(lc.owner_alive(owner))  # ps cannot run: unknown → alive

    def test_unknown_start_time_falls_back_to_pid_liveness(self):
        with self.ps_says("S    Mon Sep 28 10:00:00 2026"):
            self.assertTrue(lc.owner_alive({"pid": 4242, "pid_start": None}))
        with self.ps_says("", 1, pid_exists=False):
            self.assertFalse(lc.owner_alive({"pid": 4242, "pid_start": None}))

    def test_real_ps_sees_this_process_and_a_zombie(self):
        try:
            me = lc.process_identity(os.getpid())
        except lc.ProcessCheckUnavailable:
            self.skipTest("ps unavailable in this environment")
        self.assertTrue(lc.owner_alive({"pid": os.getpid(), "pid_start": me}))
        zombie = subprocess.Popen(["true"])
        time.sleep(0.3)  # exited, not yet reaped
        self.assertFalse(lc.owner_alive({"pid": zombie.pid, "pid_start": me}))
        zombie.wait()

    def test_release_does_not_undo_a_concurrent_escalation(self):
        self.save(phase="escalated", review_pending={"round": 1, "pid": 1, "token": "t"})
        lc.release_review("r", "a", "t")
        state = lc.read_json(os.path.join(lc.RUNS_DIR, "r", "state.json"))["lanes"]["a"]
        self.assertEqual(state["phase"], "escalated")
        self.assertNotIn("review_pending", state)

    def test_dead_owner_reservation_is_reclaimed(self):
        real_save = self.save
        self.save = lambda **o: real_save(**{**o, "phase": "reviewing",
                                             "review_pending": {"round": 1, "pid": 4242, "token": "gone",
                                                                "pid_start": "Mon Sep 28 10:00:00 2026"}})
        with self.ps_says("", 1, pid_exists=False):  # owner provably gone
            code, _, state = self.run_review("ok\nDECISION: PASS\n")
        self.assertEqual((code, state["phase"], state["review"]["decision"]), (0, "reviewed", "PASS"))

    def test_reviewed_lane_can_be_reverified_and_needs_a_new_review(self):
        self.save(phase="reviewed", acceptance=[{"cmd": "true"}], review={"round": 1, "decision": "PASS", "head": self.head})
        _, _, state = self.lanectl("verify", "r", "a")
        self.assertEqual(state["phase"], "verified")
        code, _, _ = self.lanectl("ready", "r", "a")
        self.assertNotEqual(code, 0)  # ready needs a review of this verification

    def test_published_lane_can_be_verified_again(self):
        self.save(phase="published", acceptance=[{"cmd": "true"}])
        _, _, state = self.lanectl("verify", "r", "a")
        self.assertEqual(state["phase"], "verified")


class PublishOrder(GateFixture):
    """gh/git calls are recorded so the order of remote side effects is asserted, not assumed."""

    def run_publish(self, open_prs):
        calls = []
        real_sh = lc.sh
        remote = {"gh pr list": json.dumps(open_prs), "gh pr create": "https://github.com/o/r/pull/9",
                  "remote get-url": "https://github.com/o/r.git", "rev-parse origin/": self.base}

        def fake_sh(args, cwd=None, check=True):
            calls.append(args)
            joined = " ".join(args)
            if args[0] == "gh" or any(k in joined for k in ("remote get-url", " fetch ", " push ", "merge-base", "rev-parse origin/")):
                text = next((v for k, v in remote.items() if k in joined), "")
                return subprocess.CompletedProcess(args, 0, text, "")
            return real_sh(args, cwd=cwd, check=check)

        lc.sh = fake_sh
        try:
            self.save(phase="ready", has_origin=True, verify={"head": self.head},
                      review={"head": self.head, "decision": "PASS", "round": 1, "at_epoch": 0})
            code, _, state = self.lanectl("publish", "r", "a")
        finally:
            lc.sh = real_sh
        return code, [" ".join(c) for c in calls], state

    def test_pr_body_older_than_latest_review_is_refused(self):
        body = os.path.join(self.lane_dir, "pr.md")
        open(body, "w").write("old body")
        os.utime(body, (1, 1))
        real_save = self.save
        self.save = lambda **o: real_save(**{**o, "review": {**o["review"], "at_epoch": 2}})
        code, calls, state = self.run_publish([])
        self.assertNotEqual(code, 0)
        self.assertFalse(any(" push " in c for c in calls))

    def test_wrong_base_pr_is_rejected_before_any_push(self):
        code, calls, state = self.run_publish([own_pr(base="main")])
        self.assertNotEqual(code, 0)
        self.assertFalse(any(" push " in c for c in calls))
        self.assertEqual(state["phase"], "ready")

    def test_push_then_create_with_the_reviewed_head_and_pr_body(self):
        code, calls, state = self.run_publish([])
        self.assertEqual(code, 0)
        order = [next(i for i, c in enumerate(calls) if key in c) for key in ("gh pr list", " push ", "gh pr create")]
        self.assertEqual(order, sorted(order))
        self.assertTrue(next(c for c in calls if " push " in c).endswith(f"{self.head}:refs/heads/feature/x"))
        create = next(c for c in calls if "gh pr create" in c)
        self.assertIn(f"--base develop --head feature/x --title feat: o --body-file {self.lane_dir}/pr.md", create)
        self.assertIn("## Acceptance (re-run by the orchestrator)", open(os.path.join(self.lane_dir, "pr.md")).read())
        self.assertEqual(state["published"]["head"], self.head)
        self.assertEqual(state["phase"], "published")


class AgentTypes(unittest.TestCase):
    BASE = {**ExecutorCommand.BASE, "sandbox": "safe"}

    def test_type_name_resolves_to_its_default_preset(self):
        config = lc.merge_config({})
        self.assertEqual(lc.resolve_executor("codex", config)["name"], "sol")
        self.assertEqual(lc.resolve_executor("claude", config)["name"], "opus")
        self.assertEqual(lc.resolve_executor("omp", config)["provider"], "omp")
        mine = lc.merge_config({"executors": {"sonnet": {"provider": "claude", "model": "claude-sonnet-5-5"}},
                                "defaults": {"claude": "sonnet"}})
        self.assertEqual(lc.resolve_executor("claude", mine)["name"], "sonnet")
        with self.assertRaises(ValueError):
            lc.merge_config({"defaults": {"claude": "sol"}})  # a default must be a preset of that type

    def test_mode_defaults_per_type_and_codex_defaults_to_headless(self):
        config = lc.merge_config({"executors": {"batch": {"provider": "claude", "model": "m", "mode": "headless"},
                                                "sol-tui": {"provider": "codex", "model": "m", "mode": "interactive"}}})
        self.assertEqual(config["executors"]["opus"]["mode"], "interactive")
        self.assertEqual(config["executors"]["sol"]["mode"], "headless")
        self.assertEqual(config["executors"]["batch"]["mode"], "headless")
        self.assertEqual(config["executors"]["sol-tui"]["mode"], "interactive")
        with self.assertRaises(ValueError):
            lc.merge_config({"executors": {"x": {"provider": "omp", "model": "m", "mode": "headless"}}})
        self.assertEqual(lc.executor_mode({"name": "opus", "provider": "claude"}), "headless")  # v0.3 lanes

    def test_safe_sandbox_is_refused_for_agents_without_one(self):
        omp = lc.resolve_executor("omp", lc.merge_config({}))
        self.assertIsNotNone(lc.sandbox_violation(omp, "safe"))
        self.assertIsNone(lc.sandbox_violation(omp, "yolo"))
        self.assertIsNone(lc.sandbox_violation(lc.resolve_executor("claude", lc.merge_config({})), "safe"))

    def test_interactive_claude_starts_then_resumes_its_session_sandboxed(self):
        lane = {**self.BASE, "executor": lc.resolve_executor("opus", lc.merge_config({}))}
        first = lc.build_interactive_cmd(lane, "Read p.md", "S", resume=False)
        self.assertEqual(first[:2], ["claude", "Read p.md"])
        self.assertEqual(first[first.index("--session-id") + 1], "S")
        self.assertNotIn("-p", first)
        self.assertEqual(first[first.index("--append-system-prompt") + 1], lc.LANE_RULES)
        self.assertEqual(first[first.index("--add-dir") + 1:], ["/runs/r/lanes/a", "/repo/.git"])
        again = lc.build_interactive_cmd(lane, "Read p.md", "S", resume=True)
        self.assertEqual(again[again.index("--resume") + 1], "S")
        self.assertNotIn("--session-id", again)

    def test_interactive_omp_keeps_one_session_dir_and_continues_it(self):
        lane = {**self.BASE, "sandbox": "yolo", "executor": lc.resolve_executor("omp", lc.merge_config({}))}
        self.assertEqual(lc.new_session(lane), "/runs/r/lanes/a/omp-sessions")
        cmd = lc.build_interactive_cmd(lane, "Read p.md", "/runs/r/lanes/a/omp-sessions", resume=True)
        self.assertEqual((cmd[0], cmd[-1]), ("omp", "Read p.md"))
        self.assertIn("--continue", cmd)
        self.assertEqual([cmd[i + 1] for i, a in enumerate(cmd) if a == "--add-dir"], ["/runs/r/lanes/a", "/repo/.git"])

    def test_pi_and_grok_resume_by_session_and_never_run_under_safe(self):
        config = lc.merge_config({})
        pi = {**self.BASE, "sandbox": "yolo", "executor": lc.resolve_executor("pi", config)}
        self.assertEqual(lc.build_interactive_cmd(pi, "p", "S", resume=True),
                         lc.build_interactive_cmd(pi, "p", "S", resume=False))  # --session-id creates or resumes
        grok = {**self.BASE, "sandbox": "yolo", "executor": lc.resolve_executor("grok", config)}
        first, again = (lc.build_interactive_cmd(grok, "p", "S", resume=r) for r in (False, True))
        self.assertEqual((first[first.index("--session-id") + 1], again[again.index("--resume") + 1]), ("S", "S"))
        self.assertEqual(first[first.index("--permission-mode") + 1], "bypassPermissions")
        for kind in ("pi", "grok"):
            self.assertIsNotNone(lc.sandbox_violation(lc.resolve_executor(kind, config), "safe"))

    def test_interactive_codex_resumes_its_thread_with_the_exec_sandbox(self):
        tui = {"name": "sol-tui", "provider": "codex", "model": "gpt-6-sol", "effort": "xhigh", "mode": "interactive"}
        lane = {**self.BASE, "executor": tui}
        self.assertIsNone(lc.new_session(lane))  # codex names the thread; the runner learns it from herdr
        first = lc.build_interactive_cmd(lane, "Read p.md", None, resume=False)
        self.assertEqual((first[:2], first[-1]), (["codex", "-m"], "Read p.md"))
        self.assertNotIn("exec", first)
        sandbox = lc.codex_access(lane)
        self.assertIn('approval_policy="never"', sandbox)
        self.assertEqual(first[5:-1], sandbox)  # after -m M -c effort: the same sandbox/approvals as codex exec
        self.assertEqual(lc.build_interactive_cmd(lane, "p", "T1", resume=True)[:3], ["codex", "resume", "T1"])
        yolo = lc.build_interactive_cmd({**lane, "sandbox": "yolo"}, "p", None, resume=False)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", yolo)
        self.assertNotIn('sandbox_mode="workspace-write"', yolo)

    def test_learned_session_must_belong_to_the_lane_agent(self):
        def info(agent):
            return {"result": {"agent": {"agent_session": {"agent": agent, "kind": "id", "value": "T1"}}}}
        with mock.patch.object(lc, "herdr_json", lambda args: info("codex")):
            self.assertEqual(lc.agent_session("p", "codex"), "T1")
        with mock.patch.object(lc, "herdr_json", lambda args: info("omp")):  # left on the pane by an earlier agent
            self.assertIsNone(lc.agent_session("p", "codex"))


class ResultSchema(unittest.TestCase):
    GOOD = {"status": "done", "summary": "s", "checklist": [{"item": "a", "done": True}],
            "checks": [{"cmd": "make test", "exit": 0}], "commits": ["abc feat: x"], "blockers": [], "deviations": []}

    def errors(self, **over):
        return lc.schema_errors({**self.GOOD, **over}, json.load(open(SCHEMA_FILE)))

    def test_valid_result_passes_and_each_violation_is_named(self):
        self.assertEqual(self.errors(), [])
        self.assertIn("$.status: must be one of", self.errors(status="finished")[0])
        self.assertEqual(self.errors(checks=[{"cmd": "x", "exit": True}]), ["$.checks[0].exit: expected integer"])
        self.assertEqual(self.errors(extra=1), ["$.extra: not allowed"])
        bad = dict(self.GOOD)
        del bad["commits"]
        self.assertEqual(lc.schema_errors(bad, json.load(open(SCHEMA_FILE))), ["$.commits: missing"])


class InteractiveRun(unittest.TestCase):
    """The in-pane runner: turn-end detection, attention, and next-attempt pickup — herdr faked per call."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = lc.RUNS_DIR
        lc.RUNS_DIR = os.path.join(self.tmp.name, "runs")
        self.lane_dir = os.path.join(lc.RUNS_DIR, "r", "lanes", "a")
        os.makedirs(self.lane_dir)
        self.lane = {"name": "a", "phase": "running", "attempt": 1, "lane_dir": self.lane_dir, "pane_id": "w9:p1",
                     "repo": "/repo", "worktree": self.tmp.name, "session_id": None, "sandbox": "safe",
                     "git_common_dir": "/g", "executor": lc.resolve_executor("opus", lc.merge_config({}))}
        self.save()
        self.statuses: list = []
        self.sent: list = []
        patches = [mock.patch.object(lc, "agent_status", lambda pane: self.statuses.pop(0) if self.statuses else None),
                   mock.patch.object(lc, "agent_screen", lambda pane: "SCREEN"),
                   mock.patch.object(lc, "ring", lambda *a: None),
                   mock.patch.object(lc, "sh", self.fake_sh),
                   mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w9:p1"})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.runner = lc.InteractiveRunner("r", self.lane, 1, {"prompt": "Execute TASK.md", "launch": "L1"})

    def tearDown(self):
        lc.RUNS_DIR = self.old
        self.tmp.cleanup()

    def fake_sh(self, args, cwd=None, check=True):
        self.sent.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    def save(self, **over):
        self.lane.update(over)
        lc.write_json(os.path.join(lc.RUNS_DIR, "r", "state.json"),
                      {"run_id": "r", "orchestrator": {"pane": None}, "lanes": {"a": self.lane}})

    def ticks(self, *statuses):
        self.statuses = list(statuses)
        for _ in statuses:
            self.runner.tick()

    def marker(self, n=1):
        path = os.path.join(self.lane_dir, f"attempt-{n}.exit")
        return lc.read_json(path) if os.path.exists(path) else None

    def test_idle_before_any_work_is_not_the_end_of_the_turn(self):
        self.ticks("idle", "idle", "idle")
        self.assertIsNone(self.marker())

    def test_turn_that_stops_without_report_ends_with_the_screen(self):
        self.ticks("working", "idle")
        self.assertIsNone(self.marker())  # one idle tick can be a pause between tool calls
        self.ticks("done")
        self.assertEqual((self.marker()["via"], self.marker()["screen"]), ("stopped", "SCREEN"))

    def test_reported_turn_ends_as_report(self):
        lc.write_json(os.path.join(self.lane_dir, "result-1.json"), ResultSchema.GOOD)
        self.ticks("working", "idle", "idle")
        self.assertEqual(self.marker()["via"], "report")
        self.assertNotIn("screen", self.marker())

    def test_blocked_raises_one_attention_and_keeps_the_attempt_open(self):
        self.ticks("working", "blocked", "blocked")
        first = lc.read_json(os.path.join(self.lane_dir, "attention.json"))
        self.ticks("blocked")
        self.assertEqual(lc.read_json(os.path.join(self.lane_dir, "attention.json"))["seq"], first["seq"])
        self.assertIsNone(self.marker())
        self.ticks("working", "blocked")  # a new blocked episode after work resumed is a new event
        self.assertNotEqual(lc.read_json(os.path.join(self.lane_dir, "attention.json"))["seq"], first["seq"])

    def test_next_attempt_is_picked_up_only_with_the_reserved_token_then_prompted(self):
        self.ticks("working", "idle", "idle")
        lc.write_json(os.path.join(self.lane_dir, "attempt-2.json"), {"prompt": "Fix X", "launch": "L2"})
        self.save(phase="judging", launching={"attempt": 2, "launch": "OTHER", "at_epoch": time.time()})
        self.runner.tick()
        self.assertFalse(os.path.exists(os.path.join(self.lane_dir, "attempt-2.started")))
        self.save(launching={"attempt": 2, "launch": "L2", "at_epoch": time.time()})
        self.runner.tick()  # confirm
        self.assertEqual(lc.read_json(os.path.join(self.lane_dir, "attempt-2.started"))["launch"], "L2")
        self.runner.tick()  # deliver
        prompt = next(a for a in self.sent if a[:3] == ["herdr", "agent", "prompt"])
        self.assertEqual(prompt[3], "w9:p1")
        self.assertIn("attempt-2.prompt.md", prompt[4])
        brief = open(os.path.join(self.lane_dir, "attempt-2.prompt.md")).read()
        self.assertIn("Fix X", brief)
        self.assertIn("report r a --attempt 2", brief)
        self.ticks("working", "idle", "idle")
        self.assertEqual(self.marker(2)["via"], "stopped")

    def test_report_validates_and_only_for_the_running_attempt(self):
        good, bad = os.path.join(self.tmp.name, "good.json"), os.path.join(self.tmp.name, "bad.json")
        lc.write_json(good, ResultSchema.GOOD)
        lc.write_json(bad, {**ResultSchema.GOOD, "status": "finished"})
        codes = []
        for argv in (["report", "r", "a", "--attempt", "1", "--file", bad],
                     ["report", "r", "a", "--attempt", "2", "--file", good],
                     ["report", "r", "a", "--attempt", "1", "--file", good]):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                try:
                    lc.main(argv)
                    codes.append(0)
                except SystemExit as exc:
                    codes.append(exc.code)
        self.assertEqual([c != 0 for c in codes], [True, True, False])
        self.assertEqual(lc.read_json(os.path.join(self.lane_dir, "result-1.json")), ResultSchema.GOOD)

    def test_wait_reports_attention_once_without_leaving_running(self):
        lc.write_json(os.path.join(self.lane_dir, "attention.json"),
                      {"attempt": 1, "seq": 7, "status": "blocked", "screen": "Allow Bash?"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            lc.main(["wait", "r"])
        event = json.loads(out.getvalue())["events"][0]
        self.assertEqual((event["attention"], event["screen_tail"]), ("blocked", "Allow Bash?"))
        state = lc.read_json(os.path.join(lc.RUNS_DIR, "r", "state.json"))["lanes"]["a"]
        self.assertEqual((state["phase"], state["attention_seen"]), ("running", 7))
        self.assertIsNone(lc.new_attention(state))

    def test_trust_dialog_is_answered_only_for_a_repo_the_user_already_trusts(self):
        trust = "Quick safety check ...\n ❯ No, exit\n   Yes, I trust this folder"
        keys = lambda: sum(a[:3] == ["herdr", "pane", "send-keys"] for a in self.sent)  # noqa: E731
        with mock.patch.object(lc, "agent_screen", lambda pane: trust), \
                mock.patch.object(lc, "claude_trusts", lambda path: path == "/repo"):
            self.ticks("blocked")
            self.assertIn(["herdr", "pane", "send-keys", "w9:p1", "down", "enter"], self.sent)
            self.assertFalse(os.path.exists(os.path.join(self.lane_dir, "attention.json")))
            self.ticks("working", "blocked")  # asked again: never answered twice, the user decides
            self.assertEqual(keys(), 1)
            self.assertTrue(os.path.exists(os.path.join(self.lane_dir, "attention.json")))
            self.lane["repo"] = "/untrusted"
            runner = lc.InteractiveRunner("r", self.lane, 1, {"prompt": "p", "launch": "L1"})
            self.statuses = ["blocked"]
            runner.tick()
        self.assertEqual(keys(), 1)

    def test_agent_that_never_starts_working_ends_the_attempt_with_its_screen(self):
        self.ticks("idle")
        self.assertIsNone(self.marker())
        self.runner.prompted_at -= lc.START_GRACE + 1  # e.g. stuck on a login screen
        self.ticks("idle")
        self.assertEqual((self.marker()["via"], self.marker()["screen"]), ("never_started", "SCREEN"))
        self.assertTrue(self.runner.stop)  # the TUI is torn down: a late start cannot meet the next prompt

    def test_wait_ends_an_attempt_whose_runner_vanished_and_stops_its_orphan(self):
        started = os.path.join(self.lane_dir, "attempt-1.started")
        lc.write_json(started, {**lc.identity(os.getpid()), "launch": "L1"})
        orphan = subprocess.Popen(["sleep", "30"])
        self.addCleanup(orphan.kill)
        lc.write_json(os.path.join(self.lane_dir, "executor.json"), {**lc.identity(orphan.pid), "tty": None})
        lc.reap_lost(self.lane)
        self.assertIsNone(self.marker())  # a live runner is never reaped
        self.assertIn("still runs", lc.stop_orphan(self.lane_dir, kill=False))  # `start` refuses to type into it
        dead = subprocess.Popen(["true"])
        dead.wait()
        lc.write_json(started, {"pid": dead.pid, "pid_start": None, "launch": "L1"})  # e.g. SIGKILLed
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            lc.main(["wait", "r"])
        event = json.loads(out.getvalue())["events"][0]
        self.assertEqual((event["via"], event["exit"], event["screen_tail"]), ("lost", -1, "SCREEN"))
        self.assertNotIn("orphan", event)  # stopped, so nothing is left running
        self.assertEqual(orphan.wait(timeout=10), -signal.SIGTERM)
        self.assertFalse(os.path.exists(os.path.join(self.lane_dir, "executor.json")))
        self.assertEqual(lc.read_json(os.path.join(lc.RUNS_DIR, "r", "state.json"))["lanes"]["a"]["phase"], "judging")

    def test_first_attempt_lost_keeps_the_session_the_runner_recorded(self):
        lc.write_json(os.path.join(self.lane_dir, "agent.json"),
                      {"pid": 1, "pid_start": None, "executor": self.lane["executor"], "session": "S1"})
        dead = subprocess.Popen(["true"])
        dead.wait()
        lc.write_json(os.path.join(self.lane_dir, "attempt-1.started"), {"pid": dead.pid, "pid_start": None})
        with contextlib.redirect_stdout(io.StringIO()):
            lc.main(["wait", "r"])
        self.assertEqual(lc.read_json(os.path.join(lc.RUNS_DIR, "r", "state.json"))["lanes"]["a"]["session_id"], "S1")

    def test_codex_thread_learned_from_herdr_is_persisted_for_a_hard_kill(self):
        tui = {"name": "sol-tui", "provider": "codex", "model": "m", "effort": "low", "mode": "interactive"}
        runner = lc.InteractiveRunner("r", {**self.lane, "executor": tui}, 1, {"prompt": "p", "launch": "L1"})
        self.assertIsNone(runner.session)
        with mock.patch.object(lc, "agent_session", lambda pane, agent: "T1" if agent == "codex" else None):
            self.statuses = ["working"]
            runner.tick()
        self.assertEqual(lc.read_json(os.path.join(self.lane_dir, "agent.json"))["session"], "T1")

    def test_orphan_that_already_exited_still_gets_its_terminal_repaired(self):
        dead = subprocess.Popen(["true"])
        dead.wait()
        lc.write_json(os.path.join(self.lane_dir, "executor.json"), {**lc.identity(os.getpid()), "pid": dead.pid,
                                                                     "tty": "/dev/ttysX"})
        resets = []
        with mock.patch.object(lc, "reset_terminal", resets.append):
            self.assertIsNone(lc.stop_orphan(self.lane_dir))
        self.assertEqual(resets, ["/dev/ttysX"])
        self.assertFalse(os.path.exists(os.path.join(self.lane_dir, "executor.json")))


class HeadlessSignals(unittest.TestCase):
    def test_pane_hangup_still_marks_the_attempt_and_stops_codex(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        bindir, home = os.path.join(tmp.name, "bin"), os.path.join(tmp.name, "home")
        lane_dir, child_pid = os.path.join(home, "runs", "r", "lanes", "a"), os.path.join(tmp.name, "codex.pid")
        os.makedirs(bindir)
        os.makedirs(lane_dir)
        with open(os.path.join(bindir, "codex"), "w") as fh:
            fh.write(f'#!/bin/sh\necho $$ > {child_pid}\necho "session id: T1"\nexec sleep 30\n')
        os.chmod(os.path.join(bindir, "codex"), 0o755)
        lc.write_json(os.path.join(home, "runs", "r", "state.json"),
                      {"run_id": "r", "orchestrator": {"pane": None}, "lanes": {}})
        lane_ = {"name": "a", "lane_dir": lane_dir, "worktree": tmp.name, "sandbox": "yolo", "git_common_dir": "/g",
                 "executor": {"provider": "codex", "model": "m", "effort": "low"}}
        code = f"import lanectl; lanectl.run_headless('r', {lane_!r}, 1, {{'prompt': 'x'}})"
        proc = subprocess.Popen([sys.executable, "-c", code], cwd=os.path.dirname(__file__),
                                env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "LANE_DISPATCH_HOME": home},
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log = os.path.join(lane_dir, "attempt-1.log")
        deadline = time.time() + 10
        while time.time() < deadline and not (os.path.exists(log) and "session id" in open(log).read()):
            time.sleep(0.1)
        proc.send_signal(signal.SIGHUP)
        proc.wait(timeout=20)
        marker = lc.read_json(os.path.join(lane_dir, "attempt-1.exit"))
        self.assertEqual((marker["exit"], marker["session_id"]), (-1, "T1"))
        with self.assertRaises(ProcessLookupError):
            os.kill(int(open(child_pid).read()), 0)  # the executor did not outlive its runner


class StartIntoLiveAgent(LaunchConfirmation):
    """With the lane's agent TUI alive, `start` must hand the attempt to its runner, never type into the pane."""

    def test_live_runner_confirms_and_no_shell_command_is_sent(self):
        log = os.path.join(self.tmp.name, "herdr.log")
        fake = os.path.join(self.tmp.name, "bin", "herdr")
        open(fake, "w").write(f'#!/bin/sh\necho "$@" >> {log}\n')
        lc.write_json(os.path.join(self.lane_dir, "agent.json"), {"pid": 4242, "pid_start": "S", "attempt": 0})
        alive = mock.patch.object(lc, "owner_alive", lambda info: info.get("pid") == 4242)  # no host `ps` needed
        alive.start()
        self.addCleanup(alive.stop)

        def runner():  # plays the live runner's pickup: confirm whatever start reserved
            meta = os.path.join(self.lane_dir, "attempt-1.json")
            for _ in range(40):
                if os.path.exists(meta):
                    lc.write_json(os.path.join(self.lane_dir, "attempt-1.started"),
                                  {"launch": lc.read_json(meta)["launch"]})
                    return
                time.sleep(0.05)

        t = threading.Thread(target=runner)
        t.start()
        code, lane = self.start()
        t.join()
        self.assertEqual((code, lane["phase"]), (0, "running"))
        self.assertFalse(os.path.exists(log) and open(log).read().strip())

    def test_agent_that_outlived_its_runner_blocks_a_new_launch(self):
        log = os.path.join(self.tmp.name, "herdr.log")
        fake = os.path.join(self.tmp.name, "bin", "herdr")
        open(fake, "w").write(  # no agent.json (runner gone), but herdr still sees a claude TUI in the pane
            f'#!/bin/sh\necho "$@" >> {log}\n'
            '[ "$1 $2" = "agent get" ] && echo \'{"result": {"agent": {"agent_status": "idle"}}}\'\nexit 0\n')
        began = time.time()
        code, lane = self.start()
        self.assertNotEqual(code, 0)
        self.assertEqual((lane["phase"], lane["attempt"]), ("planned", 0))
        self.assertNotIn("pane run", open(log).read())
        self.assertLess(time.time() - began, lc.START_CONFIRM_SECONDS)  # refused at once, nothing to wait for

    def test_unknown_pane_state_is_not_taken_for_a_bare_shell(self):
        log = os.path.join(self.tmp.name, "herdr.log")
        fake = os.path.join(self.tmp.name, "bin", "herdr")
        open(fake, "w").write(f'#!/bin/sh\necho "$@" >> {log}\n[ "$1 $2" = "agent get" ] && echo boom >&2 && exit 1\nexit 0\n')
        code, lane = self.start()
        self.assertNotEqual(code, 0)
        self.assertEqual((lane["phase"], lane["attempt"]), ("planned", 0))
        self.assertNotIn("pane run", open(log).read())


if __name__ == "__main__":
    unittest.main()
