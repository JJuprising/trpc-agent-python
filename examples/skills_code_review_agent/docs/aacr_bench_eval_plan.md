# AACR-Bench 小样本评测接入实施方案

> 目标：让本框架（skills_code_review_agent）作为第 4 种 reviewer 接入
> `/home/chan/aacr-bench/evaluation` 统一评测框架，先跑 10 条小样本，
> 产出语义/行号两套 P/R/F1 与耗时、token 指标。
>
> 阅读方式：每个模块先给「现状（旧框架复习）」再给「改造后（新框架模式）」，
> 最后汇总改动清单、工作量与分阶段实施步骤。

## 1. 背景速览

### 1.1 AACR-Bench 评测侧要求

评测框架（`aacr-bench/evaluation/`）的流水线为 **数据加载 → 评审执行 → 评测打分**：

1. 数据：标准 JSONL，每行一个样本：

   ```json
   {
     "instance_id": "psf__requests-5711@9484e13",
     "repo": "psf/requests",
     "base_commit": "5351469...",
     "head_commit": "9484e13...",
     "reference_comments": [
       {"path": "setup.py", "start_line": 46, "end_line": 46,
        "side": "right", "text": "人工标注的参考评论"}
     ]
   }
   ```

2. 评审：每个样本由 reviewer 模块完成 `clone → checkout head_commit →
   评审 base..head 变更 → 写结果文件 <instance_id 替换 / 为 __>.json`。
3. 评测（judge 四阶段匹配）：`path → side → 行号(±k) → LLM 语义`，
   产出语义/行号两套 Precision / Recall / F1。
   行号任一侧为 null 时跳过行号阶段，仅做语义匹配。

### 1.2 本框架现状数据流（旧框架复习）

```text
run_agent.py (CLI: --repo-path / --diff-file / --file-list / --fixture 四选一)
  └─ CodeReviewWorkflow.run(ReviewRequest)
       ├─ _parse_input()      → 四种输入模式之一，产出 ParsedReviewInput
       ├─ _sandbox_request()  → 按输入模式派发沙箱命令（确定性规则脚本）
       ├─ fake / real 分支    → FakeSandbox 或 Docker + LlmAgent(Skill 驱动)
       ├─ ReviewAnalysis      → findings / warnings / needs_human_review
       ├─ store.save()        → SQLite / PostgreSQL 审计留痕
       └─ ReportWriter        → reports/output/<task_id>/review_report.json + .md
```

关键事实（决定了改造设计）：

| # | 事实 | 出处 |
|---|------|------|
| F1 | 输入四选一互斥：`--diff-file` 与 `--repo-path` 不能同时传 | `workflow.py` `_parse_input`（"Select exactly one review input"） |
| F2 | diff/fixture 模式下**仓库源文件不挂载**，agent 无法读仓库上下文 | `SKILL.md` 分支 1（"The source files are not mounted"） |
| F3 | git_worktree 模式下整个仓库只读挂载到容器 `work/inputs`，skill 脚本在容器内跑 git | `sandbox/docker.py` `create_runtime`（binds `repository:work/inputs:ro`） |
| F4 | 输出 `ReviewFinding` 只有单行号 `line`（可为 null），无 start/end 区间 | `reports/models.py` |
| F5 | 每个输入模式有"证据完整性"要求，缺证据强制进 needs_human_review | `workflow.py` `_execution_completeness_issues` |
| F6 | token 消耗未采集（`MonitoringSummary` 只有时长/工具调用数） | `reports/models.py` |

**结论**：AACR-Bench 的样本是 `base..head` 两个已提交 commit 的差异，且该基准
核心考察**仓库级上下文理解**。直接用 `--diff-file`（F1/F2）会丢掉仓库上下文，
评测分数系统性偏低。因此核心改造 = 新增 **commit-range 输入模式**，
复用 git_worktree 的"仓库挂载"能力（F3）。

## 2. 总体架构前后对比

### 2.1 输入模式

```text
改造前（4 种，互斥）                 改造后（5 种，互斥）
┌──────────────────┐               ┌──────────────────┐
│ git_worktree     │ 工作区变更     │ git_worktree     │（不变）
│ diff_file        │ 无仓库上下文   │ diff_file        │（不变，保留）
│ file_list        │               │ file_list        │（不变）
│ fixture          │               │ fixture          │（不变）
└──────────────────┘               │ git_commit_range │ ★ 新增：base..head
                                   │   · 仓库挂载（同 git_worktree）
                                   │   · 容器内 git diff 取变更
                                   └──────────────────┘
```

### 2.2 评测链路（端到端）

```text
改造后完整链路：

aacr-bench/evaluation/pipeline.py --reviewer myagent
  └─ reviewers/myagent.py（新增，仿 ocr.py）
       ├─ prepare_repo()            # 复用框架：clone + checkout head_commit
       ├─ subprocess: run_agent.py  # 调本框架 CLI（一次性进程，隔离最干净）
       │    --repo-path <clone> --base-commit X --head-commit Y
       │    --output-dir <run目录> --database <run目录>/eval.sqlite3
       ├─ 读 review_report.json     # 转换为评测标准结果格式
       ├─ 写 <safe_id>.json
       └─ clean_worktree()          # 复用框架清理
  └─ evaluate.py                    # path→side→line→semantic 四阶段打分
```

## 3. 各模块前后变化

### 3.1 `run_agent.py`（CLI 入口）

**现状**：四个互斥输入参数，无 commit 概念。

```python
inputs.add_argument("--diff-file", ...)
inputs.add_argument("--file-list", ...)
inputs.add_argument("--fixture", ...)
```

**改造后**：互斥组新增一项；base/head 随 `--repo-path` 出现。

```python
inputs.add_argument("--commit-range", metavar=("BASE", "HEAD"), nargs=2,
                    help="评审两个已提交 commit 的差异（需同时提供 --repo-path）")
# 运行示例：
#   run_agent.py --repo-path <clone> --commit-range <base> <head>
```

`run()` 中把 base/head 塞进 `ReviewRequest`。校验：`--commit-range` 必须搭配
`--repo-path`，且与其他输入互斥（在 workflow 层统一报错）。

改动量：约 10 行。**风险：无**（纯增量）。

### 3.2 `workflow.py`（流程编排）

**现状（复习）**：

- `ReviewRequest`：`repository_path / diff_file / file_list / fixture / scope / fake_model / dry_run`
- `_parse_input()`：互斥校验 + 分派到 `inputs/parser.py` 的四个解析函数
- `_sandbox_request()`：按 kind 派发容器内命令——

  | kind | 容器命令 |
  |------|---------|
  | git_worktree | `review_git_changes.py work/inputs --mode unstaged` |
  | diff_file / fixture | `run_review_rules.py work/inputs/<diff>` |
  | file_list | `inspect_file_list.py work/inputs/<list>` |
- `_execution_completeness_issues()`：证据要求——

  | kind | required evidence |
  |------|-------------------|
  | diff_file / fixture | `{"rules"}` |
  | file_list | `{"file-list", "inspect:file-list"}` |
  | git_worktree (changed) | `{"files:changed", "diff:unstaged", "diff:staged"}` |

**改造后**（4 处小改）：

1. `ReviewRequest` 增加字段（约 3 行）：

   ```python
   base_commit: str | None = None
   head_commit: str | None = None
   ```

2. `_parse_input()`：互斥校验中允许 `repository_path + base_commit + head_commit`
   组合，分派到新的 `parse_git_commit_range()`；其余互斥规则不变。
3. `_sandbox_request()`：新增分支（复用 git 脚本，仅换 mode）：

   ```python
   elif parsed_input.summary.kind == "git_commit_range":
       command = (
           "python3 scripts/review_git_changes.py work/inputs "
           f"--mode commit --base {base} --head {head}"
       )
   ```

4. `_execution_completeness_issues()`：新证据要求
   `{"files:commit", "diff:commit"}`（变更文件清单 + 差异记录，
   语义对齐 git_worktree 的三件套，但改为两项：commit-range 模式下
   变更清单由 diff 记录自带，无需单独枚举）。

改动量：约 25–35 行。**风险：低**（新增分支不动旧分支；F5 的证据机制原样生效）。

### 3.3 `inputs/parser.py`（输入解析）

**现状（复习）**：四个解析函数，其中 `parse_git_worktree(path)` 只解析
staged/unstaged 工作区变更（`git diff` 不带 commit 参数）；
`parse_diff_text()` 已支持可选 `repository_path` 参数。

**改造后**：新增一个解析函数（约 50–70 行），复用现有设施：

```python
def parse_git_commit_range(
    path: Path, base: str, head: str,
) -> ParsedReviewInput:
    """解析 base..head 已提交差异；仓库挂载方式与 git_worktree 相同。"""
    # 1) 校验 path 是 git 仓库，校验 base/head 存在（git cat-file -e）
    # 2) 宿主侧只做轻量摘要（git diff --stat）供 ReportInputSummary 使用
    # 3) 差异正文不落宿主文件 —— 由容器内 review_git_changes.py 现场执行
    #    （保持 F3 的"证据在沙箱内产生"安全链路，不做旁路）
    return ParsedReviewInput(
        summary=ReviewInputSummary(kind="git_commit_range", source=str(path), ...),
        input_root=path, repository_path=path,
    )
```

与 `parse_git_worktree` 的差异：不跑 staged/unstaged，仅登记 commit 区间；
`repository_path` 必设（保证仓库被挂载，补上 F2 缺失的上下文能力）。

改动量：约 50–70 行。**风险：低**。

### 3.4 `reports/models.py`（结构化模型）

**现状**：`ReviewInputSummary.kind` 为四值 Literal；`ReviewFinding` 单行号。

**改造后**：

1. `kind` Literal 增加 `"git_commit_range"`（1 行）。
   SQLite/PG 中该字段是文本/JSON，无 schema 迁移。
2. `ReviewFinding` **不改**：单行号在评测侧可表达（见 3.8 转换映射），
   避免动输出契约影响存储与已有报告。

改动量：1 行。**风险：无**。

### 3.5 `skills/code-review/`（Skill 规则与脚本）

**现状（复习）**：`SKILL.md` 定义 4 个输入分支（diff / file-list /
changed git / full git），脚本全部分页返回，证据必须带 `input_digest`。
`review_git_changes.py --mode unstaged|staged` 在容器内对挂载仓库跑
`git diff --cached` / `git diff`。

**改造后**：

1. `scripts/review_git_changes.py`：`--mode` 增加 `commit`，
   新增 `--base/--head` 参数，内部执行 `git diff --find-renames <base>..<head>`；
   分页/摘要/`input_digest` 契约与现有两种 mode 完全一致（约 30–50 行）。
2. `scripts/inspect_files.py`：`--scope` 增加 `commit`，校验目标路径属于
   `git diff --name-only <base>..<head>` 的文件集合（约 10–20 行），
   供 agent 读取上下文（受控读取，与 changed/full 同级的安全约束）。
3. `SKILL.md`：新增分支 3.5「commit range」（约 15 行）：

   ```markdown
   3.5 For a committed commit range, collect the diff with
       `python3 scripts/review_git_changes.py work/inputs
        --mode commit --base <base> --head <head>`, following `next_cursor`.
       Read repository context with
       `python3 scripts/inspect_files.py work/inputs --scope commit --path ...`.
   ```

改动量：约 60–90 行。**风险：中**——这是唯一触碰"agent 行为面"的改动，
prompt 分支措辞会直接影响评测表现，需用 1 条样本人工校验
（重点验证 agent 能否正确跑完分页并读上下文）。

### 3.6 aacr-bench 侧：新增 `evaluation/reviewers/myagent.py`

**现状**：框架只有 ocr / claude / codex 三种 reviewer。

**改造后**：新增第 4 种（约 120 行，仿 `reviewers/ocr.py`）：

```python
def review_instance(instance, repo_dir, results_dir, timeout_minutes, preview):
    repo_path = prepare_repo(...)                 # 复用 repo_utils
    process = run_command([
        sys.executable, str(AGENT_ENTRY),         # run_agent.py
        "--repo-path", str(repo_path),
        "--commit-range", instance.base_commit[:12], instance.head_commit[:12],
        "--output-dir", str(work_dir),
        "--database", str(work_dir / "eval.sqlite3"),
    ], cwd=..., timeout_seconds=timeout_minutes * 60)
    report = json.loads((work_dir / "review_report.json").read_text())
    payload = {
        "instance_id": ..., "duration_seconds": ...,
        "token_usage": {...},                     # 见 3.9，可先置 0
        "review_output": convert_findings(report["analysis"]),
    }
    (results_dir / f"{instance.safe_id}.json").write_text(...)
    clean_worktree(repo_path)
```

注册 4 处（均为机械改动）：

| 文件 | 位置 | 改动 |
|------|------|------|
| `pipeline.py` | `_review_one_instance` | 加 `myagent` 分发分支 |
| `pipeline.py` | `run_review_stage` | 加环境预检（检查 run_agent.py 存在） |
| `pipeline.py` | `build_parser` | `--reviewer` choices 加 `"myagent"` |
| `evaluate.py` | `load_target_comments` 的 builder 映射 | 加 `"myagent": build_target_comments_from_codex`（复用 codex 解析器，形状同构） |

### 3.7 结果格式转换映射（核心契约）

本框架 `ReviewAnalysis` → 评测标准 `review_output[]`：

| ReviewFinding（本框架） | review_output 项（评测侧） | 说明 |
|---|---|---|
| `file` | `file` | 直接映射；judge 内部有路径归一化 |
| `title` | `summary` | 一句话标题 |
| `evidence` + `recommendation` | `description` | 两段拼接，`\n` 连接 |
| `line` | `start_line` = `end_line` = `line` | 单行号 = 闭区间退化；**null 保留**（judge 跳过行号阶段，仍可语义匹配） |
| —（隐含 right） | `side` 固定 `"right"` | 三个官方 reviewer 同样硬编码 |
| `severity` / `category` / `confidence` / `source` | 不映射 | 评测不消费；留在 review_report.json 存档 |

**取数口径（决策点）**：只取 `analysis.findings`，不含 `warnings`
（confidence < 0.70 的低置信项）。理由：评测把所有报告项计入
`generated_notes`，纳入 warnings 会直接拉低 Precision，与本框架
"precision 优先"的设计初衷一致。

### 3.8 可选改造：token 统计（~0.5 天，可后置）

**现状**：`MonitoringSummary` 只有 `total_duration_ms / tool_call_count`，
评测表里 `avg_tokens` 将为 0（不影响 P/R/F1，只影响成本对比维度）。

**改造后**：`_run_agent()` 逐事件累计 SDK usage → `MonitoringSummary` 增加
`input_tokens / output_tokens` → reviewer 转换时写入结果文件 `token_usage`。
实现前先确认 trpc-agent-python 事件流的 usage 字段名（`agent/model_io.py`
已有原始 IO 落盘，可作为解析来源）。

### 3.9 明确不做的事

- 不改 `sandbox/`（Docker 加固、挂载策略原样保留）
- 不改 `storage/` schema（kind 是文本字段）
- 不改 `filters/`（commit-range 命令走同一 Filter 策略面）
- 不动既有 4 种输入模式的任何行为

## 4. 工作量汇总

| 模块 | 改动 | 规模 | 风险 |
|------|------|------|------|
| run_agent.py | `--commit-range` 参数 | ~10 行 | 无 |
| reports/models.py | kind Literal +1 | ~1 行 | 无 |
| inputs/parser.py | `parse_git_commit_range` | ~50–70 行 | 低 |
| workflow.py | Request 字段 + 3 处分支 | ~25–35 行 | 低 |
| skills/（脚本 + SKILL.md） | commit mode + 新分支 | ~60–90 行 | **中**（影响 agent 行为） |
| aacr-bench reviewers/myagent.py | 新文件 + 4 处注册 | ~120 行 | 低 |
| 结果转换函数 | 新增 | ~40 行 | 低 |
| token 统计（可选） | 监控字段 + 采集 | ~40 行 | 低 |
| **合计** | | **~350–470 行** | 1.5–2.5 人天 |

## 5. 分阶段实施步骤

### 阶段 0：环境与数据（0.5h）

```bash
cd /home/chan/aacr-bench/evaluation
uv venv .venv --python 3.11 && uv pip install -r requirements.txt
source .venv/bin/activate
python -m converters.aacr_bench --input ../dataset/positive_samples.json \
    --output data/aacr_bench.jsonl --limit 10 --seed 42 --validate
# Judge 先用 Mock：.env 不填 JUDGE_API_KEY 即可
```

### 阶段 1：链路打通（半天，不动本框架）

临时用 `--diff-file` 模式验证评测端到端（接受无仓库上下文的低分）：
外部脚本 `git diff base..head > patch` → `run_agent.py --diff-file patch --fake-model --dry-run` → 转换函数 → `pipeline run --stage eval`。
**通过标准**：10 条样本产出 metrics JSON，字段齐全。

**阶段 1 已完成（2026-09-13），结果记录**：

- 驱动脚本：`aacr-bench/evaluation/stage1_driver.py`（blobless clone 缓存
  `repo_stage1/` → `git diff base..head` 生成 patch → fake 模式评审 →
  codex 形状结果落盘 `results/stage1_fake/`）
- 最终 metrics（10/10 样本）：expected 61 / generated 37 / 语义与行号匹配 0，
  P/R/F1 = 0——**真实基线而非 bug**：纯规则引擎在真实 diff 上与 gold
  零重叠（逐样本人工核对确认 path/行号坐标均在文件空间、可比），
  定量印证 8.1 节"规则-考纲错配"判断；Mock judge 请求 0 次是因为
  语义调用仅在 path+line 通过后触发
- 踩坑记录（阶段 3 会复现，需处理）：
  1. `astral-sh__uv@ed57db2` 为 squash-merge PR 的原始 head commit，
     GitHub 上悬空、不可 fetch（`not our ref`）。阶段 1 的解法是用
     原始数据集的 `githubPrUrl` 直接拉 `<pr>.diff`。阶段 3 的
     reviewers/myagent.py 走 prepare_repo 全量 clone 也会遇到同样问题，
     需内置同样的 PR-diff 回退
  2. `.env` 将 `CODE_REVIEW_MAX_OUTPUT_BYTES` 收紧为 15KB，而
     `workflow._sandbox_request` 构造的 `SandboxCommand` 用默认 64KB
     预算 → Filter 判超预算 → 所有命令被拦。阶段 1 由驱动进程注入
     `CODE_REVIEW_MAX_OUTPUT_BYTES=65536` 规避；正式修复应在
     `_sandbox_request` 中用 `self.limits` 构造命令预算（框架小 bug）

### 阶段 2：commit-range 模式（1 天）

按 3.1–3.5 实施。**通过标准**：
- `run_agent.py --repo-path <clone> --commit-range A B --fake-model --dry-run`
  走通，kind 记录为 `git_commit_range`；
- 真实模型跑 1 条样本，人工核对：agent 是否完成分页证据收集、
  findings 的 `line` 是否为新文件（right）侧行号。

**阶段 2 已完成（2026-09-13），结果记录**：

- 改动落点：`reports/models.py`（kind Literal + base/head 字段）、
  `inputs/parser.py`（`parse_git_commit_range`，宿主侧只验 commit 存在）、
  `workflow.py`（Request 字段 / `_parse_input` / `_sandbox_request` commit 分支 /
  证据要求 `{"files:commit", "diff:commit"}`）、`filters/policy.py`
  （`ReviewPolicyContext` 加 base/head，`_evaluate_commit_range` 仅放行
  两种命令形状且校验 commit 与上下文一致）、`review_git_changes.py --mode commit`、
  `inspect_files.py --scope commit`（可读集合 = diff 文件集）、
  `SKILL.md` 分支 4、`prompts.py`、`run_agent.py --commit-range`
- 验证：fake 模式烟测 kind/digest/证据记录正确；98 项既有测试全过
- 真实模型验证（Docker + deepseek-v4-flash，样本 `vllm-project__vllm@6217b0c`，
  1 文件 +12 行，报告 `aacr-bench/evaluation/results/stage2_real/agent_out/`）：
  - 证据链完整：`review_git_changes --mode commit` 19 条记录（1 页取完），
    `inspect_files --scope commit` 读变更文件，agent 严格使用批准的命令形状
  - 行号语义核对通过：规则候选 `test_missing`（confidence 0.65 < 0.70，
    agent 正确降级进 warnings）`line=90`，对应 right 侧第 90 行
    `def shutdown(self):`——正是新增行
  - 越界防护符合预期：读未变更的 `base.py` 被脚本拒绝，agent 如实写入
    needs_human_review 而非编造证据；2 次 Filter 拦截后均自行恢复
  - 预算消耗：53.9s / 9 工具调用 / 7 沙箱执行（默认预算内宽裕）
- 新增修复：`ReviewFinding.severity` 增加 before-validator
  `normalize_severity_alias`——首次真实运行 deepseek 提交 `severity='info'`
  被 pydantic 拒绝，重试时改提交 error 形状对象再次失败导致整轮 failed；
  现将 `info/notice/warning/error` 等常见别名归一化到四个合法字面量
- 阶段 3 前需处理的发现（按影响排序）：
  1. **`inspect_files.py` 文件内容截断不可续读**：`MAX_FILE_BYTES=1536`
     硬编码，且 `next_cursor` 只对文件列表分页、不对单文件内容分页
     （返回 `truncated:true` 同时 `next_cursor:null`）。本次样本变更区域
     在文件第 87–101 行，agent 只能看到前 1536 字节（约至第 60 行），
     无法基于完整上下文核实——这正是 8.1 节 File 级 35% gold 的覆盖瓶颈，
     建议作为创新点前的必改项（内容分页或提高单文件预算）
  2. **预算硬上限**：`agent/config.py` 校验 `TOTAL_TIMEOUT_SECONDS<=120`、
     `MAX_TOOL_CALLS<=30`，`sdk_filter.py` 校验 `MAX_SANDBOX_RUNS<=12`，
     env 无法突破——阶段 6 风险表中"评测环境 env 放宽"的预期不成立，
     大 diff 样本（40% >200 行）120s 内大概率跑不完；需改 config 上限
     或确认评测口径接受截断
  3. 阶段 1 踩坑 #2 仍在：`.env` 15KB 与 `_sandbox_request` 默认 64KB
     命令预算的错配未在框架内修复，仍靠进程 env 注入规避

### 阶段 3：正式接入评测（0.5 天）

按 3.6 实现 reviewer 并注册，跑：

```bash
python -m pipeline run --stage all --reviewer myagent \
    --dataset data/aacr_bench.jsonl --run-id baseline --concurrency 2
```

### 阶段 4：出小样本指标（0.5 天）

- 真实 Judge（填 `JUDGE_*`）重跑 eval：`--stage eval --run-id baseline`
- 记录语义/行号 P/R/F1、平均耗时；可选补 token 统计（3.8）
- 与 OCR/claude/codex 官方基线对比（官方数据见 aacr-bench README）

## 6. 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| agent 报告行号语义不是 right 侧（如 hunk 行号） | 行号 F1 崩塌 | 阶段 2 人工校验 1 条样本；必要时在 prompt 中强化约定 |
| commit-range 模式下 agent 未按新 SKILL 分支执行 | 证据不完整 → 全部进 needs_human_review | SKILL.md 措辞迭代；评测口径可决定是否把 needs_human_review 计入 findings（默认不计） |
| 本框架默认预算（110s/30 工具/12 sandbox）对 repo 级样本偏紧 | 评审不完整 | 评测环境用 `CODE_REVIEW_*` 环境变量放宽，不改代码 |
| 评测机无 Docker daemon | 真实模式不可用 | fake/dry-run 先行；Docker 为硬依赖，需部署 |
| findings 为空或超时的样本 | 计入 missing/0 生成 | judge 已有 missing 跳过逻辑；结果文件照常落盘含 stderr |

## 8. 创新点路线图与消融实验设计

> 目标：base 跑通后，引入对标 OCR 的增量模块，获得**统计上可辨识**的提升。
> 榜单锚点：OCR + 顶级模型 = P 33.9% / R 16–20% / F1 21–25%。
> 33% 即 SOTA（gold 为专家增强标注的"充分暴露缺陷集"），
> 目标应设为**同模型下的相对提升**，而非绝对值。

### 8.1 数据依据（mini-50 实测）

- gold 分布：526 条 / 50 PR，中位 7.5 条每样本
- category：Code Defect 43% + **Maintainability 47%** + Performance 6% + Security 4%
- context：Diff 级 52% / File 级 35% / Repo 级 13%
- diff 规模：中位 154 行；**>200 行占 40%，贡献 55% 的 gold**；>500 行占 18%
- 规则库现状错配：六类规则覆盖 Security(仅 4% gold) 等；
  **无通用 Defect 类、无 Maintainability 类**，且 prompt 明令
  "Avoid cosmetic style findings"——需改写为"避免纯格式化，
  保留有维护价值的结构建议"

### 8.2 五个创新点（按实施顺序）

| # | 创新点 | 对标 OCR | 瞄准指标 | 预期增益 | 成本 |
|---|--------|----------|----------|----------|------|
| 1 | 规则库对齐四类（补 Defect/Maintainability 规则） | 规则匹配 | Recall | +3~8pp | ~1d |
| 2 | 定位校准（line 吸附到 candidate lines/新增行区间） | 定位模块 | Line-F1 | +5~15pp | ~0.5–1d |
| 3 | 反思过滤（findings 二次自检，确定性+LLM，只删不加） | 反思模块 | Precision | +3~6pp | ~1d |
| 4 | **分治式上下文工程**（分组 → 子 agent 隔离 → 记忆压缩） | 文件打包 + subagent + 三分区压缩 | Recall(大 diff) + Avg Time | R +3~6pp | ~2–3d |
| 5 | 预算缩放实验（30 vs 100 工具轮 × 压缩开关） | — | 审查深度-召回曲线 | 实验设计 | ~0.5d |

### 8.3 创新点 4 的落地要点（分治式上下文工程）

- **plan 阶段纯确定性**：按同目录 / 同扩展名 / 关联文件对（test↔源文件、
  i18n 成对）规则分组，不引入 LLM planner
- **子 agent 复用 SDK 能力**：`trpc_agent_sdk` 已内置
  `SpawnSubAgentTool`（archetype 模板派生）；asyncio 并发约束组数
- **隔离的只是 LLM 消息缓冲区**：子 agent 共享同一容器与 work/inputs 挂载，
  沙箱与 Filter 架构不变；保留 `inspect_files` 全局读以补偿
  Repo 级跨组缺陷（13% gold）
- **记忆压缩自建三分区**（SDK 仅有 skip_summarization 跟随摘要）：
  指令分区不动 / 工作分区（当前组 diff + 近期证据）/
  历史分区（老工具结果 → 结构化摘要）；历史摘要附 digest 防证据丢失
- **预算分离已有对应物**：inline 16KiB 上限（输出侧）与上下文预算（输入侧）
  分离，与 OCR 的 MAX_TOKENS / MAX_COMPLETION_TOKENS 分离同构

### 8.4 消融实验设计

- 固定模型端点（GLM 或 Qwen，OpenAI 兼容），50 条子集（`--seed 42`），
  `--eval-rounds 3`；50 条规模下 precision 标准差 ≈1.5–2pp，
  3–5pp 提升 = 2–3σ，可辨识
- 实验组：base / +规则 / +定位 / +反思 / +分治（逐组叠加）
- **按 diff 规模分桶报告**（<200 行 / ≥200 行）的 Recall 对比——
  直接证明创新点 4 的增益来源于大 diff 覆盖
- 报告口径：相对提升 + 逐样本配对变化（新增/消除匹配的 PR 清单）
- token 效率叙事：本框架默认预算（110s / 30 工具 / 15KiB 截断）远低于
  OCR 榜单消耗（334K–682K tokens/样本），"同分或近分但 1/N token"
  为有效结论

## 9. 验收清单

- [ ] 转换器产出 50 条标准 JSONL（`--seed 42`）且 schema 校验通过
- [x] `--fake-model --dry-run` 下 commit-range 模式走通并产出报告
- [x] 真实模型单样本报告：行号语义人工核对通过
- [ ] reviewers/myagent.py 注册后 `pipeline --stage review` 产出 `<safe_id>.json`
- [ ] Mock Judge 全链路出分
- [ ] 真实 Judge 出分，与官方基线表格式对齐（semantic/line P/R/F1 + 耗时 + token）
