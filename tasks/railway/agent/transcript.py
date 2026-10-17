#!/usr/bin/env python3
"""Converts agent session logs into LLMBench's common transcript format.

  transcript.py opencode    MESSAGES.json OUT.json   # run_opencode.py's logs/messages.json
  transcript.py claude-code SESSION.jsonl OUT.json   # ~/.claude/projects/<project>/<session>.jsonl

The report (report/report.py) reads only this format, so supporting another
agent harness means writing one more converter here. The format:

  {"format": "llmbench-transcript/1", "source": "opencode",
   "events": [...], "usage": {...}}

Events are in time order; times are Unix epoch milliseconds.
  {"kind": "user", "t": ms, "text": str}
  {"kind": "assistant", "t": ms, "end": ms|null, "model": str|null, "text": str,
   "reasoning": str, "tokens": {input, output, reasoning, cache_read, cache_write}, "cost": float|null}
  {"kind": "tool", "t": ms, "end": ms|null, "name": str, "category": shell|edit|read|other,
   "input": {...}, "output": str, "exit": int|null, "error": bool}
usage sums the assistant steps: input, output, reasoning, cache_read,
cache_write, cost (null when the harness reports none), steps, tool_calls.
"""
import json
import sys
from datetime import datetime
from pathlib import Path

FORMAT = "llmbench-transcript/1"

CATEGORIES = {
    "shell": {"bash", "shell"},
    "edit": {"edit", "write", "patch", "apply_patch", "multiedit", "notebookedit"},
    "read": {"read", "glob", "grep", "list", "ls", "find"},
}


def category(name):
    n = (name or "").lower()
    for cat, names in CATEGORIES.items():
        if n in names:
            return cat
    return "other"


def _tokens(input=0, output=0, reasoning=0, cache_read=0, cache_write=0):
    return {"input": int(input or 0), "output": int(output or 0), "reasoning": int(reasoning or 0),
            "cache_read": int(cache_read or 0), "cache_write": int(cache_write or 0)}


def _usage(events, cost=None):
    total = _tokens()
    steps = tools = 0
    summed_cost, any_cost = 0.0, False
    for e in events:
        if e["kind"] == "assistant":
            steps += 1
            for k in total:
                total[k] += e["tokens"][k]
            if e.get("cost") is not None:
                summed_cost += e["cost"]
                any_cost = True
        elif e["kind"] == "tool":
            tools += 1
    if cost is None and any_cost:
        cost = summed_cost
    return {**total, "cost": cost, "steps": steps, "tool_calls": tools}


def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(_text_of(c) for c in content)
    if isinstance(content, dict):
        if content.get("type") in ("text", None) and "text" in content:
            return str(content["text"])
        if "content" in content:
            return _text_of(content["content"])
    return ""


def from_opencode(messages):
    """OpenCode 1.x/2.x v2-API session messages (GET /api/session/{id}/message)."""
    events = []
    for m in messages:
        kind = m.get("type")
        t = (m.get("time") or {}).get("created")
        if kind == "user":
            events.append({"kind": "user", "t": t, "text": m.get("text") or _text_of(m.get("content"))})
        elif kind == "assistant":
            texts, thoughts, tools = [], [], []
            for c in m.get("content") or []:
                if c.get("type") == "text":
                    texts.append(c.get("text") or "")
                elif c.get("type") == "reasoning":
                    thoughts.append(c.get("text") or "")
                elif c.get("type") == "tool":
                    st = c.get("state") or {}
                    meta = st.get("metadata") or st.get("structured") or {}  # 2.x | 1.x
                    exit_code = meta.get("exit")
                    tt = c.get("time") or {}
                    tools.append({
                        "kind": "tool", "t": tt.get("created", t), "end": tt.get("completed"),
                        "name": c.get("name"), "category": category(c.get("name")),
                        "input": st.get("input") or {}, "output": _text_of(st.get("content")) or str(st.get("error") or ""),
                        "exit": exit_code if isinstance(exit_code, int) else None,
                        "error": st.get("status") == "error" or (isinstance(exit_code, int) and exit_code != 0),
                    })
            tok = m.get("tokens") or {}
            cache = tok.get("cache") or {}
            events.append({
                "kind": "assistant", "t": t, "end": (m.get("time") or {}).get("completed"),
                "model": "/".join(x for x in ((m.get("model") or {}).get("providerID"), (m.get("model") or {}).get("id")) if x) or None,
                "text": "\n".join(texts), "reasoning": "\n".join(thoughts),
                "tokens": _tokens(tok.get("input"), tok.get("output"), tok.get("reasoning"), cache.get("read"), cache.get("write")),
                "cost": float(m["cost"]) if m.get("cost") is not None else None,
            })
            events.extend(tools)
    events = [e for e in events if e.get("t") is not None]
    events.sort(key=lambda e: e["t"])
    return {"format": FORMAT, "source": "opencode", "events": events, "usage": _usage(events)}


def _ms(stamp):
    return int(datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() * 1000) if stamp else None


def from_claude_code(lines):
    """Claude Code session log: one JSON object per line. An assistant message
    is spread over several lines (one per content block) sharing message.id;
    tool results come back in user lines; cost-state lines carry the total."""
    steps, order, results, events = {}, [], {}, []
    total_cost = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        kind, t = d.get("type"), _ms(d.get("timestamp"))
        if kind == "cost-state":
            total_cost = d.get("totalCostUSD", total_cost)
            continue
        msg = d.get("message") or {}
        if kind == "assistant":
            mid = msg.get("id") or d.get("uuid")
            if mid not in steps:
                u = msg.get("usage") or {}
                steps[mid] = {
                    "kind": "assistant", "t": t, "end": t, "model": msg.get("model"), "text": "", "reasoning": "",
                    "tokens": _tokens(u.get("input_tokens"), u.get("output_tokens"),
                                      (u.get("output_tokens_details") or {}).get("thinking_tokens"),
                                      u.get("cache_read_input_tokens"), u.get("cache_creation_input_tokens")),
                    "cost": None, "sidechain": bool(d.get("isSidechain")), "tools": [],
                }
                order.append(mid)
            step = steps[mid]
            step["end"] = max(step["end"] or t, t or 0) or None
            for c in msg.get("content") or []:
                if c.get("type") == "text":
                    step["text"] += c.get("text") or ""
                elif c.get("type") == "thinking":
                    step["reasoning"] += c.get("thinking") or ""
                elif c.get("type") == "tool_use":
                    step["tools"].append({"kind": "tool", "t": t, "end": None, "name": c.get("name"),
                                          "category": category(c.get("name")), "input": c.get("input") or {},
                                          "output": "", "exit": None, "error": False, "id": c.get("id")})
        elif kind == "user":
            content = msg.get("content")
            if isinstance(content, list) and any(c.get("type") == "tool_result" for c in content):
                for c in content:
                    if c.get("type") == "tool_result":
                        results[c.get("tool_use_id")] = (t, _text_of(c.get("content")), bool(c.get("is_error")))
            elif not d.get("isMeta"):
                text = _text_of(content)
                if text:
                    events.append({"kind": "user", "t": t, "text": text})
    for mid in order:
        step = steps.pop(mid)
        tools = step.pop("tools")
        events.append(step)
        for tool in tools:
            got = results.get(tool.pop("id"))
            if got:
                tool["end"], tool["output"], tool["error"] = got
            events.append(tool)
    events = [e for e in events if e.get("t") is not None]
    events.sort(key=lambda e: e["t"])
    return {"format": FORMAT, "source": "claude-code", "events": events, "usage": _usage(events, total_cost)}


CONVERTERS = {
    "opencode": lambda path: from_opencode(json.loads(Path(path).read_text())),
    "claude-code": lambda path: from_claude_code(Path(path).read_text(errors="replace").splitlines()),
}


def main():
    if len(sys.argv) != 4 or sys.argv[1] not in CONVERTERS:
        sys.exit(__doc__)
    t = CONVERTERS[sys.argv[1]](sys.argv[2])
    Path(sys.argv[3]).write_text(json.dumps(t, ensure_ascii=False, indent=1))
    u = t["usage"]
    print(f"{len(t['events'])} events, {u['steps']} steps, {u['tool_calls']} tool calls, cost {u['cost']}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
