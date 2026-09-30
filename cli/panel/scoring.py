"""agenttrace watch — local, deterministic prompt-quality scoring

Part of the `agenttrace watch` TUI; split out of agenttrace_watch.py so each
piece has one job and one test surface. The main-loop module re-exports the
names the CLI and the tests reach for.
"""
from __future__ import annotations

import re

# -- prompt scoring --------------------------------------------------------
# Every user prompt earns ONE number, computed locally: deterministic, free,
# instant, and prompt content never leaves the machine. Four dimensions add
# up to 90, length sanity adds 10, and a long-but-vague message loses up to
# 15. The panel prints the total, a letter grade and the dimensions that
# carried it, so the score is explainable instead of mystical.

_RE_PATH = re.compile(r"https?://\S+|[\w./~-]*\.[A-Za-z]{1,6}\b|/\S{2,}")
_RE_TICK = re.compile(r"`[^`\n]+`")
_RE_QUOT = re.compile(r"[\"'][^\"'\n]{3,}[\"']")
_RE_NUM = re.compile(r"\b\d[\d,.]*\b")
_RE_ACR = re.compile(r"\b[A-Z]{2,6}\b")   # KV, LLM, API — named tech terms
_RE_ACTION = re.compile(
    r"帮我|修复|检查|生成|解释|分析|总结|优化|实现|运行|测试|对比|列出|改为|改成"
    r"|删除|添加|验证|调试|部署|安装|读取|梳理|调研|对照|跑一下"
    r"|提取|解析|转换|翻译|构建|创建|统计|重命名|汇总|校验|整理"
    r"|\b(?:review|fix|add|write|explain|check|update|implement|refactor|test"
    r"|summarize|compare|find|debug|install|deploy|run|trace|measure"
    r"|read|reply|count|list|show|give|tell|translate|convert|parse|extract"
    r"|create|delete|rename|move|copy|format|lint|verify|build|draft)\b",
    re.IGNORECASE)
_RE_CONTEXT = re.compile(
    r"因为|所以|目前|之前|已经|现在|背景|要求|不要|注意|如果|但是|同时|参考"
    r"|原因|比如|例如|应该|需要|按照|直接|不用|为什么|怎么|如何|是不是|搞懂"
    r"|明白|想知道|哪里|什么时候|先.*再"
    r"|\b(?:when|given|because|currently|however|instead|note that|for context"
    r"|make sure|instead of|after|before)\b",
    re.IGNORECASE)


def score_prompt(text: str) -> tuple[int, list[str]]:
    """Heuristic quality score for one user prompt: (0-100, strong dims).

    Dimensions (caps): specific 30 (paths/URLs/`code`/quotes/numbers/tech
    acronyms), action 30 (explicit action verbs — a terse imperative can
    score well without any background), context 15 (background/constraint
    language), structure 15 (line breaks, bullets, sentences), length 10
    (substance band). Penalty −15 when a long message has no specifics,
    context or action at all. The returned labels are the dimensions that
    scored ≥60% of their cap — what the prompt did well.
    """
    t = str(text or "")
    if not t.strip():
        return 0, []
    markers = (len(_RE_PATH.findall(t)) + len(_RE_TICK.findall(t))
               + min(3, len(_RE_QUOT.findall(t)))
               + min(4, len(_RE_NUM.findall(t)))
               + min(4, len(_RE_ACR.findall(t))))
    specific = min(30, markers * 8)
    ctx_n = len(_RE_CONTEXT.findall(t))
    context = min(15, ctx_n * 5)
    act_n = len(_RE_ACTION.findall(t))
    action = min(30, act_n * 9)
    struct = 0
    if t.count("\n") >= 1:
        struct += 4
    if len(re.findall(r"(?m)^\s*(?:[-*•]|\d+[.)])\s", t)) >= 2:
        struct += 5
    if len(re.findall(r"[。！？!?]|(?:\.\s)", t)) >= 2:
        struct += 6
    struct = min(15, struct)
    n = len(t)
    if n < 12:
        length = 0
    elif n < 30:
        length = 4
    elif n < 60:
        length = 7
    elif n <= 4000:
        length = 10
    else:
        length = 8                      # a wall of text is not automatically good
    penalty = 15 if (specific == 0 and context == 0 and action < 9
                     and n > 60) else 0   # long but nothing concrete to act on
    score = max(0, min(100, specific + context + action + struct + length - penalty))
    dims = []
    for label, val, cap in (("specific", specific, 30), ("action", action, 30),
                            ("context", context, 15), ("structure", struct, 15),
                            ("length", length, 10)):
        if val >= 0.6 * cap:
            dims.append(label)
    return score, dims


def grade_for(score: int) -> str:
    return "A" if score >= 85 else "B" if score >= 70 else "C" if score >= 50 \
        else "D" if score >= 40 else "E"


