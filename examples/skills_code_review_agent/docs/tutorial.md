# 基于 Skill 的代码审查 Agent：从运行到扩展

这是一份面向初学者的源码教程。我们不先把目录拆成一堆互不相关的名词，
而是先跟着一份代码变化走过整个系统，再在它经过某个模块时解释这个模块
为什么存在。

读完后，你应该能回答：

- 一次审查从哪里开始，结果怎样到达报告和数据库？
- Agent、Skill、Tool、Filter、Sandbox 和 Workflow 怎样配合？
- 为什么 Agent 不能直接运行任意 shell 命令？
- 为什么一次小改动经常需要同步修改多个文件？
- 一个测试通过时，究竟证明了哪一层？
- 如何亲自运行一次审查，并从 JSON、Markdown 和 SQLite 看懂结果？
- Tool Use、function call、Sandbox run 和 trajectory 分别代表什么？

> 本教程中的 --diff-stdin 和 category_distribution 是综合练习，当前版本尚未实现。

## 1. 先用一句话理解项目

这个项目接收代码变化，收集有限的检查证据，让 Agent 帮忙理解证据，
然后把一个可信、可追溯的审查结果交付给人和程序。

完整流程可以先记成：

    用户命令
      -> 输入解析
      -> 创建审查任务
      -> Agent 决定需要哪些证据
      -> Agent 请求 Skill 工具
      -> Filter 检查请求
      -> Docker Sandbox 执行批准的脚本
      -> Agent 根据证据生成结构化结果
      -> Workflow 校验、去重、降级、脱敏
      -> JSON/Markdown 报告和数据库记录

每个箭头都代表一次责任交接：

| 要解决的问题 | 负责模块 | 交给它的原因 |
|---|---|---|
| 用户究竟提供了什么？ | inputs | 输入格式、路径和大小必须确定性校验 |
| 任务是否开始、失败或完成？ | workflow.py | 生命周期不能依赖模型记忆 |
| 需要收集哪些证据？ | agent | 这是需要理解上下文的推理工作 |
| 模型请求的命令能否执行？ | filters | 模型不能自己给自己授予权限 |
| 已批准的代码在哪里执行？ | sandbox | 不可信代码不能在宿主机运行 |
| 结果是否对应本次变更？ | normalization | 模型可能虚构文件或行号 |
| 如何交付和复盘？ | reports、storage | 文件和数据库是最终数据出口 |

所以这些目录不是并列的功能清单，而是一条逐步收紧的控制链：
上游产生信息，下游验证信息并限制副作用。

## 2. 先认识 diff

### 2.1 diff 是什么

diff 可以理解成“旧文件和新文件之间的变化说明”，它通常不是完整源文件。
统一 diff 的一个最小例子如下：

    --- a/app.py
    +++ b/app.py
    @@ -1,2 +1,3 @@
     def run(command):
    +    os.system(command)
         return True

逐行看：

| 内容 | 含义 |
|---|---|
| --- a/app.py | 旧文件路径 |
| +++ b/app.py | 新文件路径 |
| @@ -1,2 +1,3 @@ | 变化片段的范围说明 |
| 行首加号 | 新增行 |
| 行首减号 | 删除行 |
| 行首空格 | 没有改变、但用于提供上下文的行 |

变化片段通常叫 hunk，可以理解为“变更块”。一份 diff 可以有多个 hunk。
项目保留新增、删除、上下文、旧行号和新行号，是因为审查需要回答：

    哪个文件变了？
    哪个 hunk 变了？
    哪一行是本次新增的？
    规则发现的问题是否真的落在这次变化上？

### 2.2 初学者词汇表

| 术语 | 简单解释 |
|---|---|
| Git | 记录代码版本和变化的工具 |
| repository / 仓库 | 一组由 Git 管理的项目文件 |
| worktree / 工作区 | 当前正在编辑的仓库目录 |
| staged | 已放入 Git 暂存区、准备提交的变化 |
| unstaged | 已修改但尚未放入暂存区的变化 |
| untracked | Git 尚未跟踪的新文件 |
| fixture | 测试专用的固定输入样例 |
| Agent | 能根据上下文决定下一步动作的程序 |
| LLM | 大语言模型；本项目用它理解证据并组织结论 |
| tool call | Agent 请求调用一个程序化工具 |
| Skill | 给 Agent 的任务说明、规则和工具使用方法 |
| Sandbox | 隔离执行环境，本项目主要是 Docker 容器 |
| Filter | 工具执行前的拦截器和权限检查器 |
| scope | 审查范围，例如 changed 或 full |
| candidate | 规则脚本提出的可能问题 |
| finding | 经过治理后可以展示的问题 |
| warning | 证据或置信度不足、需要谨慎理解的问题 |
| pagination | 分页，把大结果拆成多页返回 |
| digest | 内容摘要，通常是 hash，用于判断输入是否变化 |
| redact | 脱敏，用占位符替换密码、Token 等敏感值 |
| persistence | 持久化，把内存数据保存到文件或数据库 |
| schema | 数据库表结构或结构化数据的形状 |
| idempotent | 幂等，同一结果重复保存不会产生错误重复数据 |

## 3. 第一次运行：先走 Fake 模式

Fake 模式用确定性逻辑代替模型和真实 Docker，适合学习和快速回归。
它仍然走输入、Workflow、结果、报告和 SQLite 链路，但不会调用模型 API，
也不会执行不可信代码。

    /home/chan/trpc-agent-python/.venv/bin/python \
      examples/skills_code_review_agent/run_agent.py \
      --fixture security --fake-model

运行后主要关注：

    reports/output/<task-id>/review_report.json
    reports/output/<task-id>/review_report.md
    storage/reviews.sqlite3

第一次不要急着读完所有源码，可以先检查：

1. input_summary 说明审查了什么；
2. analysis.findings 是否包含 security；
3. sandbox_runs 和 filter_decisions 是否留下过程记录；
4. SQLite 是否保存了同一任务的结构化数据。

task-id 是一次审查任务的唯一标识。它把报告、Sandbox 运行、Filter 决策和
数据库记录关联起来。

### 3.1 先确认运行环境和命令参数

所有命令都假设当前目录是仓库根目录。先确认 Python 解释器和入口脚本存在：

    pwd
    test -x .venv/bin/python
    test -f examples/skills_code_review_agent/run_agent.py

如果项目根目录没有 .venv，可以按 README 中的 uv 方式运行。之后查看 CLI
支持的参数：

    .venv/bin/python examples/skills_code_review_agent/run_agent.py --help

几个最常用的参数如下：

| 参数 | 作用 | 初学者第一次怎么用 |
| --- | --- | --- |
| --fixture security | 使用固定的安全问题样例 | 推荐作为第一个实验 |
| --fake-model | 使用确定性规则，不请求模型 | 先用它理解输入和报告 |
| --dry-run | 模拟 Sandbox 执行，不执行代码 | 研究策略和报告时使用 |
| --diff-file path | 审查一个 unified diff 文件 | 学会 diff 后再尝试 |
| --repo-path path | 审查一个 Git 工作区 | 最后再接入真实仓库 |
| --full | 审查整个仓库，而不只是变更 | 不建议第一次使用 |
| --output-dir path | 指定报告输出目录 | 实验时使用临时目录 |
| --database path | 指定 SQLite 文件 | 实验时避免污染默认数据库 |

这里要区分两件事：

    --fake-model
      不创建真实模型 Agent，直接用确定性规则分析输入。

    --dry-run
      仍然走工具策略和 Sandbox 运行记录，但 Sandbox 只返回模拟结果。

两者都适合学习。真实模式则需要 .env 中有可用的模型配置，并且本机 Docker
daemon 可用。

### 3.2 第一个完整实验：运行、定位报告、先看 Markdown

建议第一次使用临时输出目录和临时 SQLite，这样每次实验都是一份干净结果：

    REVIEW_LAB_DIR=$(mktemp -d)
    CODE_REVIEW_MAX_OUTPUT_BYTES=65536 \
      .venv/bin/python examples/skills_code_review_agent/run_agent.py \
      --fixture security \
      --fake-model \
      --output-dir "$REVIEW_LAB_DIR/reports" \
      --database "$REVIEW_LAB_DIR/reviews.sqlite3"

这里的 CODE_REVIEW_MAX_OUTPUT_BYTES=65536 只用于这个演示命令。示例 .env
可能把策略上限收紧到 15360 字节，而 FakeSandbox 构造的模拟请求默认使用
65536 字节；如果不临时对齐，Filter 会返回 output limit exceeds policy budget。
这不是安全问题检测失败，而是策略在执行前阻止了一个超出预算的请求。看到这种
情况时，先阅读报告中的 Filter 和 Sandbox 部分，不要把“没有 finding”理解成
“代码没有问题”。

成功运行后，终端会打印类似内容：

    Review completed: <task-id>
    JSON report: <output-dir>/<task-id>/review_report.json
    Markdown report: <output-dir>/<task-id>/review_report.md

先打开 Markdown：

    less "$REVIEW_LAB_DIR/reports/<task-id>/review_report.md"

把 <task-id> 替换成终端打印的实际值。阅读顺序建议是：

    1. Status 和 Conclusion
       先判断是 completed、completed_with_warnings 还是 failed。
    2. Summary 和 Findings
       看发现了什么问题、在哪个文件哪一行、证据是什么。
    3. Warnings 和 Needs Human Review
       看哪些结论不能直接当成确定问题。
    4. Filter Decisions 和 Sandbox Runs
       看工具请求是否被允许、是否真的执行、是否超时或失败。
    5. Monitoring
       看耗时、工具调用次数、阻止次数和异常分布。

completed_with_warnings 不是“程序崩溃”。它表示主流程完成了，但存在低置信度
warning、证据不完整、Sandbox 失败或人工复核项。只有读完 Conclusion 和
Needs Human Review，才能正确理解结果。

### 3.3 用 jq 从 JSON 报告回答问题

JSON 是给程序读取的结构化报告。先把路径保存到变量：

    REVIEW_REPORT=$(find "$REVIEW_LAB_DIR/reports" \
      -name review_report.json -type f | head -n 1)
    REVIEW_TASK_ID=$(jq -r '.task_id' "$REVIEW_REPORT")

如果系统没有 jq，可以先用 less 查看 JSON，或者使用 Python 标准库：

    .venv/bin/python -c \
      'import json,sys; print(json.load(open(sys.argv[1])))' \
      "$REVIEW_REPORT"

先看任务身份和输入摘要：

    jq '{
      task_id,
      status,
      scope,
      input: .input_summary
    }' "$REVIEW_REPORT"

你应该能看到这些关键信息：

    input_summary.kind       fixture
    input_summary.source     security
    input_summary.file_count 1
    input_summary.hunk_count 1
    input_summary.files      ["commands.py"]
    input_summary.digest     一串 hash

digest 是输入内容的摘要。它不是问题数量，而是用来判断这次输入是否和历史
输入完全一致。缓存只有在 digest、review profile、规则和范围等条件匹配时才
应该复用。

只查看最终 finding：

    jq '.analysis.findings[] | {
      severity,
      category,
      file,
      line,
      title,
      evidence,
      recommendation,
      confidence,
      source
    }' "$REVIEW_REPORT"

再查看所有需要谨慎处理的内容：

    jq '{
      warnings: .analysis.warnings,
      needs_human_review: .analysis.needs_human_review
    }' "$REVIEW_REPORT"

以 security fixture 为例，正常的 Fake 结果会出现类似语义：

    category: security
    file: commands.py
    line: 4
    evidence: return os.system(user_input)
    confidence: 0.96

数字可能因为代码版本变化而不同，应该重点看字段含义。source 表示这个结果
来自哪里；例如 skill:review_security.py 表示它来自 Skill 规则脚本，而不是
模型凭空生成。

### 3.4 再看“过程字段”：Filter、Sandbox 和 Monitoring

最终 finding 只能回答“发现了什么”，过程字段才能回答“它是怎么得到的”。

查看 Filter 决策：

    jq '.filter_decisions[] | {
      decision,
      command,
      reason
    }' "$REVIEW_REPORT"

decision 常见有三种：

    allow
      请求通过策略，可以进入后续执行。
    deny
      请求违反策略，不能进入 Sandbox。
    needs_human_review
      不能安全地自动放行，需要人工决定。

查看 Sandbox 运行：

    jq '.sandbox_runs[] | {
      status,
      command,
      duration_ms,
      exit_code,
      timed_out,
      output_truncated,
      error_type,
      stderr_summary
    }' "$REVIEW_REPORT"

SandboxRun 是一次执行尝试的审计摘要。它可能是：

    simulated
      Fake 或 dry-run 模式中的模拟成功。
    success
      真实 Sandbox 执行成功。
    blocked
      Filter 已经拒绝，所以没有真正执行。
    failed
      Sandbox 启动或命令执行失败。
    timeout
      超过时间预算。

最后看监控摘要：

    jq '.monitoring' "$REVIEW_REPORT"

最值得先理解的字段是：

| 字段 | 含义 |
| --- | --- |
| total_duration_ms | 整个 Workflow 花费的时间 |
| sandbox_duration_ms | Sandbox 执行所花的时间 |
| tool_call_count | Agent 事件中观察到的 function call 数量 |
| blocked_count | 被 Filter 阻止或要求人工复核的决策数量 |
| finding_count | 治理后进入 findings 的问题数量 |
| severity_distribution | 按严重级别统计的问题数量 |
| exception_distribution | Sandbox 或工具异常类型统计 |

注意 tool_call_count 和 sandbox_runs 不是一回事。一次 Agent 会话可能调用
skill_load、skill_select_docs 和 skill_run；它们都会算作工具调用，但只有
实际尝试 skill_run 才会对应 SandboxRun。另一方面，被 Filter 阻止的
skill_run 也会留下 SandboxRun，状态是 blocked，因为它需要被审计。

### 3.5 用 SQLite 看同一个 task 的持久化记录

报告是文件出口，SQLite 是查询和复盘出口。先看表：

    sqlite3 "$REVIEW_LAB_DIR/reviews.sqlite3" ".tables"

你会看到与这些职责对应的表：

    review_tasks
    review_inputs
    sandbox_runs
    filter_decisions
    findings
    monitoring_summaries
    review_reports

先查任务总体状态：

    sqlite3 -header -column "$REVIEW_LAB_DIR/reviews.sqlite3" \
      "select task_id, status, scope, created_at, completed_at
         from review_tasks
        order by created_at desc
        limit 5;"

再查本次任务的工具策略决策：

    sqlite3 -header -column "$REVIEW_LAB_DIR/reviews.sqlite3" \
      "select decision, command, reason
         from filter_decisions
        where task_id = '$REVIEW_TASK_ID';"

查 Sandbox：

    sqlite3 -header -column "$REVIEW_LAB_DIR/reviews.sqlite3" \
      "select status, command, duration_ms, exit_code, error_type
         from sandbox_runs
        where task_id = '$REVIEW_TASK_ID';"

查最终问题：

    sqlite3 -header -column "$REVIEW_LAB_DIR/reviews.sqlite3" \
      "select bucket, severity, category, file, line, confidence
         from findings
        where task_id = '$REVIEW_TASK_ID'
        order by bucket, file, line;"

这里的 bucket 是结果分桶：

    finding              经过治理、可以直接展示
    warning              置信度或证据不足
    needs_human_review   需要人工确认或补充检查

如果 JSON、Markdown 和 SQLite 对同一个 task 的结果不一致，应优先怀疑报告写入、
数据库写入或读取代码，而不是先怀疑模型。三种出口都使用同一个经过校验的
ReviewReport，正常情况下应该保持语义一致。

### 3.6 从 Tool Use 到 trajectory：完整看懂一次工具调用

你提到的 trajority 通常写作 trajectory，中文可以叫“执行轨迹”。
它不是模型的隐藏思维过程，而是外部可以观察和审计的动作序列：

    请求
      -> Agent 判断缺少证据
      -> function_call 请求工具
      -> Filter 做策略决策
      -> Skill 或 Sandbox 执行
      -> function_response 返回结果
      -> Agent 选择继续、重试、换工具或结束
      -> Workflow 生成最终报告

Tool Use 是其中的“使用工具进行决策和行动”；trajectory 是把多次 Tool Use、
状态变化和结果按时间顺序串起来。

一次真实模式的轨迹可能类似：

    1. Agent 请求 skill_list 或 skill_list_docs
       先了解可用的审查能力。
    2. Agent 请求 skill_load
       读取 code-review Skill 的操作说明。
    3. Agent 请求 skill_run
       让 Skill 在 Sandbox 中运行指定规则脚本。
    4. Filter 检查命令、脚本、路径、输入模式和预算。
    5. Sandbox 返回 stdout、stderr、退出码或超时。
    6. Agent 读取规则结果，决定是否继续分页或补充检查。
    7. Agent 写入结构化 ReviewAnalysis。
    8. Workflow 对文件范围、行号、去重、置信度和敏感信息做最终治理。

这里的“可能”很重要：具体调用顺序由模型决定，但它不能越过 SAFE_SKILL_TOOLS、
require_skill_loaded、Filter 和 Sandbox。

当前报告能够直接展示 trajectory 的一部分：

| 轨迹事实 | 从哪里看 |
| --- | --- |
| Agent 观察到多少次 function call | monitoring.tool_call_count |
| 哪些请求被允许、拒绝或转人工 | filter_decisions |
| 哪些 skill_run 进入或没进入 Sandbox | sandbox_runs |
| 工具失败是否影响最终结论 | analysis.needs_human_review、conclusion |
| 结果来自哪个规则或组件 | finding.source、checks_performed |

当前实现没有把每一个原始 Agent Event 完整保存到最终报告，所以报告是一份
“可审计摘要”，不是完整事件日志。要深挖真实 trajectory，应沿着这条源码路径：

    workflow.py::_run_agent()
      -> runner.run_async(...)
      -> event.content.parts
      -> part.function_call
      -> pending_runs[call_id]
      -> part.function_response
      -> _sandbox_run_from_response(...)
      -> monitoring 和 ReviewReport

在 function_call 分支中，Workflow 统计调用次数，并记下 skill_run 的
command 和 call id；在 function_response 分支中，它按 call id 找回请求，
计算耗时、退出码、超时和输出，再生成 SandboxRun。这个配对关系就是理解
异步工具调用的关键。

Fake 模式的轨迹要特别解释：

    FakeSandbox.run(...)
      -> 直接调用 CommandPolicy.evaluate(...)
      -> 返回 simulated、blocked、failed 或 timeout
      -> Workflow 之后调用 analyze_with_fake_model(...)

所以 Fake 模式中的 tool_call_count: 1 是 Workflow 的模拟审计数，并不表示
真实模型刚刚选择了一个工具。要研究模型如何选择工具，必须运行真实模式，或者
在测试中直接构造 Agent、ToolSet 和 InvocationContext。

### 3.7 第一次观察真实 Tool Use

真实模式需要完成三件事：

    1. .env 使用 0600 权限，并填入真实模型配置
    2. 模型服务的 base URL、模型名和 API Key 可用
    3. Docker daemon 正常运行，且能构建或找到审查镜像

确认配置后运行：

    .venv/bin/python examples/skills_code_review_agent/run_agent.py \
      --fixture security \
      --trace \
      --output-dir "$REVIEW_LAB_DIR/real-reports" \
      --database "$REVIEW_LAB_DIR/real-reviews.sqlite3"

真实模式不要添加 --fake-model。--trace 会把经过脱敏和长度限制的执行轨迹打印到
stderr；普通的报告路径仍然打印到 stdout。要同时在屏幕查看并保存轨迹，可以运行：

    .venv/bin/python examples/skills_code_review_agent/run_agent.py \
      --fixture security \
      --trace \
      --output-dir "$REVIEW_LAB_DIR/real-reports" \
      --database "$REVIEW_LAB_DIR/real-reviews.sqlite3" \
      2>&1 | tee "$REVIEW_LAB_DIR/trajectory.log"

### 3.7 现在优先阅读统一 Run Trace

使用 `--trace` 后，每个任务目录中有三个主要入口：

    run_trace.md       人类阅读入口：从输入到模型、工具、沙箱和最终报告
    run_trace.json     同一条执行链的机器可读事实源
    review_report.md   最终交付给代码作者的审查结论

推荐先打开 `run_trace.md`，按下面顺序阅读：

    1. Input
       本次究竟审查了什么，diff 摘要和文件范围是什么

    2. Model and Tool Rounds
       每次 Exact SDK request 是模型该轮真实收到的请求
       Terminal SDK response 是模型该轮真实返回的最终响应
       紧随其后的 Tool 小节把模型请求、Filter、Sandbox 和返回值关联起来

    3. Output Transformation
       raw_set_model_response_arguments 是模型提交的原始结构化参数
       sdk_validated_agent_output 是 SDK schema 校验后的结果
       normalized_report_output 是 Workflow 校验范围、去重和脱敏后的结果

    4. Backend Lifecycle
       展示输入解析、缓存、执行、报告写入和存储等后端步骤

    5. Monitoring
       展示耗时、调用次数、拦截次数和异常分布

`model_io.json`、`agent_context.json` 和 `trajectory.log` 暂时保留兼容，适合定位底层问题；
日常学习不再需要手工把它们拼起来。尤其要记住：模型 I/O、工具执行和最终报告是
三个不同边界，`run_trace` 的作用就是用 call_id 和 model_call_index 将它们连接起来。

正常情况下，你会看到类似的阶段：

    [trace] run.started ... mode='real'
    [trace] input.parsed ...
    [trace] execution.selected ... mode='real-agent'
    [trace] agent.started ...
    [trace] tool.requested ... tool='skill_load'
    [trace] tool.responded ...
    [trace] tool.requested ... tool='skill_run' command='python3 scripts/run_review_rules.py ...'
    [trace] policy.decision ... decision='allow'
    [trace] tool.responded ... exit_code='0'
    [trace] agent.completed ... structured_result='True'
    [trace] report.written ...
    [trace] storage.saved ...

具体调用次数和顺序由模型决定；如果模型认为缓存或已有证据足够，也可能少调用工具。
trace 只记录工具名、调用 ID、命令摘要、策略结果、退出码、耗时和输出长度，不记录
完整 Prompt、完整 diff 或完整工具输出。它是审计摘要，不是模型隐藏思维过程。

观察完终端轨迹后，再查看 JSON 报告：

    REAL_REPORT=$(find "$REVIEW_LAB_DIR/real-reports" \
      -name review_report.json -type f | head -n 1)

    jq '{
      task_id,
      status,
      tool_call_count: .monitoring.tool_call_count,
      blocked_count: .monitoring.blocked_count,
      filter_decisions: .filter_decisions,
      sandbox_runs: .sandbox_runs
    }' "$REAL_REPORT"

然后回答：

    tool_call_count 是否大于 0？
    是否先加载了 Skill？
    skill_run 是否通过 Filter？
    Sandbox 是否实际运行？
    是否有分页、重试或重复调用？
    最终 finding 是否引用了工具返回的证据？

如果真实模式失败，不要只看最后一行异常。按这个顺序排查：

    .env 权限和变量名
      -> 模型连接和 TLS
      -> Docker daemon 和镜像
      -> Filter decision 的 reason
      -> sandbox_runs 的 status、stderr_summary、error_type
      -> needs_human_review 和 task status

这正是可观测性设计的价值：失败也应该告诉你失败发生在哪个边界。

### 3.8 用测试逐层深挖，而不是一开始调真实模型

真实模型带来网络、模型输出和 Docker 等变量，不适合作为第一个调试对象。
先运行确定性测试：

    .venv/bin/python examples/skills_code_review_agent/tests/run_tests.py

再运行 fixture 评测：

    .venv/bin/python examples/skills_code_review_agent/tests/evaluate_fixtures.py

阅读测试时，按“观察什么”来找：

    test_governed_toolset_is_lazy_and_hides_workspace_exec
      观察工具白名单、workspace_exec 隐藏和 Docker 惰性创建。

    test_governed_skill_run_blocks_before_runtime_initialization
      观察 Filter 拒绝后不会创建 Sandbox。

    test_partial_agent_audit_survives_structured_output_failure
      观察 Agent 失败时，已经发生的工具和 Filter 记录仍被保存。

    test_sandbox_failure_does_not_abort_report
      观察 Sandbox 失败如何变成 completed_with_warnings 和人工复核项。

    test_dry_run_executes_complete_non_network_chain
      观察不请求模型、不执行代码时，仍然可以验证完整报告链路。

测试名称本身就是设计文档。每读到一个断言，都问自己：

    它在保护哪个安全边界？
    它在保护哪个数据契约？
    如果删掉这段代码，用户会看到什么错误？
    这个行为是在输入、工具、Filter、Sandbox 还是结果治理层保证的？

### 3.9 初学者的五层学习路线

不要一次打开所有目录。用同一个 security 任务逐层增加观察深度：

    第 1 层：只看 Markdown
      能说出 status、finding、warning 和 conclusion 的区别。

    第 2 层：看 JSON
      能根据 input_summary、analysis 和 monitoring 解释结果。

    第 3 层：看审计过程
      能根据 filter_decisions 和 sandbox_runs 判断工具有没有真正执行。

    第 4 层：看源码
      能从 _run_agent 的 function_call/function_response 配对追到报告字段。

    第 5 层：看测试和实验
      能故意制造拒绝、超时或 Sandbox 失败，并解释系统为什么没有伪造成功。

达到第 5 层后，再进入后面的自主 Agent 改造计划。因为只有先会读一条现有
trajectory，才能知道新增 Tool Use、Memory、Checkpoint 后系统究竟改变了什么。

## 4. 跟着一次审查走完整主线

下面按运行时顺序阅读。每一层都回答两个问题：

    这一层收到什么？
    为什么必须有这一层？

### 4.1 CLI：把命令行变成请求

入口是 [run_agent.py](../run_agent.py) 的 main 和 run。
CLI 是 Command Line Interface 的缩写，意思是命令行界面。

它负责：

    读取参数
      -> 加载受限的 .env
      -> 选择 Fake 或真实模式
      -> 创建 Store、ReportWriter、Sandbox 和 Workflow
      -> 构造 ReviewRequest

CLI 不解析 diff，也不决定命令安全。这样同一个 Workflow 可以被命令行和测试
共同使用。

### 4.2 输入解析：把不同来源变成同一种对象

当前输入来源有：

    diff_file       外部 unified diff 或 PR patch
    file_list       仓库相对路径列表
    fixture         tests/fixtures 中的固定样例
    repository_path Git 工作区

ReviewRequest 表达用户想审查什么；ParsedReviewInput 是经过验证后的详细证据。

解析器负责：

- 检查输入是否存在并限制大小；
- 拒绝绝对路径、路径穿越和符号链接；
- 拒绝明显的 Secret 路径；
- 解析文件、hunk、行号和变更行；
- 生成摘要和 digest；
- 为外部 diff 准备任务级临时目录。

这一步叫归一化：不同输入最终转换成一种内部形状，Workflow 不必为每种输入
复制一条完全不同的主流程。

### 4.3 Workflow：让任务诚实地完成或失败

入口是 [workflow.py](../workflow.py) 的 CodeReviewWorkflow。
它是确定性控制层，不替 Agent 理解代码，但控制 Agent 的生命周期和边界。

    start_task(running)
      -> parse input
      -> 计算 review profile，查询精确缓存
      -> Fake 或真实 Agent
      -> 收集工具、Filter 和 Sandbox 事件
      -> 检查证据是否完整
      -> 校验模型结果范围
      -> normalize + redact
      -> 创建 ReviewReport
      -> 写报告
      -> 保存数据库

为什么一开始就保存 running？

因为“没有返回报告”不等于“没有问题”。可能是：

    输入解析失败
    Docker 无法启动
    工具被 Filter 拒绝
    Agent 超时
    结构化输出缺失
    报告或数据库写入失败

这些情况都要留下记录。最终状态是：

    completed               成功完成且没有治理警告
    completed_with_warnings 完成了，但有警告、人工复核或证据缺口
    failed                  执行或最终化失败

### 4.4 Agent：负责理解和决定，不负责获得无限权限

真实模式在 [agent/agent.py](../agent/agent.py) 创建 LlmAgent。
它得到模型、系统指令、受治理的 Skill 工具集和 ReviewAnalysis 输出结构。

Agent 的职责是：

    理解审查范围
      -> 判断需要哪些证据
      -> 按 Skill 说明请求工具
      -> 阅读返回的候选和变更行
      -> 生成结构化 ReviewAnalysis

Agent 不应直接访问宿主机文件，也不能把任意字符串当作 shell 命令执行。
这里刻意分离“推理”和“权限”：模型可以提出请求，但不能自己批准请求。

### 4.5 Tool：Agent 和外部能力之间的接口

工具调用就是模型发出的结构化请求。例如：

    name: skill_run
    arguments:
      skill: code-review
      command: python3 scripts/run_review_rules.py work/inputs/security.diff

项目只暴露受控工具：

    skill_list
    skill_list_docs
    skill_load
    skill_run
    skill_select_docs
    sandbox_policy_info

通用 workspace 执行工具被隐藏，skill_run 还要求先完成 skill_load。
因此 Agent 只能请求“加载审查说明”和“运行被治理的 Skill 命令”。

### 4.6 Skill：把审查方法说清楚

Skill 位于 [skills/code-review/](../skills/code-review/)：

    SKILL.md             给 Agent 的操作说明
    references/RULES.md  规则的含义和分类标准
    scripts/             实际执行的解析、读取和审查脚本

它规定：

- diff 输入使用聚合规则脚本；
- file list 输入先验证列表，再读取声明文件；
- Git 输入使用指定的 Git helper；
- 大结果沿 next_cursor 继续分页；
- 脚本结果只是候选证据；
- 证据不完整时不能声称完成全量审查。

三者的关系是：

    Skill：告诉 Agent 应该怎样完成任务
    Filter：决定请求实际上能不能执行
    Sandbox：执行已经获准的请求

### 4.7 Filter：真正的执行闸门

Filter 位于 [filters/sdk_filter.py](../filters/sdk_filter.py)，策略位于
[filters/policy.py](../filters/policy.py)。

一次 skill_run 大致经过：

    参数是对象吗？
      -> 字段是否在允许集合？
      -> Skill 是否是 code-review？
      -> 是否带有禁止的 staging 或 output 参数？
      -> Sandbox run 次数是否超限？
      -> 顶层命令、脚本、路径和参数是否合法？
      -> 网络、环境变量、timeout 和输出是否符合预算？
      -> 当前输入模式是否允许这条精确命令？

自动允许的顶层命令只有 git、python3 和 pytest，但还要继续检查脚本、
子命令、路径和输入模式。仅仅因为命令以 python3 开头，并不代表可以执行。

Filter 拒绝后不进入 Sandbox，拒绝本身也写入审计记录。因此报告可以区分：

    检查没有发现问题

和：

    检查没有执行，因为请求被安全策略阻止

### 4.8 Docker Sandbox：隔离不可信代码

Sandbox 位于 [sandbox/](../sandbox/)。真实容器会：

    禁用网络
    使用只读根文件系统
    以非 root 用户运行
    删除 capabilities
    设置 no-new-privileges
    限制内存、CPU、PID 和临时文件系统
    只读挂载 repository 和 Skill 输入

LazySandboxRuntime 的意义是：

    创建工具对象时：不启动容器
    Filter 放行且确实执行时：才创建容器

所以危险请求会在容器创建前被挡住。

BoundedProgramRunner 还会在容器内限制命令时间和输出大小，分别处理
stdout 和 stderr，脱敏后再按字节截断。输出过大时必须标记截断，不能让
Agent 误以为自己看到了完整结果。

### 4.9 Agent 事件和结构化结果

Workflow 会监听 Agent 的异步事件：

    function_call
      -> 记录工具名、命令和 call id

    function_response
      -> 按 id 找回对应请求
      -> 计算耗时、退出码、超时和输出
      -> 生成 SandboxRun

Agent 最终结果必须进入 session.state 的 review_analysis 字段，并通过
ReviewAnalysis 结构校验。工具执行成功但结构化结果缺失，仍然是 Agent 失败。

### 4.10 结果治理：不要盲信模型

模型可能返回格式正确、但证据错误的结果：

    file = unrelated.py
    line = 999

enforce_analysis_scope 会检查文件是否属于选中输入；diff 和 fixture 模式
还会检查行号是否属于真实变更行。越界结果被丢弃，并追加人工复核项。

normalize_analysis 接着：

    按 file、line、category 去重
    保留置信度更高的结果
    低于 0.70 的结果放入 warnings
    统一未知行号
    对自由文本脱敏

必须区分：

    candidate         规则脚本发现的可能问题
    finding           经过证据和范围治理后可以展示的问题
    warning           结果存在，但置信度或证据不足
    needs_human_review 必须由人确认或补充检查

### 4.11 报告和数据库：交付结果并保留过程

ReportWriter 生成：

    review_report.json  便于程序读取
    review_report.md    便于人阅读

写入时会拒绝符号链接目录，使用私有权限，并通过临时文件、flush、fsync
和原子替换发布。模型控制的文本还要进行 Markdown 转义。

Storage 层通过 [storage/base.py](../storage/base.py) 抽象，保存：

    review_tasks
    review_inputs
    sandbox_runs
    filter_decisions
    findings
    monitoring_summaries
    review_reports

SQLite 和 PostgreSQL 都应实现：

    initialize()
    start_task()
    mark_task_failed()
    save()
    get()
    get_latest_by_input_digest()
    get_task_details()

save 要有事务性和幂等性，并在进入数据库前脱敏。这样不仅能看最终 finding，
还能复盘任务是否被拒绝、执行了几次、是否超时以及输出是否截断。

## 5. 为什么不能合并成一个大模块

前面每一层的存在原因可以归纳为四个词：

    职责分离
    权限分离
    故障隔离
    替换能力

职责分离意味着：解析器回答“输入是什么”，Agent 回答“怎样理解”，规则回答
“有什么候选”，Workflow 回答“任务是否完整、怎样收尾”。

权限分离意味着：Agent 可以请求工具，但不能批准命令；Filter 可以批准命令，
但不执行代码；Sandbox 执行代码，但不决定审查结论。

故障隔离意味着：规则失败不能抹掉任务记录，Docker 超时不能静默变成“无问题”，
报告写入失败不能让数据库继续显示成功。

替换能力意味着：

    FakeSandbox             用于测试
    DockerSandbox           用于真实隔离执行
    SQLiteReviewStore       用于本地持久化
    PostgreSQLReviewStore   用于服务化持久化

它们可以替换实现，但必须遵守相同的安全和数据契约。

## 6. 遇到问题时，按问题找源码

| 问题 | 先看哪里 |
|---|---|
| CLI 为什么启动失败？ | run_agent.py、.env 权限和配置 |
| 输入为什么没有被审查？ | inputs/parser.py、Workflow 的输入选择 |
| Agent 为什么没有调用工具？ | agent/prompts.py、缓存和 Session state |
| 工具为什么被拒绝？ | filters/sdk_filter.py、filters/policy.py |
| Docker 为什么没有启动？ | Filter 决策、LazySandboxRuntime、factory |
| finding 为什么消失？ | agent/normalization.py |
| finding 为什么变成 warning？ | confidence 和 normalize_analysis |
| 报告为什么没有 Secret？ | security.py、ReportWriter、Storage |
| 任务为什么是 warning？ | Workflow 的完整性检查和运行状态 |
| 数据库为什么重复？ | save、records.py、事务和稳定 ID |
| 新规则为什么没生效？ | 聚合器 RULES、SKILL.md、allowlist、fixture |

这种按问题导航的方式，比从每个目录独立阅读更容易建立因果关系。

## 7. 如何扩展：沿着数据流修改

### 7.1 新增一条规则

以 review_performance.py 为例：

    规则脚本
      -> 注册到 run_review_rules.py
      -> 更新 RULES.md
      -> 检查 Filter 脚本 allowlist
      -> 添加风险和清洁 fixture
      -> 添加规则、聚合器和 Workflow 测试
      -> 验证分页、去重、scope 和脱敏

规则只产生 candidate，不应自己写报告、访问数据库或执行用户代码。
复杂控制流无法被正则可靠证明时，应降低置信度或交给人工复核。

### 7.2 新增一种输入

先问：这是新的传输方式，还是新的审查语义？

如果只是把 diff 从 stdin 传入，通常可以复用 diff_file 语义：

    CLI 参数
      -> ReviewRequest
      -> parser
      -> ParsedReviewInput
      -> Policy context
      -> Prompt 和 Skill 分支
      -> Workflow 完整性检查
      -> 缓存
      -> 测试

必须测试输入大小、临时目录、文件权限、清理和冲突参数。

### 7.3 新增存储后端

实现 BaseReviewStore 的全部方法，并验证：

    事务性
    幂等性
    参数化 SQL
    错误信息脱敏
    规范化明细查询
    完整报告快照
    缓存查询

不能只让 save 工作，还要验证任务失败、重复保存、旧数据读取和查询接口。

### 7.4 新增 Sandbox 后端

实现 SandboxProvider.create_runtime，并保持 Runtime 的：

    manager()
    fs()
    runner()
    describe()
    close()

重新验证只读、禁网、非 root、timeout、输出上限、清理和“拒绝前不初始化”。
换执行平台不等于可以取消这些约束。

### 7.5 新增报告字段

必须同步：

    Pydantic 模型
      -> Workflow 构造
      -> JSON 和 Markdown Writer
      -> SQLite schema、写入和读取
      -> PostgreSQL schema、写入和读取
      -> 旧数据兼容
      -> cache profile
      -> 测试

新增字段可以使用默认值兼容旧 JSON；数据库新增列则必须考虑迁移。

## 8. 测试：每个命令证明什么

### 8.1 确定性测试

    /home/chan/trpc-agent-python/.venv/bin/python \
      examples/skills_code_review_agent/tests/run_tests.py

主要验证解析器、规则、Fake Workflow、Filter、规范化、报告、SQLite 和失败路径。
它不能单独证明真实 Docker 隔离、PostgreSQL 连接或模型质量。

### 8.2 Fixture 指标评测

    /home/chan/trpc-agent-python/.venv/bin/python \
      examples/skills_code_review_agent/tests/evaluate_fixtures.py

关注高风险检测率、clean diff 误报率、敏感信息检测/脱敏率，以及必需 fixture
是否生成 JSON 和 Markdown。

### 8.3 Docker 集成测试

    python examples/skills_code_review_agent/tests/run_docker_tests.py

验证真实容器的 Skill 加载、Filter、分页、只读挂载、禁网、timeout、输出限制
和安全配置。没有 Docker daemon 时，这一层只能标记为未验证。

### 8.4 PostgreSQL 存储契约

    CODE_REVIEW_POSTGRES_DSN='postgresql://review_agent:<password>@127.0.0.1:5432/code_reviews' \
      uv run --project examples/skills_code_review_agent \
      --extra postgresql --with-editable . \
      python examples/skills_code_review_agent/tests/run_postgres_tests.py

只对专用测试数据库执行。它验证 schema、往返读取、幂等保存、缓存查询、失败
审计、规范化明细和脱敏。

## 9. 安全审计：每次修改都问这些问题

    1. 输入有没有大小、数量和格式限制？
    2. 路径是否拒绝绝对路径、.. 和符号链接？
    3. 是否可能读取 .env、Private Key 或其他 Secret？
    4. Agent 是否获得了不必要的工具？
    5. skill_run 是否先经过 Filter？
    6. Filter 是否在 Sandbox 初始化前执行？
    7. 新命令是否使用最小 allowlist？
    8. 网络、超时、进程、内存和输出是否有界？
    9. finding 是否对应选中输入和真实变更行？
   10. Secret 是否在工具输出、报告、数据库和异常中都脱敏？
   11. 失败、拒绝、超时和分页中断是否可审计？
   12. SQLite 和 PostgreSQL 是否保持同一契约？
   13. 是否同时添加成功测试和拒绝/失败测试？

安全审计的核心不是只找一行危险代码，而是确认新功能没有绕过原有信任边界。

## 10. 综合练习：实现 stdin diff 和类别统计

以下功能当前源码尚未实现，适合用来检验全链路理解。

### 10.1 stdin diff

建议把 stdin 看成新的传输方式，而不是新的审查语义：

    stdin
      -> 有界读取，最多 MAX_INPUT_BYTES
      -> 任务级临时目录
      -> stdin.diff，文件权限 0400
      -> 临时目录权限 0500
      -> 复用 parse_diff_text()
      -> summary.kind 使用 diff_file
      -> 复用现有 diff Prompt、Skill、Filter 和分页

至少测试合法输入、超限输入、和其他输入冲突、--full 冲突、临时目录清理，
以及 Secret 不进入报告和数据库。

### 10.2 category_distribution

如果统计最终类别，应在 scope 校验、去重和置信度分桶之后计算：

    findings
      + warnings
      + needs_human_review

可能的改动链：

    MonitoringSummary
      -> Workflow._build_monitoring()
      -> JSON 和 Markdown
      -> SQLite schema、写入和读取
      -> PostgreSQL schema、写入和读取
      -> 旧数据迁移
      -> 一致性测试

要先明确 finding_count 是否只统计确定性 findings，不能让总数和类别统计的
语义互相矛盾。

## 11. 推荐学习顺序

    第一步：运行 security fixture，先读报告
    第二步：读 workflow.py，画出主流程
    第三步：读 parser.py，理解 diff、Git 和 scope
    第四步：读 agent/tools.py、prompts.py，理解工具调用
    第五步：读 SKILL.md 和 RULES.md，理解证据来源
    第六步：读 sdk_filter.py 和 policy.py，理解为什么请求会被拒绝
    第七步：读 sandbox/docker.py，理解隔离和资源治理
    第八步：读 normalization.py、security.py 和 writers.py
    第九步：读 storage/，理解报告如何查询和复盘
    第十步：读 tests/，把测试映射回设计约束
    第十一步：新增规则或输入，并完成全链路验收

最后记住四个问题：

    它是否只执行了被批准的事情？
    它引用的证据是否真实、完整、可定位？
    它失败时是否留下了诚实的审计结果？
    它是否把敏感信息带到了下一个边界？

这四个问题把 CLI、解析、Agent、工具、Filter、Skill、Sandbox、规范化、报告、
存储和测试重新连接成了一个整体。

## 12. 下一步改进计划：从 Skill Agent 到确定性代码审查 Agent

前面的章节解释了当前示例如何完成一次代码审查。本章不把 OCR 当成需要照搬的
实现，而是提取它最值得借鉴的系统方法：让确定性工程负责范围、调度、上下文边界、
评论定位和结果治理，让 Agent 负责代码理解、证据探索和风险判断。

OpenCodeReview 的设计可以概括为：规则驱动分派、按文件或文件组审查、受限工具探索、
上下文记忆压缩，以及独立的评论反思。[OpenCodeReview README](https://github.com/alibaba/open-code-review#readme)
和[论文](https://arxiv.org/html/2608.09290v2)分别介绍了项目能力和这些设计背后的动机。

这一点会改变本教程后续改进的重点：目标不是让 Agent 无限自主，而是把它放进一个
可控、可恢复、可解释、可评测的审查流水线。

### 12.1 先区分四种上下文和记忆

“记忆”是本章最容易混淆的词。OCR 中的记忆压缩主要解决一次审查内上下文不断增长
的问题；它不是把经验永久写进 MemoryService。

| 概念 | 保存什么 | 生命周期 | 主要目的 |
| --- | --- | --- | --- |
| 工作上下文 | 当前 diff、工具结果、候选 finding | 当前 Agent 运行 | 支持下一轮推理 |
| 会话历史 | 消息、工具调用、事件和状态 | 一个 run 或 session | 还原本次审查过程 |
| 检查点 | 某一时刻的可恢复状态 | 跨进程保留 | 中断后继续执行 |
| 长期记忆 | 已确认的仓库经验和人工反馈 | 跨多次审查 | 减少重复探索、降低误报 |
| 缓存 | 完全相同输入的已有结果 | 按缓存策略保留 | 节省时间和成本，不等于记忆 |

因此，以下两件事不能混为一谈：

    工作记忆压缩
      把当前运行中过长的消息和工具轨迹整理成可追溯摘要

    长期记忆写入
      从已确认结果和人工反馈中提取跨任务仍然有效的经验

当前示例已经有 Agent 事件、报告持久化和 Skill/Sandbox 边界，但还没有 OCR 意义上的
文件级审查调度、细粒度代码探索工具、上下文压缩器和独立 Reflection。文档后续应始终
用“当前实现”和“计划能力”两个标签，避免把设计目标误写成已有功能。

### 12.2 目标架构：确定性内核包住 Agent

当前项目的主线是：

    parser.py
      -> workflow.py 创建审查任务
      -> Agent 通过 function call 请求 Skill
      -> Filter 检查请求
      -> Sandbox 执行被批准的脚本
      -> Agent 返回结构化结果
      -> normalization.py / security.py 治理结果
      -> reports/ 和 storage/ 输出、保存、查询

参考 OCR 后，目标主线应扩展为：

    ReviewRequest
      -> Deterministic Review Kernel
           解析 diff、过滤文件、解析规则、生成审查范围
      -> Diff Dispatcher
           按文件或语义相关文件组分派任务，并发执行
      -> Grounded Agent Loop
           只使用受限工具读取代码、搜索证据和提交评论
      -> Context Manager
           管理 Frozen、Compressible、Active 三类上下文
      -> Comment Post-Processor
           处理行号定位、建议校验和评论去重
      -> Independent Reflection
           只过滤无法被当前 diff 支持的评论
      -> Workflow / Evaluation Recorder
           记录事件、检查点、结果、轨迹、耗时和 token
      -> Report / ReviewStore / MemoryService
           分别负责交付、查询和经过筛选的长期经验

一次未来的审查应该沿着下面的路径运行：

    1. parser.py 生成稳定的 ReviewRequest 和 input_digest
    2. 调度内核确定 diff、规则、文件和预算
    3. Dispatcher 创建一个或多个文件审查任务
    4. Agent 在每个任务内使用有边界的工具循环
    5. Context Manager 监控上下文并按需压缩工作记忆
    6. Comment Post-Processor 修正位置并关联证据
    7. Reflection 尝试证伪候选评论
    8. Workflow 做范围校验、去重、脱敏和降级
    9. Recorder 保存结果、轨迹、压缩统计和恢复信息
   10. 只有经过筛选的经验才进入长期记忆

“自主决策”仍然必须定义为：

    在批准的工具集合、仓库范围、资源预算和停止条件内选择下一步动作

Filter、Sandbox 和结果治理不会因为 Agent 更自主而被绕过。

### 12.3 分阶段路线图

建议按照以下依赖关系推进。每一阶段都要有可验收结果，完成前一阶段后再扩大
Agent 的自由度。

| 阶段 | 主要目标 | 主要代码落点 | 完成标志 |
| --- | --- | --- | --- |
| 0 | 固化基线和数据契约 | workflow.py、reports/、storage/、tests/ | 同一输入的结果、错误和安全边界可比较 |
| 1 | 确定性审查内核 | inputs/、workflow.py、skills/、新增 dispatcher/ | 文件范围、规则和排除原因可解释 |
| 2 | 受限工具和文件组 Agent | agent/、filters/、sandbox/、新增 tools/ | 工具、分组、并发和预算都有边界 |
| 3 | 工作记忆与上下文压缩 | agent/ 或新增 context/、storage/ | 长工具轨迹不会无界增长，关键证据可追溯 |
| 4 | 评论定位和独立 Reflection | agent/normalization.py、reports/、新增 reflection/ | 错位、重复和无法被 diff 支持的评论可治理 |
| 5 | 可观测性和检查点恢复 | workflow.py、storage/、新增 observability/、recovery/ | 可以解释、暂停并恢复一次审查 |
| 6 | 长期记忆 | 新增 memory/，适配 tRPC-Agent MemoryService | 记忆按范围读取、筛选写入、可过期和撤销 |
| 7 | 有边界的自主循环 | workflow.py、agent/、recovery/ | Agent 能连续收集证据但不能越过安全边界 |
| 8 | 完整评测和生产治理 | 新增 evaluation/、tests/eval/、部署配置 | 质量、效率、可靠性和安全可以一起比较 |

顺序上的关键变化是：先解决审查上下文和结果可靠性，再接入跨任务长期记忆。否则
Memory 很容易只是让 Prompt 变长，却无法证明它让审查变好。

### 12.4 阶段 0：固定当前基线

当前项目已经有 Fake Agent、fixture、Docker 测试、存储测试和结果治理逻辑。首先
应把这些能力固定成可重复的 baseline：

    输入
      -> 原始 diff、fixture 或仓库状态
    输出
      -> findings、warnings、needs_human_review
    约束
      -> scope、Filter、Sandbox、脱敏、分页和错误降级
    记录
      -> 输入摘要、运行配置、工具调用、最终状态和报告摘要

建议固定三个基础数据对象：

    ReviewRun
      run_id、session_id、input_digest、repository_ref、review_profile、status、budget

    RunEvent
      event_id、run_id、seq、event_type、phase、payload、occurred_at、redaction_status

    Checkpoint
      run_id、checkpoint_id、last_event_seq、phase、state_snapshot、pending_tool_call

至少要能回答：

    同一 fixture 是否产生相同的审查语义？
    Fake Agent 和真实 Agent 是否经过同一套结果治理？
    工具失败、超时、拒绝和输出截断是否可区分？
    报告中的证据是否能追溯到 diff、Skill 或 Sandbox 输出？
    缓存、会话、检查点和长期记忆是否没有被混用？

### 12.5 阶段 1：建立确定性审查内核

OCR 的第一个核心经验是：不要让 LLM 决定“应该审查哪些文件”。这部分应由程序
先完成，再把确定的审查任务交给 Agent。

建议依次实现：

    Git diff / commit / workspace / scan path
      -> 解析文件、hunk、旧行和新行
      -> 过滤二进制、生成文件、超大 diff 和明确排除项
      -> 解析命令行、项目、用户和系统规则
      -> 输出审查范围和排除原因
      -> 为每个文件注册覆盖状态

规则需要记录来源和优先级。对于每个文件，系统应该能解释：

    为什么这个文件被审查？
    使用了哪条规则？
    为什么另一个文件被排除？
    是否因为 diff 太大、类型不支持或范围冲突而跳过？

验收标准：

    相同输入和规则得到相同的文件范围
    预览结果不需要调用模型
    排除原因可以写入报告和事件
    Agent 不能通过 Prompt 改变已确定的审查范围

### 12.6 阶段 2：受限工具和文件组 Agent

当前示例通过 `skill_run` 暴露审查能力。参考 OCR，后续可以逐步增加更细的只读工具：

    file_read
      读取指定文件和行范围，限制最大行数
    file_find
      查找仓库文件，限制结果数量和超时
    code_search
      搜索代码，限制匹配数量和执行时间
    file_read_diff
      读取其他变更文件的预解析 diff
    code_comment
      提交带文件、行号、类别和严重程度的结构化评论
    task_done
      显式结束当前审查任务

每个工具至少要声明：

    参数 schema 和路径范围
    所需权限
    超时、输出大小和 token 预算
    是否只读、幂等、可并行和可重试
    成功和失败结果 schema
    证据引用格式

大型变更不应由一个 Agent 从头看到尾。建议采用：

    小变更
      -> 一个文件组
    相关文件
      -> 语义文件组，共享必要上下文
    不相关文件
      -> 独立 SubAgent，并发审查
    分组失败或超预算
      -> 降级为逐文件审查

每组都要有最大文件数、最大 token、最大工具轮数和超时。并发只解决效率问题，不能
改变规则、范围和最终结果治理。

### 12.7 阶段 3：工作记忆压缩

这是本次重构新增的核心章节。

#### 12.7.1 为什么需要压缩

Agent 的工作上下文会随着每次工具调用增长：

    读取文件
      -> 搜索调用方
      -> 读取类型定义
      -> 查看其他 diff
      -> 重复工具输出进入消息历史
      -> 上下文接近上限

不处理时会出现：

    token 成本上升
    工具结果被截断
    模型注意力被旧信息占用
    新文件或关键 hunk 被漏看
    最终请求超过上下文窗口

因此，记忆压缩的目标不是“把历史变短”这么简单，而是：

    在减少上下文成本的同时，保留审查范围、关键证据、未解决问题和可追溯关系

#### 12.7.2 三段式上下文

建议把一次 Agent 运行中的消息分成三块：

    Frozen
      不允许压缩的稳定事实和约束
      - 当前 diff 和审查范围
      - 已解析的规则
      - 权限、预算和停止条件
      - 已确认的关键证据引用

    Compressible
      可以被摘要的历史探索
      - 较早的工具请求和结果
      - 重复的文件读取内容
      - 已经解决的证据问题
      - 已完成文件组的中间推理

    Active
      当前必须保持原文或近原文的内容
      - 当前文件和 hunk
      - 最近的工具结果
      - 未解决的问题
      - 候选 finding
      - 待执行或正在等待的工具调用

其关系可以理解为：

    Frozen  +  压缩后的历史摘要  +  Active
                       ↑
             Compressible 的增量压缩结果

#### 12.7.3 压缩触发和执行顺序

参考 OCR 论文中描述的策略，可以把上下文占用率作为触发信号：

    约 60%
      后台启动压缩，尽量不阻塞当前 Agent 循环
    约 80%
      下一次模型调用前同步完成压缩
    超过安全上限
      不再继续追加工具结果，进入压缩、降级或停止流程

具体阈值应作为配置，而不是散落在工具实现中的魔法数字。一次压缩建议按以下步骤：

    1. 冻结当前事件序号和上下文边界
    2. 暂停把新的历史消息加入 Compressible 区
    3. 对可压缩消息生成结构化摘要
    4. 保留原始消息的 event_id、文件、行号和 evidence_refs
    5. 用摘要替换已经确认可压缩的消息
    6. 重新计算 token 和上下文 digest
    7. 记录 compression.started、compression.completed 或 compression.failed
    8. 恢复 Agent 循环

OCR 还对工具循环设置最大轮数，并识别连续空转的轮次。当前示例也应把最大轮数、
空转检测和压缩阈值统一纳入运行预算，而不是让模型无限调用工具。

#### 12.7.4 摘要必须保存什么

压缩结果不能只有一段泛化的自然语言。建议使用结构化的 `ContextSummary`：

    ContextSummary
      summary_id
      run_id / session_id
      compressed_event_start / compressed_event_end
      inspected_files
      inspected_hunks
      confirmed_facts
      evidence_refs
      candidate_findings
      resolved_questions
      unresolved_questions
      tool_failures
      next_action_hint
      source_digest
      created_at

其中：

    confirmed_facts
      只记录已经被工具结果支持的事实
    evidence_refs
      指向原始 diff、Skill 输出或 Sandbox 输出
    candidate_findings
      区分已确认、待验证和已否定的候选问题
    unresolved_questions
      防止压缩后 Agent 误以为所有问题都已经检查完
    next_action_hint
      只能作为参考，不能绕过工具、Filter 或审查范围

#### 12.7.5 压缩的安全和失败处理

以下内容不能只因为“比较旧”就被丢弃：

    当前变更的关键 diff
    审查规则和 Filter 决策
    未完成工具调用
    尚未定位的候选 finding
    关键证据的来源和 digest
    需要人工确认的高风险状态

压缩失败时应按以下顺序降级：

    1. 保留原始上下文，尝试一次受控重试
    2. 减少后续工具输出范围或停止低价值探索
    3. 如果仍然超限，保存检查点并输出诚实的降级状态
    4. 不得用一个没有来源的摘要替换原始证据

压缩摘要本身也可能错误或被 Prompt Injection 污染，所以它不能：

    改写审查范围
    授予新的工具权限
    替代原始证据证明 finding 成立
    直接成为跨任务长期记忆

#### 12.7.6 压缩的验收指标

新增测试和评测时，不能只看“压缩成功”。至少要统计：

    压缩前后 token 数和压缩比例
    压缩耗时和上下文溢出率
    文件、hunk、行号和证据的保留率
    压缩造成的漏报率和误报率
    压缩与不压缩时的 finding 一致性
    压缩失败后的降级率
    压缩后恢复运行的结果一致性

建议增加长上下文 fixture、跨文件依赖 fixture、重复工具输出 fixture、压缩失败
fixture 和包含敏感信息的 fixture。压缩测试必须验证 Secret 不会被摘要重新带出。

### 12.8 阶段 4：评论定位和独立 Reflection

Agent 负责发现问题，但不应该独自决定最终展示位置。推荐的结果链路是：

    Agent 生成候选评论
      -> 校验文件属于当前审查范围
      -> 将代码片段定位到新文件和 diff hunk
      -> 必要时进行受控的二次定位
      -> 校验建议代码和结构化字段
      -> 去重并交给 Reflection

Reflection 的职责是过滤，而不是重新生成评论：

    主 Agent
      看到 diff、完整文件和按需搜索到的仓库上下文

    Reflection
      只看到当前 diff 和候选评论
      尝试证明评论与当前变更矛盾或缺乏支持
      无法证伪时保留评论

这种信息边界可以降低“主 Agent 和反思 Agent 共享同一错误上下文”的风险。Reflection
无法调用高风险工具，也不能改变原始 diff、规则和定位结果。

验收标准：

    评论必须对应当前审查范围
    行号定位失败时不能伪造位置
    相同问题重复出现时只保留一个规范结果
    Reflection 失败时有明确降级策略
    结果仍能追溯到原始证据和 Agent 事件

### 12.9 阶段 5：可观测性和检查点恢复

可观测性不是多打印日志，而是让一次审查可以按 `run_id` 还原：

    run.started
    input.parsed
    dispatch.started / dispatch.completed
    context.compression.started / context.compression.completed
    agent.turn.started / agent.turn.completed
    tool.requested
    filter.allowed / filter.denied
    skill.started / skill.completed
    sandbox.started / sandbox.completed
    comment.positioned
    reflection.completed
    checkpoint.saved
    result.normalized
    report.written
    run.completed

工具事件至少记录：

    tool_name、tool_call_id、normalized_arguments
    policy_decision、started_at、finished_at、duration_ms
    result_summary、error_code、retry_count、evidence_refs

默认不记录完整 Prompt、完整源码和未经处理的工具输出。调试原文必须具备单独的访问
权限、保留期限和审计记录。

可恢复和重试不是一回事：

    重试：重新执行某个调用
    恢复：读取已保存状态，从确定的下一步继续

建议状态机至少包括：

    queued -> running -> waiting_tool -> checkpointed -> resuming
      -> completed / completed_with_warnings / failed / needs_human_review

检查点应保存：

    input_digest、repository_ref、review_profile
    最近事件序号和当前阶段
    Frozen 区、ContextSummary 和 Active 区摘要
    未完成工具调用及其幂等信息
    当前文件组、预算和停止条件

恢复后必须比较“不中断运行”和“中断后恢复”的规范化结果。如果仓库或 diff 已经
变化，不能静默覆盖旧结果，应进入明确的降级或人工复核状态。

### 12.10 阶段 6：长期记忆

长期记忆应只保存跨任务仍然有用、低敏感、可追溯的内容，例如：

    repository_profile
      仓库的语言、框架、构建和测试习惯
    review_convention
      团队确认过的审查约定
    finding_feedback
      人工接受、驳回或标记为误报的记录
    rule_outcome
      某条规则在该仓库中的适用性和例外
    operational_fact
      某类检查的已知超时或资源要求

第一版不应默认记住完整源码、未脱敏 diff、Secret、Token 和未经确认的模型猜测。

每条长期记忆需要带上：

    memory_id、memory_type、scope
    source_run_id、repository_ref 或 repository_digest
    evidence_refs、content、confidence
    status: candidate / confirmed / rejected / expired
    created_at、updated_at、expires_at、sensitivity

读取和写入必须分开：

    审查开始
      -> 按仓库、审查 profile 和当前 diff 特征检索
      -> 做权限、过期和敏感信息过滤
      -> 以参考资料提供给 Agent

    审查结束
      -> 从最终确认结果和人工反馈中提取候选记忆
      -> 脱敏、去重、设置置信度和生命周期
      -> 必要时等待人工确认
      -> 写入 MemoryService

工作记忆压缩摘要不能自动升级为长期记忆。它可能只是一次运行中的临时判断，必须
经过结果确认、来源校验和生命周期治理。

### 12.11 阶段 7：形成有边界的自主审查循环

推荐的循环是显式的，而不是让 Agent 无限思考：

    prepare
      -> understand_scope
      -> retrieve_context
      -> gather_evidence
      -> validate_candidates
      -> decide_next_action
      -> conclude_or_continue
      -> report_and_learn

每轮回答三个问题：

    现在已经知道什么？
    还缺什么证据？
    下一步动作的收益是否值得成本和风险？

停止条件至少包括：

    已覆盖目标文件或 hunk
    没有新的证据缺口
    达到轮数、时间、token 或工具预算
    同一工具和参数重复达到上限
    工具连续失败或 Sandbox 资源不足
    输入仓库或 diff 发生变化
    发现需要人工判断的高风险结果
    Filter 拒绝必要动作且没有安全替代

第一版自主循环应保持只读。自动生成补丁、创建 Issue 或修改审查状态，需要另行设计
权限、审批、回滚和审计流程。

### 12.12 阶段 8：建立完整的 Agent Evaluation

评测要同时覆盖组件、轨迹、结果和系统四层：

    组件层
      parser、Dispatcher、Filter、Skill、Sandbox、normalization 是否符合契约
    轨迹层
      工具选择、参数、顺序、压缩、失败处理和停止是否合理
    结果层
      finding 是否准确、完整、可定位、严重程度合理
    系统层
      是否安全、可恢复、可观测、成本可接受，记忆是否带来净收益

可以参考 AACR-Bench 的做法，把输入转换成稳定 JSONL，固定 repository、base commit、
head commit 和人工标注评论，再比较不同 reviewer 的 Precision、Recall、F1、耗时
和 token。[AACR-Bench 评测框架](https://github.com/alibaba/aacr-bench/blob/main/evaluation/README.md)

新增的上下文压缩评测至少要包含以下对照组：

    无压缩
    简单截断旧消息
    滑动窗口
    Frozen / Compressible / Active 三段式压缩
    三段式压缩 + 长期记忆

固定模型、Prompt、工具集合、规则、diff 和预算，只改变上下文策略。指标包括：

| 维度 | 指标 |
| --- | --- |
| Finding 质量 | precision、recall、F1、严重级别和类别准确率 |
| 定位质量 | 文件命中率、hunk 命中率、行号定位率 |
| 上下文质量 | token 压缩比例、证据保留率、压缩造成的漏报率 |
| Tool Use | 无效调用率、成功率、重复调用率、停止准确性 |
| 可靠性 | 超时率、失败率、恢复成功率、恢复后结果一致性 |
| Memory | 检索命中率、有效利用率、误导率、跨仓库泄露率 |
| 效率 | 延迟、token、工具成本、每个有效 finding 的成本 |
| 安全 | 越界调用率、未授权执行率、敏感信息泄露率 |

每次实验都保存模型版本、Prompt 版本、规则版本、工具轨迹、压缩摘要、记忆读写、
恢复记录和最终结果。不能只比较最终 Markdown。

建议保留三种运行模式：

    offline replay
      固定输入和工具结果，比较策略变化
    shadow run
      新版本旁路运行，不影响用户最终结果
    canary run
      只对小范围请求启用，出现质量或安全回退即可关闭

评测还要防止数据泄漏：仓库历史反馈不能同时作为测试集长期记忆和评测答案来源。

### 12.13 模块演进映射

下面是建议的职责演进。新增目录只是组织方案，不代表它们当前已经存在：

| 当前模块 | 当前职责 | 未来演进 |
| --- | --- | --- |
| inputs/parser.py | 解析输入 | 输出 ReviewRequest、input_digest 和稳定文件清单 |
| workflow.py | 串起流程和治理结果 | 演进为 RunController 和状态机驱动器 |
| agent/agent.py | 创建 LLM Agent | 增加文件组任务、预算、停止条件和上下文管理 |
| agent/tools.py | 暴露 Skill 工具 | 增加细粒度 ToolRegistry、schema 和统一结果 |
| filters/ | 检查工具请求 | 作为所有工具的范围和权限门禁 |
| skills/ | 提供审查规则和脚本 | 继续提供领域证据，不直接管理恢复和记忆权限 |
| sandbox/ | 隔离执行检查 | 输出可追踪的执行事件和证据引用 |
| agent/normalization.py | 治理 Agent 结果 | 增加位置校正、去重、评论 schema 校验 |
| reports/ | 输出最终报告 | 携带 run_id、证据引用、压缩和降级信息 |
| storage/ | 保存审查记录 | 扩展 RunEvent、Checkpoint、ContextSummary 和评测结果 |
| tests/ | 验证组件和集成约束 | 增加长上下文、回放、恢复、记忆和安全评估 |

建议的新增目录：

    dispatcher/
      rules.py、grouping.py、coverage.py
    context/
      models.py、compressor.py、policy.py
    reflection/
      validator.py、filter.py
    observability/
      events.py、recorder.py、redaction.py、metrics.py
    recovery/
      state_machine.py、checkpoints.py、idempotency.py
    memory/
      schemas.py、policy.py、retrieval.py、writer.py
    evaluation/
      dataset.py、replay.py、metrics.py、reports.py

目录名称不是重点，重点是每个能力都有明确的输入、输出和责任边界。特别是：

    context/ 负责当前运行的上下文窗口
    memory/ 负责跨任务经验
    storage/ 负责可靠保存
    evaluation/ 负责比较结果
    Agent 不能同时决定权限、schema 和评测分数

### 12.14 建议的前三个实际迭代

#### 第一步：定义事件、状态和审查范围契约

不改变 Agent 决策逻辑，先把现有 Agent、Skill、Filter、Sandbox、结果治理和报告
写入统一成 RunEvent；同时增加文件范围、规则来源和排除原因。

验收：

    Fake Agent 和真实 Agent 产生同一类核心事件
    每个事件关联 run_id 和单调递增 seq
    可以还原一次审查的文件范围和工具轨迹
    Secret 和原始敏感内容不会进入默认事件

#### 第二步：实现工作记忆压缩的离线实验

先不接长期 MemoryService，也不扩大 Agent 权限。使用长上下文模拟数据比较“无压缩”、
“简单截断”和“三段式压缩”，观察压缩对 token、证据保留和 finding 的影响。

验收：

    ContextSummary 能关联原始事件和 evidence_refs
    Frozen 内容不会被压缩删除
    压缩失败可以保留原文或诚实降级
    压缩前后结果差异可以自动统计

#### 第三步：加入检查点、恢复和 Tool Use 回放

在 SQLite 上先验证契约。主动在 parser 后、tool call 后、压缩完成后、Sandbox 超时
后和报告写入前终止进程，比较不中断和恢复运行的结果。

验收：

    重启后不会丢失已完成的证据和压缩摘要
    未完成工具调用不会被无条件重复执行
    恢复后的规范化结果与正常运行一致，或明确标出差异
    同一个 case 可以回放并比较结果、轨迹、耗时和 token

完成这三步后，再接入长期记忆和更强的自主循环。否则无法判断记忆究竟带来了收益，
还是只是让上下文变长、压缩变复杂。

### 12.15 三条研究主线的统一方法

Tool Use、上下文压缩和长期 Memory 不应成为三个孤立课题。每个实验都使用同一套方法：

    1. 提出假设
       例如“上下文压缩可以降低 token，而不降低跨文件 finding 的召回率”
    2. 固定输入和权限
       明确 diff、仓库范围、规则、工具集合和预算
    3. 记录完整轨迹
       保存工具调用、压缩事件、证据、恢复和最终结果
    4. 设计对照组
       例如无压缩、截断、三段式压缩、无记忆和有记忆
    5. 使用分层指标
       同时看 finding、定位、证据、成本、可靠性和安全
    6. 做失败分析
       判断问题来自调度、Prompt、工具、压缩器、记忆、模型还是 Workflow
    7. 保留可回放样本
       把有代表性的成功、漏报、误报和恢复失败加入回归集

最值得持续研究的问题是：

    Tool Use
      Agent 如何识别证据缺口？什么时候串行、并行或停止？
      工具描述和参数 schema 如何影响调用质量？

    Context Compression
      哪些事实必须 Frozen？如何判断一段历史可以压缩？
      摘要如何保留证据引用、未解决问题和负面结果？
      压缩失败、摘要错误或上下文污染时如何安全降级？

    Long-term Memory
      哪些历史信息具有跨任务价值？如何过期、撤销和防止跨仓库泄露？
      人工反馈如何更新已有记忆，而不是不断追加矛盾事实？

    Agent Evaluation
      如何区分“没有问题”和“没有查到”？
      如何同时评价质量、压缩收益、恢复能力和安全边界？

最后用下面的发布原则判断每次改动：

    质量没有下降
      且关键证据没有因压缩丢失
      且安全边界没有变弱
      且工具轨迹和降级原因更可解释
      且中断后可以恢复或诚实失败
      且长期记忆的收益大于污染和泄露风险

达到这个标准后，当前项目才会从“能调用 Skill 的代码审查示例”，逐步演进成一个
具有确定性调度、受控上下文、可验证评论、可恢复执行和科学评测能力的 Code Review Agent。
