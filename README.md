# lane-dispatch

## 1. 项目简介

lane-dispatch 是一个让 AI 编排者（omp 或 Claude Code）把编码任务拆成多条 **lane** 并行执行的 skill。每条 lane 是「一个分支 + 一个 git worktree + 一个 [herdr](https://herdr.dev) workspace」，由一个执行者 agent 在 pane 里实现；编排者只负责拆分、写计划、验收、review 和发布，不亲自写 lane 的代码。

本项目受 [bestony/herdr-dispatch](https://github.com/bestony/herdr-dispatch)（MIT）启发：保留了「编排者规划 + lane 执行 + 编排者发布」的分工，把巡检循环换成磁盘事件，并把状态机、上限和门禁写进一个带测试的 Python 脚本。lane brief 的部分措辞改编自它，许可声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

当前版本 v0.5 的架构：

- **编排者**：omp 或 Claude Code（`claude-fable-5-1`，退回 `claude-opus-5-5`），在 herdr pane 中运行，按 `skills/lane-dispatch/SKILL.md` 的流程调用 `lanectl`。
- **执行者**：按类型选择 `claude` / `codex` / `omp` / `pi` / `grok`。默认 Claude Opus 5.5 的真实交互式 TUI；codex 默认 `codex exec` headless，也可用预设切换为 codex TUI。
- **事件驱动**：每次 attempt 结束都落一个磁盘标记 `attempt-N.exit`，`lanectl wait` 在后台阻塞等待（0 token），编排者只在事件发生时醒来，不做 LLM 轮询。
- **门禁**：lane 只能 commit；`verify`（分支 / clean / freshness / 验收命令）通过且 codex review 对同一个 HEAD 判定 `PASS`，lane 才能 `ready`。默认只保留在本地，任务头明确要求上传时才开 PR。

这个项目主要能做这些事情：

- 把一个需求拆成可并行、跨仓库的多条 lane，每条 lane 有独立 worktree 和 herdr workspace
- 一次确认计划与执行者后自动派发，最多 4 条同时运行
- 执行者卡住、需要授权、限流、崩溃、被硬杀时都能以磁盘事件回报
- 自动验收、codex review（最多 7 轮）、按配置的分支规则（kind）开 PR
- 编排者会话中断后，按磁盘状态续跑同一个 run

当前公开入口统一为：

```text
编排者里调用 skill：lane-dispatch（或固定执行者类型的 lane-dispatch-claude / -codex / -omp / -pi / -grok）
编排者内部调用的 CLI：python3 <skill 目录>/bin/lanectl.py <子命令>
```

以下不作为公开调用方式：

```text
lanectl run …            # 只由 start 在 lane pane 中启动，不要手动执行
```

## 2. 常用操作运行方式

### 安装

只依赖 Python 3 标准库。运行环境需要：`git`、`herdr`（已安装对应 agent 的 herdr 集成）、`codex`（review 必需）、所选执行者的 CLI；publish 需要 `gh`。

本仓库自身就是一个插件 marketplace（`.claude-plugin/marketplace.json`），Claude Code 和 omp 用同一份清单安装，装好后有 6 个 skill：`lane-dispatch` 和 `lane-dispatch-{claude,codex,omp,pi,grok}`。

#### Claude Code

```bash
claude plugin marketplace add Sandu1213/lane-dispatch
claude plugin install lane-dispatch@lane-dispatch
```

或在 Claude Code 会话里执行 `/plugin marketplace add Sandu1213/lane-dispatch`、`/plugin install lane-dispatch@lane-dispatch`，然后按提示 `/reload-plugins`。`claude plugin details lane-dispatch@lane-dispatch` 应列出 6 个 skill。

#### omp

```bash
omp plugin marketplace add Sandu1213/lane-dispatch
omp plugin install lane-dispatch@lane-dispatch
```

或在 omp 会话里执行 `/marketplace add Sandu1213/lane-dispatch`、`/marketplace install lane-dispatch@lane-dispatch`。`omp plugin list` 应显示 `lane-dispatch@lane-dispatch`。

#### 更新与卸载

```bash
claude plugin marketplace update lane-dispatch && claude plugin update lane-dispatch@lane-dispatch
omp plugin marketplace update lane-dispatch && omp plugin upgrade lane-dispatch@lane-dispatch

claude plugin uninstall lane-dispatch@lane-dispatch
omp plugin uninstall lane-dispatch@lane-dispatch
```

插件按版本缓存，正在运行的 lane 会引用当时版本的 `lanectl.py`。请在 `$L runs` 里没有未结束的 lane 时再更新。

#### 开发 / 软链接安装（可选）

要改源码并立即生效时，可以不用插件，直接在 clone 目录把 `skills/` 下的每个 skill 软链接进 skill 目录（omp 读 `~/.agents/skills`，Claude Code 读 `~/.claude/skills`）。不要和插件同时装，否则会出现两份同名 skill：

```bash
git clone https://github.com/Sandu1213/lane-dispatch.git && cd lane-dispatch
mkdir -p ~/.agents/skills ~/.claude/skills
for dir in skills/*/; do
  name=$(basename "$dir")
  ln -sfn "$PWD/skills/$name" ~/.agents/skills/"$name"
  ln -sfn ../../.agents/skills/"$name" ~/.claude/skills/"$name"
done
```

之后在 clone 目录 `git pull` 即可更新。

#### `$L`：lanectl 的位置

编排者会自己找到 `lanectl.py`；你手动查看配置或状态时，下文的 `$L` 指 `python3 <lane-dispatch skill 目录>/bin/lanectl.py`：

```bash
# Claude Code 插件
L="python3 $(ls -d ~/.claude/plugins/cache/lane-dispatch/lane-dispatch/*/skills/lane-dispatch | tail -n 1)/bin/lanectl.py"
# omp 插件
L="python3 $(ls -d ~/.omp/plugins/cache/plugins/lane-dispatch___lane-dispatch___*/skills/lane-dispatch | tail -n 1)/bin/lanectl.py"
# 软链接安装
L="python3 $HOME/.agents/skills/lane-dispatch/bin/lanectl.py"
```

运行状态默认放在 `~/.agents/lane-dispatch/`（与安装方式无关），设置 `LANE_DISPATCH_HOME` 可改位置。

### 启动编排者并派发任务

编排者必须在 herdr pane 里运行（`HERDR_ENV=1` 且有 `HERDR_PANE_ID`）：

```bash
omp --model claude-fable-5-1
# 或
claude-herdr --model claude-fable-5-1
```

然后直接说任务，例如：

```text
用 lane-dispatch 派发：my-service 和 my-web 支持西班牙语。上传 PR。
```

编排者会先列出 lane 表、执行者选择、每条 lane 的计划和验收命令，**只问一次**；确认后自动创建、启动、等待、验收、review，最后给出中文报告。任务头写了「上传 / 提 PR」才会开 PR。

### 固定执行者类型

```text
/lane-dispatch-codex   派发：……
/lane-dispatch-claude  派发：……
```

Claude Code 插件方式下 skill 带插件前缀：`/lane-dispatch:lane-dispatch-codex`。也可以不打命令，直接说「用 lane-dispatch 派发……」。

### 查看生效配置与可选执行者

```bash
$L config
```

输出中的 `types` 按类型列出 `presets`、`default`、`modes`、`safe_sandbox`、`installed`。

### 自定义执行者预设

写 `~/.agents/lane-dispatch/config.json`（与内置默认按名字合并）：

```json
{
  "executor": "opus",
  "executors": {
    "sol-tui": {"provider": "codex", "model": "gpt-6-sol", "effort": "xhigh", "mode": "interactive"}
  },
  "defaults": {"codex": "sol"}
}
```

### 配置分支规则（kind）

lane 的 `kind` 决定 base / PR 目标分支、分支前缀和 commit 类型。内置两种：`feature`（默认）和 `fix`，都基于 `main` 并向 `main` 提 PR。`config.json` 的 `"kinds"` 可以按名字覆盖或新增，例如默认分支是 `master`、或者有 `develop` 分支的仓库：

```json
{
  "kinds": {
    "feature": {"target": "develop", "prefix": "feature/", "commit": "feat"},
    "fix": {"target": "master", "prefix": "fix/", "commit": "fix"},
    "hotfix": {"target": "main", "prefix": "hotfix/", "commit": "fix", "confirm": true, "local_only": true}
  }
}
```

kind 可用字段：`target`、`prefix`、`commit`（必填），`confirm`（`new` 需要 `--confirm-kind`，即用户显式确认）、`local_only`（不允许开 PR）。加载配置时会校验分支名是否合法。

### 查看进度与续跑

```bash
$L runs          # 所有 run 及其未结束 lane
$L status <run>  # 每条 lane 的 phase / attempt / review / PR
```

编排者会话关掉后，新开一个编排者说「resume lane-dispatch run <run>」即可续跑。

### 运行测试

```bash
python3 -m unittest skills/lane-dispatch/bin/test_lanectl.py   # 在 clone 目录执行
```

## 3. 单个模块的实现方式

以下命令都由编排者调用，`$L` = `python3 <lane-dispatch skill 目录>/bin/lanectl.py`。全部实现都在 `bin/lanectl.py`；本节「核心代码」的路径都相对仓库里的 `skills/lane-dispatch/`。

### init / new：创建 run 与 lane

#### 流程

1. `init` 生成 run id，记录编排者 pane 作为门铃目标。
2. 编排者把每条 lane 的 spec 写到 `<run 目录>/specs/<lane>.json`（仓库、kind、slug、计划、清单、验收命令、执行者、沙箱、publish）。
3. `new` 按 kind 的规则决定 base（内置 `feature` → `origin/main`），记录完整 base SHA；需要确认的 kind 要带 `--confirm-kind`，`local_only` 的 kind 拒绝 `publish: pr`。
4. 在 `<仓库父目录>/.codex-worktrees/<run>/<repo>-<lane>` 创建 worktree，写 `TASK.md` / `progress.md`，跑 `setup`，创建 herdr workspace（pane `impl`）。

#### 调用示例

```bash
$L init --label i18n
$L new <run> --spec <run 目录>/specs/i18n-api.json
```

核心代码：

```text
bin/lanectl.py  cmd_init / cmd_new / kind_rule / expected_base / branch_name / render_brief
```

### start / run：启动执行者

#### 流程

1. `start` 校验 phase 允许的续跑原因和各项上限（并发 4、续跑 3、fix 6、单 lane 14 次 attempt），在锁内写 attempt 元数据和唯一 launch token。
2. pane 里已有存活的交互 runner 时，由它接走下一次 attempt；否则在 pane 中执行 `lanectl run`。孤儿执行者还活着时拒绝往 pane 输入命令。
3. runner 在同一把锁下确认 token 后启动执行者：交互模式运行 agent 自己的 TUI，headless 模式运行 `codex exec` 或 `claude -p`。
4. 15 秒内没确认就撤回元数据，lane 保持不变。

#### 调用示例

```bash
$L start <run> <lane>                                              # 首次
$L start <run> <lane> --reason continue --prompt "Continue from progress.md. Remaining: …"
$L start <run> <lane> --reason ratelimit --after 30 --prompt "Continue."
$L start <run> <lane> --reason answer --user-answer "<用户原话>" --prompt "…"   # escalated 之后
```

核心代码：

```text
bin/lanectl.py  cmd_start / start_violation / cmd_run / run_headless / InteractiveRunner
                build_exec_cmd / build_interactive_cmd / codex_access / claude_access
```

### wait：等待事件

#### 流程

1. 每 5 秒检查各 running lane 的 `attempt-N.exit` 和 `attention.json`。
2. 有 exit 标记：lane 转为 `judging`，返回结果 JSON、屏幕或日志尾部。
3. 有 attention（权限 / 信任弹窗）：只报告一次，lane 仍 `running`。
4. runner 已确定死亡但没有 exit 标记（SIGKILL、崩溃）：补写 `via: lost`，停掉孤儿执行者并修复 pane 终端。
5. 没有 waiter 在听时，runner 落盘后向编排者 pane 按「门铃」。

#### 调用示例

```bash
$L wait <run>      # 编排者以后台任务运行：omp bash async / Claude Code run_in_background
```

核心代码：

```text
bin/lanectl.py  cmd_wait / reap_lost / stop_orphan / lane_event / new_attention / ring
```

### report：交互执行者记录结果

#### 流程

1. runner 把完整提示写进 `attempt-N.prompt.md`，只向 TUI 发一行「Read … and carry out its instructions」。
2. 执行者结束回合前写 `result-draft-N.json` 并调用 `report`。
3. `report` 只接受当前 running attempt，按 `schema/lane-result.schema.json` 校验后写 `result-N.json`。

#### 调用示例

```bash
python3 <lanectl.py> report <run> <lane> --attempt 2 --file <lane 目录>/result-draft-2.json
```

核心代码：

```text
bin/lanectl.py  cmd_report / schema_errors / InteractiveRunner.pointer
schema/lane-result.schema.json
```

### verify：验收

#### 流程

1. 检查在 lane 分支、工作区 clean、有新 commit。
2. 落后 `origin/<target>` 时在 worktree 里 merge（冲突则 abort，交回 lane）。
3. 前置条件满足才逐条跑验收命令，核对退出码。
4. 验收命令跑完再查一次：HEAD 不变、分支不变、工作区 clean。记录 verified HEAD。

#### 调用示例

```bash
$L verify <run> <lane>
```

核心代码：

```text
bin/lanectl.py  cmd_verify / tree_state
```

### review：codex review

#### 流程

1. 在锁内原子预留轮次（phase `reviewing`），要求 worktree 仍是 verified HEAD。
2. 运行 `codex exec -s read-only` 审查 `<target>...HEAD`；codex 装有 `local_review` skill 时使用它，没有时提示词本身也要求按 must_fix / should_fix / suggestion / accepted 分级并给出判定行。
3. 只认代码块之外、整行为 `DECISION: PASS|BLOCK|UNVERIFIED` 的那一行；运行失败或没有判定时不消耗轮次。
4. 最多 7 轮，第 7 轮仍不通过 → `HUMAN_CONFIRMATION_REQUIRED`。

#### 调用示例

```bash
$L review <run> <lane>
$L review <run> <lane> --context "配套的接口改动在 other-repo 的 PR #N 中"
```

核心代码：

```text
bin/lanectl.py  cmd_review / parse_decision / release_review
```

### ready / publish：就绪与开 PR

#### 流程

1. `ready` 只接受 `PASS` 且 review HEAD == verified HEAD 的 lane，没有人工豁免。
2. `publish` 只对 `publish: pr` 的 lane 生效：PR 仓库由 GitHub `origin` 推导，重新校验 freshness，先查 PR 再 push（不 force），然后创建或更新 PR。

#### 调用示例

```bash
$L ready <run> <lane>
$L publish <run> <lane>     # 事先写好 <lane 目录>/pr.md，按目标仓库的 PR 模板
```

核心代码：

```text
bin/lanectl.py  cmd_ready / cmd_publish / pick_pr / default_pr_body / github_slug
```

### escalate / abandon / set-executor：异常处理

#### 流程

1. `escalate`：超出计划或达到上限，转给用户决定；之后只能用 `--reason answer --user-answer` 续跑。
2. `abandon`：用户结束 lane（例如改动并入别处），终态，不会变成 `ready`。
3. `set-executor`：attempt 之间换执行者，新 session 从 `TASK.md` / `progress.md` 开始。

#### 调用示例

```bash
$L escalate <run> <lane> --reason "<阻塞原因>"
$L abandon <run> <lane> --reason "<用户原话；改动去向>"
$L set-executor <run> <lane> codex
```

核心代码：

```text
bin/lanectl.py  cmd_escalate / cmd_abandon / cmd_set_executor
```

### close / reopen：收尾

#### 流程

1. 所有 lane 都是 `published` / `ready` / `failed` / `abandoned` 后，`close` 只关闭本 run 创建、并核对过 pane cwd 仍在该 lane worktree 内的 workspace。
2. worktree 不自动删除；删除 worktree 和合并 PR 的命令由编排者打印给用户执行。
3. lane 之后还要改（例如 PR 意见）时，`reopen` 在原 worktree 上重建 workspace。

#### 调用示例

```bash
$L close <run> [--lane <lane>]
$L reopen <run> <lane>
```

核心代码：

```text
bin/lanectl.py  cmd_close / cmd_reopen / workspace_owner / herdr_lookup
```

## 4. 核心实现说明总结

### 文件结构

```text
.claude-plugin/plugin.json       # Claude Code / omp 插件清单
.claude-plugin/marketplace.json  # 仓库自身即 marketplace（Claude Code 与 omp 通用）
Guide.md                         # 使用指南（以给 Flutter App 增加一种语言为例）
LICENSE                          # MIT
THIRD_PARTY_NOTICES.md           # 改编自 herdr-dispatch 的部分及其 MIT 声明
skills/lane-dispatch/SKILL.md    # 编排者遵循的流程、不变量、分支规则、事件处理表
skills/lane-dispatch/bin/lanectl.py       # 唯一实现（Python 标准库）
skills/lane-dispatch/bin/test_lanectl.py  # 单元 / 行为测试
skills/lane-dispatch/schema/lane-result.schema.json  # 执行者结果 JSON 的 schema
skills/lane-dispatch-<type>/SKILL.md      # 固定执行者类型的薄入口 skill

~/.agents/lane-dispatch/         # 运行状态（可用 LANE_DISPATCH_HOME 改位置）
  config.json                    # 可选：执行者预设
  runs/<run>/state.json          # 账本，flock 保护的读-改-写
  runs/<run>/lanes/<lane>/       # TASK.md、progress.md、attempt-N.*、result-N.json、review-N-*.md …
```

### 核心分层思想

```text
编排层：编排者 LLM 按 SKILL.md 拆 lane、写计划、处理事件、和用户沟通
控制层：lanectl 管状态机、上限、锁、门禁（verify / review / ready / publish）
执行层：pane 里的 runner + 执行者（交互 TUI 或 headless），只 commit，结果落盘
```

### 入口调用链

```text
编排者（SKILL.md）
  -> lanectl new        创建 worktree / workspace / TASK.md
  -> lanectl start      写 attempt 元数据 + launch token
       -> herdr pane run "lanectl run …"
            -> run_headless()       codex exec / claude -p，进程退出写 exit
            -> InteractiveRunner    agent TUI，herdr 回合状态 + report 写 exit
  -> lanectl wait       读磁盘事件，lane → judging
  -> lanectl verify -> review -> ready -> publish -> close
```

### 重点模块实现

#### 两段式启动（不重复启动、不往 TUI 里打字）

问题：`start` 发出启动命令后，runner 可能没起来、起来得晚，或者 pane 里其实还有一个 TUI。

实现原则：

1. 第一段在锁内写 attempt 元数据和唯一 launch token，lane 标记 `launching`。
2. 不持锁等待 runner 写 `attempt-N.started`（带同一 token），最多 15 秒。
3. 第二段在锁内：token 匹配才记 `running`，否则把元数据改名 withdrawn，迟到的 runner 看到后直接退出。
4. 有存活 runner 时由它接走；herdr 只有明确返回 `agent_not_found`，或孤儿执行者已确认死亡时，才往 pane 里输入命令。

#### 交互模式的回合检测

1. runner 每 2 秒读 `herdr agent get`：见过 `working` 后连续 2 次 `idle` / `done` 才算回合结束（有结果 `via: report`，无结果 `via: stopped`）。
2. `blocked` 写 `attention.json`，只请求介入，不结束 attempt。
3. 提示送达 180 秒仍未开始工作 → `via: never_started`（例如登录页）。
4. Claude 的信任弹窗只在用户已信任主 checkout 时代答一次；codex 的信任弹窗从不代答。

#### runner 硬杀恢复（v0.5）

问题：runner 被 SIGKILL 后没人写 exit 标记，`wait` 永远不返回；执行者成了孤儿，herdr 认不出它，被外部杀掉的 TUI 还会把 pane 终端留在 raw 模式和 kitty 键盘协议里，后续按键全部乱码。

实现原则：

1. `attempt-N.started` 记录 runner 的 pid + 启动时间；`executor.json` 记录执行者的 pid + 启动时间 + tty。
2. `wait` 只在 runner 确定死亡时（无法判断按存活处理）补写 `via: lost`。
3. 启动时间匹配才对孤儿发 SIGTERM，5 秒后 SIGKILL；随后 `stty sane` 并弹出键盘协议、关闭 bracketed paste、退出备用屏。
4. headless runner 收到 SIGTERM / SIGHUP 时自己写标记并停掉子进程。

关键参数：

```text
MAX_RUNNING=4  MAX_CONTINUE=3  MAX_REVIEW_ROUNDS=7  MAX_ATTEMPTS=14
POLL_SECONDS=5  INTERACTIVE_TICK=2  START_GRACE=180  START_CONFIRM_SECONDS=15
```

#### 沙箱

- `safe`（默认）：Claude 用 sandbox + `acceptEdits` + 工具白名单 + `--add-dir`；codex 用 `workspace-write` + 网络 + 显式 writable roots + `approval_policy="never"`（exec 和 TUI 共用 `codex_access`）。
- `yolo`：只有用户逐 lane 批准后才能用。omp / pi / grok 没有可保证的 OS 沙箱，`safe` 会被拒，选它们就等于批准 yolo。

## 5. 项目常见问题说明

### 单独控制某一条 lane 怎么调用？

所有子命令都带 `<run> <lane>`，例如 `$L verify <run> i18n-api`、`$L start <run> i18n-api --reason continue --prompt "…"`。正常使用时由编排者调用，不需要手动执行。

### codex 为什么默认 headless？

headless 有三个确定保证：`--output-schema` 强制结构化结果、进程退出就是回合结束、`approval_policy="never"` 不会弹对话框。交互模式靠 agent 自觉跑 `report`，回合结束靠 herdr 屏幕检测（codex 集成只上报 session）。只有需要在 pane 里实时观察或干预 codex 时，才用 `"mode": "interactive"` 预设。

### attention 和结果里的 `status: blocked` 有什么区别？

attention 是 TUI 卡在权限 / 信任弹窗上，attempt 还在进行，lane 仍是 `running`：用户到 lane pane 处理弹窗即可，编排者不能重新 `start`。结果里的 `status: blocked` 是执行者主动结束回合，需要计划之外的决定。

### 收到 `via: lost` 怎么办？

runner 被硬杀了。`wait` 已经停掉孤儿执行者并修复了终端；事件带 `orphan` 字段时，说明还有进程没停掉，先在 lane pane 里结束它（`start` 在它存活时会拒绝）。之后按 `via: exited` 续跑，同一 session 会被恢复。

### 为什么 lane 不能直接 merge 或 force push？

执行与发布分权：lane 只 commit；验收、review、push、开 PR 都由编排者在门禁之后做，merge 由用户决定（按仓库自己的合并方式）。`local_only` 的 kind 只能留在本地。

### 没有 `local_review` skill 能用吗？

能。review 提示词本身要求按 must_fix / should_fix / suggestion / accepted 分级，并在最后单独输出 `DECISION: PASS|BLOCK|UNVERIFIED`；codex 有同名 skill 时会按 skill 的规则执行。判定行缺失时本轮不计数，可以重跑。

### 只用 GitHub 吗？

`publish` 只支持 GitHub：PR 仓库由 `origin` 推导，用 `gh` 创建。不开 PR（`publish: local`）时任何 git 仓库都能用。

### 上下文快满了会怎样？

五种执行者都自带自动压缩；`TASK.md` 要求每完成一项清单就重写 `progress.md`，它就是 handoff 文件。换执行者（`set-executor`）或开新 session 时，执行者从 `TASK.md` 和 `progress.md` 继续。

### 如何验证 README 里的入口没坏？

```bash
python3 -m unittest skills/lane-dispatch/bin/test_lanectl.py
python3 skills/lane-dispatch/bin/lanectl.py --help
python3 skills/lane-dispatch/bin/lanectl.py config
```

## 6. 安全说明

- lane 默认 `safe`：Claude 用自带沙箱 + `acceptEdits` + 工具白名单，codex 用 `workspace-write`。`yolo` 会关闭审批（codex 还会关闭沙箱），只应在你逐条 lane 批准后使用；omp / pi / grok 没有可保证的 OS 沙箱，只能 `yolo`。
- spec 里的 `setup` 和 `acceptance` 命令在沙箱外、以你的身份执行，等同于你在终端里运行它们。只派发你信任的计划。
- `lanectl` 会对自己记录的 runner / 执行者进程发送信号，并在恢复时向 lane pane 的 tty 写入终端复位序列；只作用于本 run 记录过、且 pid 启动时间匹配的进程。
- 运行状态（提示词、结果、review 报告）以明文保存在 `~/.agents/lane-dispatch/`。

## 7. 许可与致谢

MIT，见 [LICENSE](LICENSE)。

设计思路来自 [bestony/herdr-dispatch](https://github.com/bestony/herdr-dispatch)（MIT）：编排者规划、lane 执行、编排者发布的分工，以及 `TASK.md` / `progress.md` 的 lane 记忆约定；lane brief 模板的部分措辞改编自它的 `plan.md`，原许可声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。运行环境依赖 [herdr](https://herdr.dev)。
