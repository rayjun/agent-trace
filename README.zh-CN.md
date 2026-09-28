# agent-trace

[English](README.md) · [中文](README.zh-CN.md)

把 agent 的每一次 LLM 调用 —— 请求、响应、工具、token —— 记录到本地的
一个 JSONL 流里，用一个 CLI 查询回放，或者在 agent 运行时用侧边面板实时观看。

支持 **Hermes**、**OpenAI Codex CLI** 和 **Pi**。没有代理、没有云端、没有
外部服务：trace 全程以纯 JSONL 留在磁盘上。

## 安装

在仓库根目录执行 —— 每个 agent 一条命令，外加 CLI：

```bash
bash install.sh hermes [profile|all]    # Hermes 插件
bash install.sh pi                      # Pi 扩展
python3 codex/install_hook.py           # Codex hook
bash install.sh cli                     # agenttrace 命令
bash install.sh all                     # 以上全部
```

各跑一次真实调用来验证 —— `agenttrace ls` 的第二列就是 agent 名：

```bash
hermes chat -q "Reply with exactly: OK"    # -> ~/.hermes/traces/
pi -p "Reply with exactly: OK"             # -> ~/.pi/agent/traces/

agenttrace ls        # 新增行显示 `hermes` / `pi`
agenttrace watch     # 同样的流量，实时面板
```

trace 文件分别落在 `~/.hermes/traces/`、`~/.pi/agent/traces/` 和
`~/.codex/traces/`。

## 查询 trace

`agenttrace` 由 `bash install.sh cli` 安装（软链接到 `~/.local/bin`）：

```bash
agenttrace ls                      # 最近的记录，每行一条
agenttrace show <request_id>       # 一次调用的完整请求 + 响应
agenttrace search "17*23"          # 在全部记录内容里 grep
agenttrace sessions                # 按 session id 分组
agenttrace stats                   # 按 agent / model 统计调用、token、错误
```

所有命令都支持 `--agent`、`--model`、`--event`、`--session`、`--since`、
`--contains`、`--limit`，以及可选的 trace 目录参数。

## 实时观看

```bash
agenttrace watch                    # 当前 session，实时（默认）
agenttrace watch --all-sessions     # 所有 session
agenttrace watch --session <id>     # 固定某个 session
agenttrace watch --history 200      # 预载 200 条记录
agenttrace watch --no-follow        # 打印当前内容后退出
```

一个全屏面板，跟着 agent 运行实时滚动 —— 一次调用一块：你发了什么、
模型回了什么、跑了哪些工具、token 和耗时。

按键：`e` 展开折叠字段 · `/` 过滤 · `PgUp`/`PgDn` 滚动 · `q` 退出。

在另一个终端里跑，或者拆分你的 shell：

```bash
tmux new-session -d -s trace
tmux split-window -t trace -h -l 45
# 右侧面板运行: agenttrace watch
```
