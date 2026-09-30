"""agenttrace watch — turning one record into display lines

Part of the `agenttrace watch` TUI; split out of agenttrace_watch.py so each
piece has one job and one test surface. The main-loop module re-exports the
names the CLI and the tests reach for.
"""
from __future__ import annotations

import json

from panel.scoring import grade_for, score_prompt
from panel.text import (C_AI, C_BLUE, C_BOLD, C_DIM, C_MAGENTA, C_RED, C_REV,
                        C_RESET, C_USER, C_WHITE, C_YELLOW, EXPAND_LIMIT,
                        FOLD_THRESHOLD, FIELD_W, NORMAL_BODY_LINES, _as_text,
                        _bar, _fmt_tok, _fit, _take_cells, _wide, avail_for,
                        call_hue, clip, clip_plain, sane, strip_ansi, vlen)

from agenttrace_common import cache_hit_rate, total_input_tokens

def _args_preview(arguments) -> str:
    """One-line preview of a tool_call's `arguments`.

    Prefer the argument that identifies WHAT was decided on (path/command/
    query/url/pattern — the same preference the tool summary line uses); fall
    back to a compact JSON blob. `arguments` arrives as a JSON string from
    providers, sometimes already parsed, sometimes missing.
    """
    args = arguments
    if isinstance(args, str):
        s = args.strip()
        if not s:
            return ""
        try:
            args = json.loads(s)
        except ValueError:
            return sane(s)
    if isinstance(args, dict):
        for k in ("path", "command", "query", "url", "pattern"):
            v = args.get(k)
            if isinstance(v, str) and v.strip():
                return sane(v)
        try:
            blob = json.dumps(args, ensure_ascii=False)
        except (TypeError, ValueError):
            blob = str(args)
        return sane(blob)
    return sane(str(args or ""))



# --------------------------------------------------------------------------
# record -> lines
# --------------------------------------------------------------------------

class Formatter:
    """Renders one record into display lines. `expanded` toggles long fields."""

    def __init__(self, expand: bool = False, width: int = 96):
        self.expand = expand
        self.width = width          # content width, excluding indent

    def _field(self, label: str, value, indent: int, color: str = "") -> list[str]:
        if value is None or value == "":
            return []
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        text = sane(text)
        # Continuation lines must line up under the first line's text, so the
        # label is padded to a fixed column instead of using len(label).
        pad = " " * max(0, FIELD_W - len(label))
        cont_indent = " " * (indent + FIELD_W + 2)
        lines: list[str] = []

        if not self.expand and vlen(text) > FOLD_THRESHOLD:
            # Fold to ONE line first. A system prompt starts with newlines, and
            # leaving them in turns a single budgeted record line into three
            # terminal rows — the panel then overflows by more than its budget
            # and every row below lands out of position.
            text = self._fold_one(text)
            stub = f"… ({len(text)} chars · e to expand)"
            # Reserve room for the stub so the fold notice always survives the
            # panel's own width clipping, and cut by DISPLAY columns rather
            # than characters: `text[:room]` on a CJK prompt overshoots by
            # nearly a full width and pushes the stub off the panel.
            room = avail_for(self.width, indent, FIELD_W + 2 + vlen(stub))
            if room < 4:
                # Narrow panel: the stub alone is wider than the space left for
                # the text. Shorten it rather than emit a line that cannot fit
                # — the char count is the useful part, "to expand" is not.
                stub = f"…{len(text)}c"
                room = avail_for(self.width, indent, FIELD_W + 2 + vlen(stub))
            if room < 2:
                # Even the short stub does not fit beside the label. Show the
                # stub alone: knowing there are 34k hidden characters is more
                # useful than a truncated preview of them.
                lines.append(_fit(f"{' ' * indent}{color}{label}{pad}",
                                 stub, "", self.width, C_RESET))
                return lines
            head, _ = _take_cells(text, room)
            if head != text:
                head = head.rsplit(" ", 1)[0]
            lines.append(f"{' ' * indent}{color}{label}{pad}{head}{stub}"
                         f"{C_RESET}")
            return lines

        if not self.expand:
            # The preview must be clipped to what is LEFT after the label, not
            # to the panel width: prefixing an already width-fitted preview
            # with the label overshoots by exactly the label block (10 cols),
            # and the terminal hard-wraps the line — the same bug class this
            # renderer has hit four times, here in the fold branch.
            room = self.width - indent - max(len(label), FIELD_W)
            preview = clip_plain(self._fold_one(text), room) if room > 0 else ""
            lines.append(f"{' ' * indent}{color}{label}{pad}"
                         f"{preview}{C_RESET}")
            return lines

        # Expanded: hard-wrap each source line to the panel width. The label
        # appears once, on the first line; the rest are continuation-indented.
        # The floor is a *relative* one: an absolute `max(20, ...)` silently
        # overrode the real budget on a 30-column panel (18 -> 20) and pushed
        # every continuation line 2 columns past the edge.
        avail = avail_for(self.width, indent, FIELD_W + 2)
        printed_label = False
        for raw in text[:EXPAND_LIMIT].splitlines() or [""]:
            if raw == "":
                lines.append("")
                continue
            body = raw
            while body:
                out, w = [], 0
                for ch in body:
                    cw = 2 if _wide(ch) else 1
                    if w + cw > avail:
                        break
                    out.append(ch)
                    w += cw
                if not out:                       # pathological: single wide char
                    out, body = [body[0]], body[1:]
                    chunk = "".join(out)
                else:
                    chunk = "".join(out)
                    body = body[len(chunk):]
                if not printed_label:
                    lines.append(f"{' ' * indent}{color}{label}{pad}{chunk}{C_RESET}")
                    printed_label = True
                else:
                    lines.append(f"{cont_indent}{color}{chunk}{C_RESET}")
        return lines

    def _fold_one(self, text: str) -> str:
        """Single-line preview of possibly-multiline text."""
        return sane(text).replace("\r", "").replace("\n", " ⏎ ")

    # -- what the panel is actually for -----------------------------------
    # The reader wants two things: what was SENT to the model, and what the
    # model SAID. Tool traffic is context, not content: a single read_file can
    # dump 6KB of file into the transcript, and rendering it inline buries the
    # two things that matter. So tool records collapse to one dim summary line
    # and only the prompt/reply pairs get body text.

    def _summary_line(self, rec: dict) -> str | None:
        """One dim line describing a tool interaction, or None to render fully."""
        ev = rec.get("event")
        t = rec.get("tool") or {}
        name = sane(t.get("name") or "?")
        if ev == "tool_call":
            args = t.get("args")
            detail = ""
            if isinstance(args, dict):
                # Prefer the single most identifying argument over the whole blob.
                for k in ("path", "command", "query", "url", "pattern"):
                    v = args.get(k)
                    if isinstance(v, str) and v.strip():
                        detail = v
                        break
                else:
                    detail = json.dumps(args, ensure_ascii=False)
            elif isinstance(args, str):
                detail = args
            # Assemble the line, then measure it. Hand-budgeting the columns
            # (mark, name, gaps, counter) kept missing one — a 2-space gap here,
            # a label there — and a line 2 columns too wide makes the terminal
            # hard-wrap, which desynchronises everything below it.
            tail = f"  ({len(str(args or ''))} chars args)"
            flat = sane(detail).replace("\r", "").replace("\n", " ⏎ ")
            return _fit(f"  {C_DIM}⚙ {C_YELLOW}{name}{C_RESET}{C_DIM}  ",
                        flat, tail, self.width, C_RESET)

        if ev == "tool_result":
            body = (rec.get("response") or {}).get("content") or ""
            status = t.get("status") or ""
            n = len(str(body))
            err = rec.get("error")
            bad = status == "error" or bool(err)
            mark = "✗" if bad else "✓"
            mark_c = C_RED if bad else C_AI
            # Collapsed is a STATUS line, not a content line: size + `e` is all
            # a healthy result earns. A read_file answer is 6KB of JSON that
            # buries the prompt/reply this panel exists for — `e` shows it.
            # A failure is the one case where the first words matter, so it
            # keeps a short clipped preview (clip in _fit bounds the width).
            # One assembly rule for all three cases:
            #   name block (ends with 2 spaces) + middle + " · e".
            head = (f"  {mark_c}{mark} {C_YELLOW}{name}{C_RESET}"
                    f"{C_DIM}  ")
            hint = f" · {C_BOLD}e"
            if bad:
                preview = self._fold_one(
                    str(err.get("message") if isinstance(err, dict) else "")
                ) or self._fold_one(str(body)) or ""
                return _fit(head, preview or "(no output)", hint,
                            self.width, C_RESET)
            if not str(body):
                return _fit(head, "(no output)", hint, self.width, C_RESET)
            return _fit(head, f"{n} chars", hint, self.width, C_RESET)
        return None

    def render(self, rec: dict, call: int = 0) -> list[str]:
        """Render one record. `call` is the current LLM-call sequence number
        (assigned by render_view, 0 = no call context, e.g. a standalone
        render in a test) — it is what ties a request, its reply and the
        `call #N` banner together as one complete call."""
        ev = rec.get("event", "?")
        agent = rec.get("agent", "?")

        # Tool traffic collapses to one dim line — but only while collapsed.
        # With `e` held down the reader asked for the real args/output, so the
        # full renderer takes over.
        if not self.expand:
            summary = self._summary_line(rec)
            if summary is not None:
                return [summary]

        # session_start / session_end are structural, not conversational.
        if ev in ("session_start", "session_end"):
            ts = (rec.get("ts") or "")[11:19]
            note = clip(sane(rec.get("note") or "").replace("\n", " "), max(10, self.width - 30))
            # Magenta marks the boundary between conversations — the one line
            # that answers "where did this session start/end" while scrolling.
            return [f"{C_MAGENTA}{ts} {rec.get('session_id', '')[-8:]} {ev}{C_RESET}"
                    + (f" {C_DIM}· {note}{C_RESET}" if note else "")]

        if ev == "llm_request":
            return self._render_request(rec, agent, call)
        if ev in ("llm_response", "assistant_message"):
            return self._render_response(rec, agent, call)
        if ev == "llm_error":
            return self._render_error(rec, agent, call)
        if ev == "user_prompt":
            return self._render_user(rec, agent)
        if ev in ("tool_call", "tool_result"):
            return self._render_tool(rec)
        return self._render_other(rec)

    def _render_tool(self, rec: dict) -> list[str]:
        """Full tool rendering — only reached with `e` held (see render())."""
        t = rec.get("tool") or {}
        status = t.get("status") or ""
        head = f"  {C_BOLD}⚙ {sane(t.get('name', '?'))}{C_RESET}"
        if status:
            head += f" {C_DIM}{sane(status)}{C_RESET}"
        out = [head, ""]
        if rec.get("event") == "tool_call":
            out += self._field("args", t.get("args"), 2, C_DIM)
        else:
            out += self._body("result",
                              _as_text((rec.get("response") or {}).get("content")),
                              C_DIM, 2)
            if rec.get("error"):
                out.append("")
                out += self._field("err", rec["error"].get("message"), 2, C_RED)
        return out

    # -- per-event renderers ------------------------------------------------

    def _meta_line(self, rec: dict, lead: str, call: int = 0) -> str:
        """`  ← codex llm_response · gpt-5.5 · 2 msgs · 24 tools · 18.8k chars`.

        Agent and event both stay on the line. With several agents writing into
        the same panel — the default, since `watch` tails every trace dir at
        once — the agent name is the only thing that says whose turn this is,
        and the event name is the cheapest way to tell a request from the reply
        that answered it.

        Bits are dropped from the right until the line fits. Order encodes
        importance (who/what/how long, then counts), so a long model name costs
        the least useful field rather than pushing the line off the panel.
        """
        head = [lead, sane(rec.get("agent", "?")), sane(rec.get("event", "?"))]
        tail: list[str] = []
        for k in ("model", "api_mode"):
            if rec.get(k):
                tail.append(sane(rec[k]))
        req = rec.get("request")
        if isinstance(req, dict):
            for label, key, fmt in (("msgs", "message_count", "{}"),
                                    ("tools", "tool_count", "{}"),
                                    ("tokens", "approx_input_tokens", "~{}")):
                v = req.get(key)
                if v is not None:
                    tail.append(f"{label} {fmt.format(v)}")
            ch = req.get("char_count")
            if ch:
                tail.append(f"{ch / 1000:.1f}k chars")

        # `head` is never dropped: without it the line is unattributable.
        # The arrow carries direction (blue in, green out) so scanning down the
        # panel you can see request/reply pairing without reading the words.
        # `#N` (in the call's colour) says WHICH request/reply pair this is.
        arrow = C_BLUE if str(head[0]).startswith("→") else C_AI
        tag = f"{C_BOLD}{call_hue(call)}#{call}{C_RESET}{C_DIM} " if call else ""
        pre = f"  {arrow}{head[0]}{C_RESET}{C_DIM} {tag}{' · '.join(head[1:])}"
        pre = clip_plain(pre, self.width)
        tail_bits = tail
        while tail_bits and vlen(strip_ansi(pre)) + 3 + vlen(tail_bits[0]) > self.width:
            tail_bits.pop(0)
        if not tail_bits:
            return pre + C_RESET
        return _fit(pre + " · ", " · ".join(tail_bits), "", self.width, C_RESET)

    def _body(self, label: str, text: str, color: str = "", indent: int = 2) -> list[str]:
        """Render one body field. Unfolded: the first real lines, hard-wrapped.

        Unlike the old `_field`, an unfolded body is NOT capped at
        FOLD_THRESHOLD. A model reply is the thing the reader opened this panel
        to see; truncating it to 160 chars to make room for a fold notice is
        backwards. It wraps across as many lines as it needs, and `e` is for
        the genuinely huge fields (system prompt, tool output).
        """
        if not text:
            return []
        if not self.expand:
            return self._wrap_body(label, text, color, indent,
                                   limit=NORMAL_BODY_LINES)
        return self._field(label, text, indent, color)

    def _wrap_body(self, label: str, text: str, color: str, indent: int,
                   limit: int) -> list[str]:
        """Hard-wrap text to the panel, keeping source line structure.

        The label is part of the line, so it comes out of the width budget. A
        `user` label plus its two-space gap is 7 columns; budgeting only
        `width - indent - 2` let every labelled line overshoot the panel by
        that much, which the terminal then hard-wraps and desynchronises the
        whole layout.
        """
        # The label renders as a reverse-video tag — ` you ` — so the role
        # reads as a block marker rather than a word. The pill's padding is
        # part of the label, so the width budget is derived from the assembled
        # head instead of a hand-counted label length (this function exists
        # because hand-counted budgets kept overshooting by exactly that).
        text = sane(text)
        head = (f"{' ' * indent}{C_REV}{C_BOLD}{color} {label} {C_RESET}"
                if label else "")
        # Columns the label block occupies: indent + pill + 2-space gap.
        label_w = (vlen(strip_ansi(head)) - indent) + 2 if label else 0
        avail = avail_for(self.width, indent, label_w)
        lines: list[str] = []
        printed_label = False
        cont = f"{' ' * (indent + label_w)}"

        for raw in text.splitlines()[:limit] or [""]:
            if raw == "":
                if lines:
                    lines.append("")
                continue
            body = raw
            while body:
                chunk, body = _take_cells(body, avail)
                if not chunk:
                    break
                if not printed_label and label:
                    lines.append(f"{head}  {chunk}{C_RESET}" if color else
                                 f"{head}  {chunk}")
                    printed_label = True
                else:
                    lines.append(f"{cont}{chunk}")
                if len(lines) >= limit:
                    break
            if len(lines) >= limit:
                break
        if len(lines) >= limit and text.strip().count("\n") >= limit - 1:
            lines.append(f"{cont}{C_DIM}… {len(text)} chars total · e to expand{C_RESET}")
        return lines

    def _call_banner(self, rec: dict, call: int) -> str:
        """Full-width rule that opens LLM call #N.

        This is the answer to "where does one complete call start?" — before
        it, everything belonged to the previous call (or to your prompt);
        after it, the request, its reply and the tools that reply triggered
        form one block. The number carries the block's colour so the matching
        `#N` on the request and reply lines reads as one pair.
        """
        hue = call_hue(call)
        model = sane(rec.get("model") or "-")
        ts = (rec.get("ts") or "")[11:19]
        label = f"call #{call}"
        tail_txt = f"{model} · {ts}" if ts else model
        # Measure the visible text, then fill the rest of the row with the
        # rule so the divider spans the panel — assembled, never hand-counted.
        fill = max(0, self.width - vlen(f"── {label} · {tail_txt} "))
        line = (f"{C_DIM}── {C_RESET}{C_BOLD}{hue}{label}{C_RESET}"
                f"{C_DIM} · {tail_txt} {C_DIM}{'─' * fill}{C_RESET}")
        # Narrow panel: the fill collapses to zero first, then the tail is
        # clipped — `call #N` sits at the front and survives.
        return clip_plain(line, self.width)

    def _via_line(self, rec: dict) -> str:
        """`  via opencode-go · chat · https://… · max_tok 8192`.

        How the request LEAVES this machine — provider, API mode, endpoint
        and the response ceiling when the adapter records one. The meta line
        says what the request contains; this one says where it goes.
        """
        bits = []
        for v in (rec.get("provider"), rec.get("api_mode"), rec.get("base_url")):
            if v:
                bits.append(sane(str(v)))
        req = rec.get("request")
        if isinstance(req, dict) and req.get("max_tokens"):
            try:
                bits.append(f"max_tok {int(req['max_tokens'])}")
            except (TypeError, ValueError):
                pass
        if not bits:
            return ""
        return _fit(f"  {C_DIM}via {C_WHITE}", " · ".join(bits), "",
                    self.width, C_RESET)

    def _anatomy_line(self, sysp: str, conv: list) -> str:
        """`  anatomy system 12.4k (22%) · conv 41.9k (78%) · msgs u5 a4 t9`.

        The meta line counts messages and tools; this splits the PROMPT ITSELF
        into system vs conversation with their share of the payload, plus the
        role histogram — the two numbers that decide what a request costs.
        """
        sys_c = len(str(sysp or ""))
        conv_c = sum(len(_as_text(m.get("content"))) for m in conv)
        total = sys_c + conv_c
        bits = []
        if sys_c:
            pct = f" ({100 * sys_c // total}%)" if total else ""
            bits.append(f"system {C_BOLD}{sys_c / 1000:.1f}k{C_RESET}{C_DIM}{pct}")
            bits[-1] += C_RESET
        if conv_c:
            pct = f" ({100 * conv_c // total}%)" if total else ""
            bits.append(f"conv {C_BOLD}{conv_c / 1000:.1f}k{C_RESET}{C_DIM}{pct}{C_RESET}")
        u = sum(1 for m in conv if m.get("role") == "user")
        a = sum(1 for m in conv if m.get("role") == "assistant")
        t = sum(1 for m in conv if m.get("role") not in ("user", "assistant"))
        if u or a or t:
            bits.append(f"msgs u{u} a{a} t{t}")
        if not bits:
            return ""
        return _fit(f"  {C_DIM}anatomy ", " · ".join(bits), "",
                    self.width, C_RESET)

    def _render_request(self, rec: dict, agent: str, call: int = 0) -> list[str]:
        req = rec.get("request")
        if isinstance(req, list):
            req = {"messages": req, "message_count": len(req)}
        elif not isinstance(req, dict):
            req = {}

        out: list[str] = []
        if call:
            out.append(self._call_banner(rec, call))
        out.append(self._meta_line(rec, "→", call))
        via = self._via_line(rec)
        if via:
            out.append(via)

        # A system prompt reaches the panel two ways depending on the adapter:
        # as a dedicated `system_prompt` field, or as a `system` message inside
        # `messages` (which is what the Hermes adapter does). Prefer the field
        # but fall back to the message, or the prompt silently disappears.
        sysp = (req.get("system_prompt") or req.get("instructions") or "")
        msgs = [m for m in (req.get("messages") or []) if isinstance(m, dict)]
        if not sysp:
            for m in msgs:
                if m.get("role") == "system":
                    sysp = _as_text(m.get("content"))
                    break
        conv = [m for m in msgs if m.get("role") != "system"]
        anat = self._anatomy_line(sysp, conv)
        if anat:
            out.append(anat)
        # The full prompt is system + conversation + TOOL SCHEMAS — the
        # schemas are a real part of the payload (25 tools ≈ several k
        # tokens) and the count alone says nothing about what the model can
        # call. List the names; `_fit` clips to the panel.
        tools = req.get("tools")
        if isinstance(tools, list) and tools:
            names = []
            for t in tools:
                if isinstance(t, dict):
                    n = t.get("name") or (t.get("function") or {}).get("name")
                else:
                    n = t
                if n:
                    names.append(sane(str(n)))
            if names:
                out.append(_fit(f"  {C_DIM}tools {C_RESET}", ", ".join(names),
                                "", self.width, C_RESET))
        out.append("")
        if sysp:
            out += self._field("system", sysp, 2, C_DIM)
            out.append("")

        # An agent request carries the WHOLE conversation — by turn 20 that is
        # hundreds of messages and megabytes of replay. The panel's job is the
        # exchange in front of you, so history collapses to a count and only the
        # last user message is shown. This is the single biggest readability
        # win in the panel: without it, one llm_request buried every live view.
        conv = [m for m in msgs if m.get("role") != "system"]
        if not conv:
            return out

        # Anchor on the last USER message, not the last message: by the time a
        # turn is on its 2nd LLM call the tail of the request is the previous
        # turn's tool result — raw JSON, and the opposite of what the reader
        # opened the panel for.
        users = [i for i, m in enumerate(conv) if m.get("role") == "user"]
        anchor = users[-1] if users else len(conv) - 1
        if anchor:
            # What the collapse hides, summarized: role histogram (first-seen
            # order) + how many characters of the prompt live back there.
            earlier = conv[:anchor]
            order: list[str] = []
            counts: dict[str, int] = {}
            for m in earlier:
                r = str(m.get("role") or "?")
                if r not in counts:
                    order.append(r)
                    counts[r] = 0
                counts[r] += 1
            roles = " ".join(f"{r[0]}{counts[r]}" for r in order)
            chars = sum(len(_as_text(m.get("content"))) for m in earlier)
            hist = (f"… {anchor} earlier msg{'s' if anchor != 1 else ''}"
                    + (f" ({roles} · {_fmt_tok(chars)} chars)" if roles else ""))
            out.append(_fit(f"  {C_DIM}", hist, "", self.width, C_RESET))

        last = conv[anchor]
        role = last.get("role", "user")
        label = sane("user" if role == "user" else role)
        color = C_USER if role == "user" else C_DIM
        out += self._body(label, _as_text(last.get("content")), color, 2)
        # Score the anchor right where it sits: the you-block's `prompt …`
        # line sits far up the stream by turn 3; inside a request the anchor
        # is the message the reader is actually looking at.
        if role == "user":
            out.append(self._score_line(_as_text(last.get("content"))))
        return out

    def _render_response(self, rec: dict, agent: str, call: int = 0) -> list[str]:
        resp = rec.get("response") or {}
        u = resp.get("usage") or {}
        d = rec.get("duration_ms")
        # Same fitting path as the request meta line: a reply row carries
        # model + duration + six usage figures and will overflow a narrow
        # panel if it is not measured rather than assumed.
        # Green `←` pairs with the blue `→` above it: one request/reply cycle
        # reads as two coloured marks down the left edge. The `#N` (in the
        # call's colour) is the explicit half of that pairing — it matches the
        # `#N` on the request line and on the `call #N` banner.
        tag = f"{C_BOLD}{call_hue(call)}#{call}{C_RESET}{C_DIM} " if call else ""
        pre = clip_plain(
            f"  {C_AI}←{C_RESET}{C_DIM} {tag}{sane(rec.get('model') or '-')}",
            self.width)
        rest: list[str] = []
        if d is not None:
            try:
                rest.append(f"{int(d)}ms")
            except (TypeError, ValueError):
                pass

        def _n(v):
            """usage value as int, or None when absent/unusable."""
            try:
                return None if v is None else int(v)
            except (TypeError, ValueError):
                return None

        # Two numbers raw counts don't give, both derived from usage:
        #   tok   — what THIS call consumed (input + output).  In an agent loop
        #           the input carries the whole history, so this is the cost
        #           of this turn, not of the conversation.
        #   cache — hit rate of that input against the prompt cache.  It is
        #           what separates "13318 input tokens" (expensive) from
        #           "13318 input, 13298 cached" (nearly free).
        #
        # `_n` decides whether the field is PRESENT (absent usage must keep
        # rendering as no counters at all); `total_input_tokens` then supplies
        # the value, repairing records whose `input_tokens` excluded the cache
        # — without it a Pi call rendered `cache 183%` and `tok` undercounted
        # by the cached portion.
        in_raw = _n(u.get("input_tokens"))
        in_t = total_input_tokens(u) if in_raw is not None else None
        out_t = _n(u.get("output_tokens"))
        cache_t = _n(u.get("cache_read_tokens"))
        if in_t is not None or out_t is not None:
            rest.append(f"tok {(in_t or 0) + (out_t or 0)}")
        for label, val in (("in", in_t), ("out", out_t)):
            if val is not None:
                rest.append(f"{label} {val}")
        if cache_t is not None:
            hit = cache_hit_rate(cache_t, in_t or 0)
            if hit is not None:
                # `cache 99.8% ████████ 97152` — rate first with a meter so
                # the KV hit is visible without reading digits; the raw
                # cached tokens stay at the end for the exact figure.
                hue = C_AI if hit >= 90 else (C_YELLOW if hit >= 50 else C_RED)
                rest.append(f"cache {hue}{hit:.1f}%{C_RESET}"
                            f" {hue}{_bar(hit)}{C_RESET}"
                            f" {C_DIM}{cache_t}{C_RESET}")
            else:
                rest.append(f"cache {cache_t}")
        if resp.get("finish_reason"):
            rest.append(sane(resp["finish_reason"]))
        # Drop segments from the head until the whole line fits — but the
        # ruler must be THE SAME ONE _fit uses, or a line that passes here
        # gets decapitated there:
        #   * _fit measures the PREFIX with strip_ansi (display columns) …
        #   * … and clips the BODY with clip(), whose first check is a raw
        #     vlen() that counts escape sequences as visible characters.
        # Measuring the join with strip_ansi under-counted by ~20 columns
        # (one coloured segment ≈ 16 bytes of CSI), so the loop passed a
        # line that clip() then cut mid-escape (`cache 99.8% ████ \x1b[2…`
        # — the cached token count gone). Mirror the hybrid: strip the
        # pre, keep the join raw.
        # Priority order when dropping: head counters first (ms/tok/in/out),
        # then finish — cache dies LAST, it is the figure the reader came
        # for and it must survive a narrow panel.
        while rest and (vlen(strip_ansi(pre)) + 3
                        + vlen(" · ".join(rest))) > self.width:
            drop = next((i for i, s in enumerate(rest)
                         if not s.startswith("cache")), None)
            rest.pop(0 if drop is None else drop)
        if rest:
            head_line = _fit(pre + " · ", " · ".join(rest), "", self.width, C_RESET)
        else:
            head_line = _fit(pre, "", "", self.width, C_RESET)
        out = [head_line, ""]
        out += self._body("ai", _as_text(resp.get("content")), C_AI, 2)
        if resp.get("reasoning"):
            out.append("")
            out += self._field("think", _as_text(resp.get("reasoning")), 2, C_DIM)

        calls = resp.get("tool_calls") or []
        if calls:
            out.append("")
            for c in calls:
                if not isinstance(c, dict):
                    continue
                # A decision is the model's CHOICE, shown where it was made:
                # inside the reply that made it. The tool_call record below
                # then shows execution; this line shows intent (name + the
                # one argument that identifies it), numbered by call block.
                name = sane(c.get("name") or "?")
                prev = sane(_args_preview(c.get("arguments")))
                head = (f"  {C_BOLD}{call_hue(call)}↳{C_RESET}{C_DIM} decide "
                        f"{C_RESET}{C_YELLOW}{name}{C_RESET}{C_DIM}  ")
                out.append(_fit(head, prev, "", self.width, C_RESET))
        if not resp.get("content") and not calls:
            note = rec.get("note")
            if note:
                out += self._field("note", note, 2)
        return out

    def _render_error(self, rec: dict, agent: str, call: int = 0) -> list[str]:
        err = rec.get("error") or {}
        # The failed call keeps its number (in the call's hue) so the reader
        # can tell WHICH request never got a reply; the rest stays red.
        tag = f"{C_BOLD}{call_hue(call)}#{call}{C_RESET}{C_RED} " if call else ""
        head = f"✗ {tag}error · {sane(rec.get('model') or '-')}"
        if err.get("status_code"):
            head += f" · HTTP {err['status_code']}"
        if err.get("retryable") is not None:
            head += f" · retryable={err['retryable']}"
        # The head carries model, status code and retryability — several
        # variable-length parts assembled by string concat, so it gets the
        # same clip every other free-form line gets.
        out = [clip_plain(f"  {C_RED}{head}{C_RESET}", self.width), ""]
        out += self._body("err", _as_text(err.get("message")), C_RED, 2)
        if err.get("type"):
            out.append(f"  {C_DIM}{sane(err['type'])}{C_RESET}")
        return out

    def _score_line(self, text: str) -> str:
        """One line: `prompt 75/100 grade B ████████░░ · specific · action`.

        Score, letter grade AND a block meter — the number alone was too
        easy to miss in the stream. Hue: green ≥70, yellow ≥50, red below.
        Local and deterministic (score_prompt): same text, same number,
        and the prompt never leaves this machine.
        """
        score, dims = score_prompt(text)
        grade = grade_for(score)
        hue = C_AI if score >= 70 else (C_YELLOW if score >= 50 else C_RED)
        detail = " · ".join(dims) if dims else "thin specifics"
        head = (f"  {C_DIM}prompt {C_RESET}{C_BOLD}{hue}{score}{C_RESET}"
                f"{C_DIM}/100 grade {hue}{C_BOLD}{grade}{C_RESET}"
                f"{C_DIM} · {C_RESET}{hue}{_bar(score, 8)}{C_RESET}"
                f"{C_DIM} · {C_RESET}")
        return _fit(head, detail, "", self.width, C_RESET)

    def _render_user(self, rec: dict, agent: str) -> list[str]:
        req = rec.get("request") or {}
        out = []
        texts = [m for m in (req.get("messages") or []) if isinstance(m, dict)]
        for m in texts:
            out += self._body("you", _as_text(m.get("content")), C_USER, 2)
        if rec.get("note"):
            out.append(f"  {C_DIM}{clip(sane(rec['note']), max(10, self.width - 6))}{C_RESET}")
        # One prompt, one score — see _score_line for the format and why the
        # heuristic is local (deterministic, free, content never leaves).
        out.append(self._score_line(
            "".join(_as_text(m.get("content")) for m in texts)))
        return out

    def _render_other(self, rec: dict) -> list[str]:
        note = rec.get("note")
        if not note:
            return []
        return [f"  {C_DIM}· {clip(sane(note).replace(chr(10), ' '), self.width - 4)}{C_RESET}"]

