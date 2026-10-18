# agent-trace

[English](README.md) · [中文](README.zh-CN.md)

**面向 AI Agent 的 local-first 可观测与评估平台。** 追踪、调试、评估你的 agent —— 数据留在你自己的机器上。

每一次模型调用、工具执行、子代理和失败都会被记录到本地 JSONL 流，索引进 SQLite，并以四种方式读取：命令行、实时 TUI 面板、浏览器 Dashboard，以及一套把历史运行变成可重复测试的评估框架。

支持 **Hermes**、**OpenAI Codex CLI** 和 **Pi**，也支持**任何使用 OpenTelemetry 的应用**与 **LangChain/LangGraph**。没有代理、没有云端、不需要账号、不依赖外部服务。

```bash
bash install.sh all              # 安装适配器 + CLI
agenttrace serve --daemon        # 索引进程 + API + Dashboard，监听 127.0.0.1:8741
```

## 安装

在仓库根目录执行 —— 每个 agent 一条命令，外加 CLI：

```bash
bash install.sh hermes [profile|all]    # Hermes 插件
bash install.sh pi                      # Pi 扩展
python3 codex/install_hook.py           # Codex hook
bash install.sh cli                     # agenttrace 命令
bash install.sh all                     # 以上全部
bash install.sh status                  # 已安装了什么 —— 以及哪些是过期代码
```

Hermes 适配器是**拷贝**部署，所以 `status` 会把部署的字节和本仓库逐字节比较，告诉你正在运行的 agent 是否还在用旧代码。其余三个是指向 checkout 的实时引用。

跑一次真实调用验证 —— `agenttrace ls` 的第二列就是 agent 名：

```bash
hermes chat -q "Reply with exactly: OK"    # -> ~/.hermes/traces/
pi -p "Reply with exactly: OK"             # -> ~/.pi/agent/traces/

agenttrace ls        # 新增行显示 `hermes` / `pi`
agenttrace watch     # 同样的流量，实时面板
```

trace 落在 `~/.hermes/traces/`、`~/.pi/agent/traces/` 和 `~/.codex/traces/`。

## 采集了什么

每个观测一条记录，使用共享格式（`schema/trace.schema.json`）：

* **模型调用** —— 模型真正收到的 prompt、响应、它请求的工具调用、token（输入/输出/缓存/推理）和延迟；
* **工具执行** —— 参数、结果体、退出码、耗时；
* **会话与轮次** —— 足以重建整段对话的结构；
* **失败** —— 供应商错误、工具错误，以及从未结束的 span；
* **工作区** —— 仓库、分支、提交和改动内容（见下文）。

## 查看 trace

`agenttrace` 由 `bash install.sh cli` 安装（软链接到 `~/.local/bin`）：

```bash
agenttrace ls                      # 最近的记录，每行一条
agenttrace show <request_id>       # 一次调用的完整请求 + 响应
agenttrace search "17*23"          # 在全部记录内容里 grep
agenttrace sessions                # 按 session id 分组
agenttrace stats                   # 按 agent / model 统计调用、token、错误
agenttrace session <id>            # 一次对话：每一轮各自的耗时
```

所有命令都接受 `--agent`、`--model`、`--event`、`--session`、`--since`、`--contains`、`--limit`，以及可选的 trace 目录。

`show` 打印的是模型真正收到的 prompt：system 消息永远在第一位，每条消息都标注了是谁写的。三个适配器存储 system prompt 的方式不同（Hermes 同时写进 `messages` 和一个单独的 `system_prompt` 字段，Pi 只写进 `messages`，Codex 只写进字段且 `messages` 为空），所以 `show` 把三者折叠成请求载荷真实的形状 —— 一份 prompt，旁边没有 `system_prompt` / `instructions` 标题：

```jsonc
{
  "model": "space-bunny-free",
  "messages": [
    { "role": "system", "content": "You are a helpful coding assistant." },
    { "role": "user", "content": "Hello, who are you?" }
  ],
  "tools": ["read", "write"],
  "message_count": 2,
  "tool_count": 2
}
```

`ls` 打印最近的记录 —— 取最新的 `--limit` 条，按旧到新排列；加 `--reverse` 则新到旧。首次运行会在 `~/.cache/agent-trace` 建一个小索引，之后 `ls`/`sessions`/`stats` 大约一秒返回，而不是重新扫描数百 MB。设 `AGENTTRACE_INDEX=0` 强制全量扫描，或设 `AGENTTRACE_VALIDATE=1` 让每个适配器在写入时逐条校验记录是否符合契约。

## 实时观看

```bash
agenttrace watch                    # 当前 session，实时（默认）
agenttrace watch --all-sessions     # 所有 session
agenttrace watch --session <id>     # 固定某个 session
agenttrace watch --history 200      # 预加载 200 条记录
agenttrace watch --no-follow        # 打印当前内容后退出
```

一个全屏面板，跟随 agent 运行时的 trace 流 —— 每次调用一块：你发了什么、模型回了什么、跑了哪些工具、token 与延迟。

按键：`e` 展开折叠字段 · `/` 过滤 · `PgUp`/`PgDn` 滚动 · `q` 退出。

在另一个终端运行，或者拆分你的 shell：

```bash
tmux new-session -d -s trace
tmux split-window -t trace -h -l 45
# 右侧 pane 运行： agenttrace watch
```

## Dashboard

```bash
agenttrace serve --daemon          # 索引进程 + 本地 API + Web UI，后台常驻
agenttrace serve --status          # 是否在运行？pid 是多少？
agenttrace serve --stop
agenttrace serve                   # 前台运行（Ctrl-C 退出）
```

要「起一次，之后随时打开看」就用 `--daemon`：它通过 `scripts/serve-daemon.py` 重新执行，用 `fork` + `setsid` 脱离当前会话（macOS 没有 `setsid(1)` 命令）。直接 `nohup ... &` 起的进程仍留在启动它的进程组里，会被回收进程组的一方连带杀掉，表现就是「页面突然连不上」。pid 写在 `~/.agenttrace/server.pid`，日志在 `~/.agenttrace/server.log`。

它把所有已知 trace 目录索引进 `~/.agenttrace/agenttrace.db`（增量、崩溃安全 —— JSONL 始终是真源），并在 `http://127.0.0.1:8741` 提供 Dashboard，启动日志里给出一次性 token。agent 写入新 trace 时通过 SSE 实时推送。

| 页面 | 回答什么 |
|---|---|
| **Overview** | trace 数、错误率、token 用量、耗时 p50/p95、按适配器的汇总 |
| **Traces** | 按时间、agent、状态、耗时、token、session 搜索 |
| **Trace detail** | 执行树 + waterfall、span 的输入/输出/错误/原始数据、工作区、token 用量。一个 trace 是**一轮** |
| **Sessions** | 所有对话；点进去看 **Session detail** |
| **Session detail** | 每一轮的耗时、占 agent 时间的比例，以及把这些轮放在真实时间轴上的墙钟视图 —— 空档看得见 |
| **Datasets / Experiments** | 评估运行、每条样本的评分与证据 |
| **Adapters** | 声明能力 vs 实际观测数据、采集健康度、覆盖率缺口 |

```bash
agenttrace index                   # 一次性重建索引，输出报告
agenttrace index --full            # 从 JSONL 文件重新摄入全部数据
```

`--full` 会先清空归档再重读文件 —— 这是让转换器的改进到达已索引 trace 的方式。Datasets 和 Experiments 会保留：那是你的数据，不是派生数据。

## 不只是查看，还要评估

把一次糟糕的运行变成回归测试，然后对整批打分：

```bash
agenttrace report --limit 100       # 近期 trace 的效率报告
agenttrace report --verbose         # ...附带每处标记的证据
```

`report` 使用与 Dashboard 相同的评估器，把每个信号**分开**列出 —— 首次有效操作延迟、错误恢复、重复重试、重复调用、上下文膨胀 —— 并给出样本量、平均置信度和 `no_signal` 计数。**信号永远不会被加总成一个分数**：重试可能是正确的恢复策略，多花 token 可能是任务更难。模型没有配置价格时成本显示为 `unknown`，而不是估算值。

三类评估器：

| 类型 | 例子 |
|---|---|
| deterministic | 测试是否通过、输出是否包含某内容、是否超出预算、**agent 声称成功但最后一次测试其实失败** |
| trajectory | 相同失败调用重复、错误恢复、上下文增长但没有推进、首次操作延迟 |
| LLM-as-judge | 任务完成度，由**本地**模型端点评判（未配置 ⇒ 报"无信号"，绝不伪造判决） |

Dataset 从真实 trace 构建（保存任务与指针，而不是整份 prompt 历史的副本），Experiment 钉住 dataset 版本、evaluator 版本和 agent 配置；运行采用 **playback** 评分：对已录制的执行打分，而不是重新跑 agent。

## 「agent 在跑，但页面上什么都没有」

适配器在其宿主进程启动时加载，所以对已经在运行的会话来说，装好扩展没有任何影响 —— 需要重启 agent，Pi 可以 `/reload`。Dashboard 会自己说明这一点，`agenttrace adapter coverage` 会告诉你到底缺什么：

```bash
agenttrace adapter coverage         # 缺什么、为什么缺（有近期缺口时退出码 1）
```

它把每个 agent 留下的会话证据与「我们有 trace 的会话」做比对，并按适配器安装时间分年龄段：安装**之后**启动却无 trace 的会话是缺陷，会告警；安装之前的属于历史，列为可回填候选。

留有 transcript 的 agent 可以恢复历史：

```bash
python3 pi/pi_import.py             # Pi：所有没有 live trace 的会话
python3 pi/pi_import.py --list      # 先看会导入什么
python3 codex/codex_import.py <rollout.jsonl>
```

回填记录带有标记（`backfill=session`，以及自己的 id 命名空间），不会被误认为实时观测；请求侧是从 transcript 重建的，记录里也写明了这一点。Hermes 不留 transcript，所以那里没有可恢复的内容。

## 适配器

内置五个：**hermes**、**codex**、**pi**、**otlp**（导入/导出任何接入 OTel 的应用）和 **langgraph**（LangChain/LangGraph callback handler）。

```bash
agenttrace adapter list            # 来源、能力、验证测试
agenttrace adapter test langgraph  # 对内置 fixture 跑一致性校验
agenttrace otlp import otel.json   # OTLP 导入
agenttrace otlp export --trace <id> > otel.json   # OTLP 导出
```

能力用四级诚实声明 —— `supported`、`partial`、`unsupported`、`unknown` —— Dashboard 逐项展示每个适配器能看见什么、看不见什么。缺失的字段显示为「该接入方式未提供此字段」，而不是空字符串；例如 codex 声明 `streaming: unsupported`，因为它的 rollout 文件是在完成后才写入的。

第三方适配器通过 manifest 或 entry point 被发现，**发现时不导入任何代码**：发现只读元数据，只有你显式要求时才执行代码。详见 `docs/adapters.md` 与 `docs/writing-an-adapter.md`。

## trace 面对的是哪份代码

脱离代码去读一个 coding agent 的 trace，只能读懂一半，所以每个 session 都会记录工作区：仓库根、分支、提交、是否有未提交改动、以及改动文件（含未跟踪文件）。工具调用如果指定了文件，会把该文件带上，因此你可以沿着一个文件看完整条 trace。

快照是**只读**的（`git --no-optional-locks`，绝不会拿你的 index 锁）、**有界的**（超时 + 大小上限），并且**标注了读取时刻**：`at: capture` 表示适配器当场看到的，`at: index` 表示当时没人记录、后来补读的。混淆两者会让几个月前的 trace 看起来像是跑在今天的分支上。

diff **默认不抓** —— 它可能包含源码和凭据 —— 快照会说明它 withheld 了：

```bash
AGENTTRACE_GIT=full agenttrace serve   # 抓取 diff（有上限）
AGENTTRACE_GIT=off  agenttrace serve   # 完全不抓工作区
```

## 隐私与安全

* **数据不离开本机。** 没有云端、没有账号、没有遥测。
* **脱敏发生在采集时**，在适配器内、写入磁盘之前 —— 不存在"先写后擦"的路径。`AGENTTRACE_REDACT=0` 关闭脱敏；`AGENTTRACE_CAPTURE=metadata` 保留结构（计数、id、时间、工具名、token）而去掉正文。
* **本地 API 不被默认为可信。** 它只绑定回环地址、校验 `Host` 头（防 DNS rebinding）、不发送任何 CORS 头、检查 `Origin`，并要求 bearer token（`~/.agenttrace/server.token`，权限 0600）。
* **Dataset 存的是指针而不是副本**；只有在导出 trace 且你明确要求时，diff 才可能出现在其中。

目前**刻意还没有**：保留期限与自动删除、从 CLI 删除单条 trace/session、真正的 agent 重放（目前只有 playback）、跨机器聚合。理由见 `docs/decisions.md`。

## 各部分如何衔接

```
hermes/__init__.py    plugin hooks ─┐
codex/codex_import.py rollout tail  ├─ 适配器 ─> canonical events (JSONL)
pi/agent-trace.ts     Pi 事件        │                 │
adapters/otlp/        OTLP JSON     │                 │  schema/events.schema.json
adapters/langgraph/   callbacks    ─┘                 │  common/agenttrace_common.py
                                                      │  adapter_sdk/*
                                       core/storage/ ──┴─> indexer ─> SQLite
                                                           │
                            core/query.py ─────────────────┤
                                                           ├─> server/  (API + SSE + Dashboard)
                                                           ├─> cli/     (ls show search sessions stats session watch)
                                                           └─> evaluation/ (datasets · evaluators · experiments)
```

`schema/events.schema.json` 是所有适配器共同遵守的契约；`schema/trace.schema.json` 是三个宿主适配器仍在写入的旧扁平记录格式，读取时转换（`core/convert/v1_to_v2.py`），因此已有数据不会丢失。`common/agenttrace_common.py` 存放每个适配器都必须共享的规则 —— 时间戳、内容扁平化、usage 映射、截断、脱敏、加锁追加 —— 它会被拷贝到部署的 Hermes 插件旁边，这也是共享层只依赖标准库的原因。`schema/CHANGELOG.md` 记录每个契约字段的含义和由来。

## 开发

```bash
python3 -m venv .venv && .venv/bin/pip install ruff mypy
test/run-all.sh            # 全部测试（SKIP_E2E=1 跳过真实模型调用）
.venv/bin/ruff check cli common codex hermes test
.venv/bin/mypy
cd pi && npm ci && npm run typecheck
```

套件约 30 个文件，包含每个适配器的 fixture、一致性测试工具、Dashboard 路由测试，以及两个端到端测试（真实模型调用，需要有可用的 `hermes`/`pi` 安装）。`test/review-fixes.test.py` 保存了各轮 review 的全部发现，**包括那些由修复本身引入的问题**；每条的理由记录在 `docs/decisions.md`。

CI 在每次 push 时运行 lint、类型检查和完整套件（`.github/workflows/ci.yml`）。

## 文档

| 文件 | 内容 |
|---|---|
| `docs/architecture.md` | 六层架构与层间边界 |
| `docs/schema.md` | canonical event 模型，逐字段说明 |
| `docs/adapters.md` | 五个适配器、能力、覆盖率、数据恢复 |
| `docs/writing-an-adapter.md` | 如何新增适配器，以及它会被如何检验 |
| `docs/examples.md` | 可直接运行的起步示例 |
| `docs/decisions.md` | 每一个架构决策及其理由 |
