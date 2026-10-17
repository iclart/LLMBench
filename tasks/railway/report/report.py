#!/usr/bin/env python3
"""Builds the HTML report of one season (docs/DESIGN.md 10).

  report.py --season DIR --runs PATH [PATH...] --out DIR
            [--names FILE] [--env-file FILE]... [--full-transcript]

--runs takes run directories (with run.json) or directories holding them,
up to two levels deep. The report is a directory: index.html (the season's
leaderboard) and runs/<run>.html (one page per run). Every page is
self-contained: no network access, no external scripts or styles.

Only standard runs are ranked: strict mode, the standard time limit, scored
against this season, with the task version and prompt the season was
prepared with. Other runs get pages and appear in their own sections.

--names maps model IDs to display names ({"anthropic/claude-x": "Claude X"}).
--env-file: every value in these KEY=VALUE files is replaced by *** wherever
it appears in a page, as are the runs' web UI passwords and strings shaped
like common API keys. Transcripts keep the first and last lines of each tool
output unless --full-transcript.
"""
import argparse
import difflib
import hashlib
import html
import json
import re
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASK = HERE.parent
sys.path.insert(0, str(TASK))
sys.path.insert(0, str(TASK / "agent"))
import taskinfo  # noqa: E402
import transcript as transcript_format  # noqa: E402

SCENARIOS = ["daily", "spring", "refund_wave"]
THREADS = [1, 4, 16]
KEYS = [f"{sc}/t{t}" for sc in SCENARIOS for t in THREADS]
STANDARD_MINUTES = 150
CURVE_STEP_MIN = 5
SOURCE_SUFFIXES = {".cpp", ".cc", ".cxx", ".h", ".hpp", ".hh", ".inc"}
OUTPUT_HEAD = OUTPUT_TAIL = 20
MAX_LINE = 400
MAX_DIFF_LINES = 4000
MAX_SOURCE_LINES = 30000

KEY_PATTERNS = [
    r"sk-[A-Za-z0-9_\-]{16,}",
    r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}",
    r"AKIA[0-9A-Z]{16}",
    r"gh[pousr]_[A-Za-z0-9]{30,}",
    r"xox[baprs]-[A-Za-z0-9\-]{10,}",
    r"AIza[0-9A-Za-z_\-]{30,}",
]

# Concurrency fingerprint (heuristic): what the code synchronizes with.
SYNC_PATTERNS = {
    "mutex": r"\bstd::(?:recursive_|timed_)?mutex\b",
    "shared_mutex": r"\bstd::shared_(?:timed_)?mutex\b",
    "atomic": r"\bstd::atomic\b|\batomic<",
    "atomic_flag": r"\batomic_flag\b",
    "cas": r"\bcompare_exchange_(?:weak|strong)\b",
    "lock_guard": r"\b(?:lock_guard|unique_lock|scoped_lock)\b",
    "shared_lock": r"\bshared_lock\b",
    "thread_local": r"\bthread_local\b",
    "condition_variable": r"\bcondition_variable\b",
    "memory_order": r"\bmemory_order_\w+",
}
LOCK_ARRAY = re.compile(r"(?:std::)?(?:shared_)?mutex\s+\w+\s*\[|(?:vector|array|deque|unique_ptr)\s*<\s*(?:std::)?(?:shared_)?mutex")
TYPE_HEAD = re.compile(r"\b(?:struct|class)\s+(?:alignas\s*\([^)]*\)\s*)?(\w+)[^{;()]*\{")


def lock_holding_types(code):
    """Names of structs/classes with a mutex member (brace-matched, roughly)."""
    names = []
    for m in TYPE_HEAD.finditer(code):
        depth, i = 1, m.end()
        while i < len(code) and depth:
            depth += {"{": 1, "}": -1}.get(code[i], 0)
            i += 1
        body = code[m.end():i]
        if re.search(r"\bstd::(?:shared_)?(?:timed_)?mutex\s+\w+\s*;", body):
            names.append(m.group(1))
    return names


def sharded_locks(code):
    """A mutex per element: arrays of mutexes, or containers of lock-holding structs."""
    if LOCK_ARRAY.search(code):
        return True
    for name in lock_holding_types(code):
        if re.search(rf"(?:vector|array|deque|unique_ptr|span)\s*<\s*{name}\b|\b{name}\s+\w+\s*\[", code):
            return True
    return False


def load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


# ------------------------------------------------------------------ season

def world_stats(path):
    stats = {}
    try:
        with open(path) as f:
            for line in f:
                parts = line.split()
                if len(parts) == 2 and parts[0] in ("STATIONS", "LINES", "TRAINS"):
                    stats[parts[0].lower()] = int(parts[1])
    except OSError:
        pass
    return stats


def load_intents(path):
    try:
        with open(path) as f:
            for _, line in zip(range(5), f):
                if line.startswith("INTENTS "):
                    return int(line.split()[1])
    except OSError:
        pass
    return None


def load_season(d):
    d = Path(d).resolve()
    meta = load_json(d / "season.json")
    anchors = load_json(d / "anchors.json")
    if not meta or not anchors:
        sys.exit(f"{d} is not a prepared season (season.json and anchors.json are needed)")
    fingerprint = meta.get("fingerprint") or hashlib.sha256((d / "anchors.json").read_bytes()).hexdigest()[:16]
    return {
        "dir": d, "meta": meta, "anchors": anchors, "fingerprint": fingerprint,
        "name": meta.get("name") or d.name,
        "world": world_stats(d / "world.world"),
        "intents": {sc: load_intents(d / f"{sc}.load") for sc in SCENARIOS},
    }


# ------------------------------------------------------------------ runs

def discover(paths):
    found, seen = [], set()
    for p in map(Path, paths):
        cands = [p] if (p / "run.json").exists() else sorted({*p.glob("*/run.json"), *p.glob("*/*/run.json")})
        for c in cands:
            d = (c if c.is_dir() else c.parent).resolve()
            if d not in seen:
                seen.add(d)
                found.append(d)
    return found


def snapshot_minutes(name, run):
    m = re.match(r"(\d+(?:\.\d+)?)(_final)?$", name)
    if not m:
        return None
    if m.group(2) and run.get("elapsed_min") is not None:
        return float(run["elapsed_min"])
    return float(m.group(1))


def classify_failure(text):
    if not text:
        return None
    if text.startswith("crash"):
        return "crash"
    if text.startswith("memory"):
        return "memory"
    return "wrong"


def measurement_status(res, key):
    if res.get("fatal"):
        return "fatal"
    sc = key.split("/")[0]
    fail = classify_failure((res.get("scenarios", {}).get(sc) or {}).get("failure"))
    if fail:
        return fail
    m = res.get("measurements", {}).get(key)
    if not m:
        return "missing"
    return m.get("status", "missing")


def group_key(run):
    return (run.get("model") or "?", run.get("variant") or "", run.get("tool") or "?", bool(run.get("manual")))


def group_label(key, names):
    model, variant, tool, manual = key
    label = names.get(model, model) + (f" ({variant})" if variant else "")
    return f"{label} · {taskinfo.tool_name(tool)}"


def load_run(d, season, names):
    run = load_json(d / "run.json", {})
    snaps = sorted(p.name for p in (d / "snapshots").iterdir() if p.is_dir()) if (d / "snapshots").is_dir() else []
    results = {s: load_json(d / "results" / f"{s}.json") for s in snaps}
    final_name = snaps[-1] if snaps else None
    final = results.get(final_name) if final_name else None
    r = {"dir": d, "run": run, "snaps": snaps, "results": results, "final_name": final_name, "final": final}

    reasons = []
    if run.get("tool") == "reference":
        category = "reference"
    elif not final:
        category = "unscored"
    else:
        if run.get("mode") != "strict":
            reasons.append("试跑模式" if run.get("mode") == "trial" else f"模式 {run.get('mode')}")
        if run.get("minutes") != STANDARD_MINUTES:
            reasons.append(f"时限 {run.get('minutes')} 分钟（标准 {STANDARD_MINUTES}）")
        fp = (final.get("season") or {}).get("fingerprint")
        if not fp:
            reasons.append("评分结果没有赛季指纹（旧版评测，请重新评分）")
        elif fp != season["fingerprint"]:
            reasons.append(f"用另一个赛季评分（{(final.get('season') or {}).get('name')}）")
        if final.get("mode") != "full":
            reasons.append("最后一张快照不是完整评测")
        if final.get("sandbox") == "none":
            reasons.append("评测时没有沙箱")
        tv, sv = run.get("task_version"), season["meta"].get("task_version")
        if not tv:
            reasons.append("运行记录没有任务版本")
        elif sv and tv != sv:
            reasons.append(f"任务包版本 {tv} 与赛季 {sv} 不同")
        ps, sps = run.get("prompt_sha"), season["meta"].get("prompt_sha")
        if ps and sps and ps != sps:
            reasons.append("提示词与赛季不同")
        category = "nonstandard" if reasons else "ranked"
    r["category"], r["reasons"] = category, reasons
    r["key"] = group_key(run)
    r["label"] = group_label(r["key"], names) if category != "reference" else f"参考实现 · {run.get('engine')}"
    return r


def final_summary(r):
    f = r["final"]
    if not f:
        return None
    s = f["scores"]
    statuses = {k: measurement_status(f, k) for k in KEYS}
    return {
        "total": s["total"], "performance": s["performance"], "preference": s["preference"],
        "fatal": f.get("fatal"), "needs_review": f.get("needs_review"),
        "scores": {k: (s["measurements"].get(k) or {}).get("score", 0.0) for k in KEYS},
        "statuses": statuses,
        "gated": sum(1 for k in KEYS if statuses[k] not in ("ok", "timeout")),
        "failed": bool(f.get("fatal")) or any(st in ("wrong", "crash", "memory", "fatal") for st in statuses.values()),
    }


def score_curve(r):
    pts = []
    for name in r["snaps"]:
        res = r["results"].get(name)
        t = snapshot_minutes(name, r["run"])
        if res is None or t is None:
            continue
        pts.append({"name": name, "t": t, "total": res["scores"]["total"], "mode": res.get("mode"),
                    "fatal": res.get("fatal")})
    return pts


def step_value(points, t):
    """Score at time t: the latest snapshot at or before t (0 before the first)."""
    v = 0.0
    for p in points:
        if p["t"] <= t + 1e-9:
            v = p["total"]
    return v


# ------------------------------------------------------------------ transcript

def load_transcript(d):
    t = load_json(d / "logs/transcript.json")
    if t and t.get("format") == transcript_format.FORMAT:
        return t
    msgs = load_json(d / "logs/messages.json")
    if isinstance(msgs, list):
        return transcript_format.from_opencode(msgs)
    return None


def union_ms(intervals):
    total, cur_s, cur_e = 0, None, None
    for s, e in sorted(intervals):
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                total += cur_e - cur_s
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        total += cur_e - cur_s
    return total


def clip_output(text, full):
    lines = (text or "").splitlines()
    lines = [ln if len(ln) <= MAX_LINE else ln[:MAX_LINE] + " …" for ln in lines]
    if full or len(lines) <= OUTPUT_HEAD + OUTPUT_TAIL:
        return "\n".join(lines)
    return "\n".join(lines[:OUTPUT_HEAD] + [f"… 省略 {len(lines) - OUTPUT_HEAD - OUTPUT_TAIL} 行 …"] + lines[-OUTPUT_TAIL:])


MAKE_CHECK = re.compile(r"\bmake\b[^;&|\n]*\bcheck\b")
SCENARIO_LINE = re.compile(r"^== (\w+): (\d+) threads, judged")
JUDGE_LINE = re.compile(r"railjudge: (ok|FAILED), (\d+) calls/s, p99 ([\d.]+) us")
COMPILE_ERROR = re.compile(r"^\S+?:\d+(?::\d+)?: (?:fatal )?error: (.*)$", re.M)


def edit_lines(inp, name):
    """Old/new text of an edit-like tool call as diff lines [(sign, text)]."""
    g = lambda *ks: next((inp[k] for k in ks if isinstance(inp.get(k), str)), None)  # noqa: E731
    path = g("filePath", "file_path", "path") or ""
    old, new = g("oldString", "old_string"), g("newString", "new_string")
    out = []
    if old is not None or new is not None:
        out += [("-", ln) for ln in (old or "").splitlines()] + [("+", ln) for ln in (new or "").splitlines()]
    elif g("content") is not None:
        out += [("+", ln) for ln in g("content").splitlines()]
    elif g("patchText", "patch", "diff") is not None:
        for ln in g("patchText", "patch", "diff").splitlines():
            out.append((ln[:1] if ln[:1] in "+-@" else " ", ln[1:] if ln[:1] in "+- " else ln))
    elif isinstance(inp.get("edits"), list):
        for e in inp["edits"]:
            if isinstance(e, dict):
                out += edit_lines(e, name)[1]
    return path, out


def analyze_transcript(t, run, full):
    events = t["events"]
    if not events:
        return None
    t0 = (run.get("start_epoch") or 0) * 1000 or events[0]["t"]
    mins = lambda ms: round((ms - t0) / 60000, 4) if ms is not None else None  # noqa: E731
    swim, checks, ctx, cum = [], [], [], []
    gen_iv, tool_iv = [], []
    tool_counts, error_msgs = Counter(), Counter()
    compile_fail_calls = 0
    cum_cost = cum_out = 0.0
    any_cost = False
    entries = []
    for e in events:
        if e["kind"] == "assistant":
            end = e.get("end") or e["t"]
            gen_iv.append((e["t"], max(end, e["t"])))
            swim.append({"lane": "model", "s": mins(e["t"]), "e": mins(end)})
            tok = e["tokens"]
            ctx.append([mins(e["t"]), tok["input"] + tok["cache_read"] + tok["cache_write"]])
            cum_out += tok["output"]
            if e.get("cost") is not None:
                any_cost = True
                cum_cost += e["cost"]
            cum.append([mins(end), round(cum_cost, 6), cum_out])
            if e.get("text") or e.get("reasoning"):
                entries.append({"k": "assistant", "t": mins(e["t"]), "text": e.get("text") or "",
                                "reasoning": e.get("reasoning") or ""})
        elif e["kind"] == "user":
            entries.append({"k": "user", "t": mins(e["t"]), "text": e.get("text") or ""})
        elif e["kind"] == "tool":
            end = e.get("end") or e["t"]
            tool_iv.append((e["t"], max(end, e["t"])))
            inp = e.get("input") or {}
            cmd = inp.get("command") if isinstance(inp.get("command"), str) else None
            lane = e["category"]
            out = e.get("output") or ""
            if lane == "shell" and cmd and MAKE_CHECK.search(cmd):
                lane = "check"
                scen, cur = {}, None
                for line in out.splitlines():
                    m = SCENARIO_LINE.match(line)
                    if m:
                        cur = m.group(1)
                    m = JUDGE_LINE.search(line)
                    if m and cur:
                        scen[cur] = {"ok": m.group(1) == "ok", "throughput": int(m.group(2)),
                                     "p99_us": float(m.group(3))}
                passed = "make check: all scenarios passed" in out and not e.get("error")
                checks.append({"t": mins(e["t"]), "passed": passed, "scenarios": scen})
            if lane in ("shell", "check"):
                errs = COMPILE_ERROR.findall(out)
                if errs:
                    compile_fail_calls += 1
                    error_msgs.update(m.strip()[:140] for m in errs)
            tool_counts[e.get("name") or "?"] += 1
            ok = not e.get("error")
            swim.append({"lane": lane, "s": mins(e["t"]), "e": mins(end), "ok": ok,
                         "label": (cmd or e.get("name") or "")[:120]})
            entry = {"k": "tool", "t": mins(e["t"]), "dur_s": round((end - e["t"]) / 1000, 2) if end else None,
                     "name": e.get("name"), "cat": e["category"], "ok": ok, "exit": e.get("exit")}
            if cmd:
                entry["cmd"] = cmd
                if isinstance(inp.get("description"), str):
                    entry["desc"] = inp["description"]
            if e["category"] == "edit":
                path, lines = edit_lines(inp, e.get("name"))
                entry["path"] = path
                if not full and len(lines) > 80:
                    lines = lines[:60] + [(" ", f"… 省略 {len(lines) - 70} 行 …")] + lines[-10:]
                entry["diff"] = lines
            elif not cmd:
                entry["args"] = json.dumps(inp, ensure_ascii=False)[:600]
            entry["out"] = clip_output(out, full) if e["category"] != "edit" else clip_output(out, False)[:2000]
            entries.append(entry)
    elapsed_ms = (run.get("elapsed_min") or 0) * 60000 or (events[-1].get("end") or events[-1]["t"]) - t0
    tool_ms = union_ms(tool_iv)
    gen_ms = max(0, union_ms(gen_iv + tool_iv) - tool_ms)
    first_pass = next((c["t"] for c in checks if c["passed"]), None)
    u = t["usage"]
    cache_den = u["input"] + u["cache_read"] + u["cache_write"]
    return {
        "source": t.get("source"),
        "swim": swim, "checks": checks, "context": ctx, "cumulative": cum, "has_cost": any_cost,
        "entries": entries,
        "stats": {
            "first_pass_min": first_pass,
            "checks": len(checks), "checks_passed": sum(1 for c in checks if c["passed"]),
            "gen_pct": 100 * gen_ms / elapsed_ms if elapsed_ms else None,
            "tool_pct": 100 * tool_ms / elapsed_ms if elapsed_ms else None,
            "max_context": max((c[1] for c in ctx), default=0),
            "cache_hit_pct": 100 * u["cache_read"] / cache_den if cache_den else None,
            "compile_fail_calls": compile_fail_calls,
            "top_errors": error_msgs.most_common(5),
            "tool_counts": tool_counts.most_common(12),
            "steps": u["steps"], "tool_calls": u["tool_calls"],
        },
    }


# ------------------------------------------------------------------ code

def read_sources(src):
    files = {}
    if src.is_dir():
        for f in sorted(src.rglob("*")):
            if f.is_file() and f.suffix in SOURCE_SUFFIXES:
                files[f.relative_to(src).as_posix()] = f.read_text(errors="replace")
    return files


def strip_comments(code):
    code = re.sub(r"/\*.*?\*/", " ", code, flags=re.S)
    return re.sub(r"//[^\n]*", "", code)


def fingerprint(files):
    code = strip_comments("\n".join(files.values()))
    counts = {k: len(re.findall(p, code)) for k, p in SYNC_PATTERNS.items()}
    labels = []
    sharded = sharded_locks(code)
    if counts["shared_mutex"] or counts["shared_lock"]:
        labels.append("读写锁")
    if sharded:
        labels.append("分片锁")
    elif counts["mutex"]:
        labels.append("互斥锁")
    if counts["cas"] or counts["atomic_flag"]:
        labels.append("CAS / 自旋")
    elif counts["atomic"]:
        labels.append("原子变量")
    if counts["thread_local"]:
        labels.append("线程局部")
    if not any(counts[k] for k in ("mutex", "shared_mutex", "atomic", "atomic_flag", "cas")):
        labels.append("无同步")
    return {"counts": counts, "labels": labels}


def code_evolution(r):
    snaps = []
    prev = None
    diffs = []
    for name in r["snaps"]:
        files = read_sources(r["dir"] / "snapshots" / name / "src")
        lines = sum(len(v.splitlines()) for v in files.values())
        snaps.append({"name": name, "t": snapshot_minutes(name, r["run"]), "files": len(files), "lines": lines,
                      "fp": fingerprint(files)})
        if prev is not None:
            out = []
            for path in sorted(set(prev) | set(files)):
                a, b = prev.get(path, ""), files.get(path, "")
                if a != b:
                    out += list(difflib.unified_diff(a.splitlines(), b.splitlines(), f"a/{path}", f"b/{path}",
                                                     lineterm="", n=2))
            added = sum(1 for ln in out if ln.startswith("+") and not ln.startswith("+++"))
            removed = sum(1 for ln in out if ln.startswith("-") and not ln.startswith("---"))
            note = None
            if len(out) > MAX_DIFF_LINES:
                note = f"差异共 {len(out)} 行，只显示前 {MAX_DIFF_LINES} 行"
                out = out[:MAX_DIFF_LINES]
            diffs.append({"from": snaps[-2]["name"], "to": name, "added": added, "removed": removed,
                          "lines": out, "note": note})
        prev = files
    final_files = read_sources(r["dir"] / "snapshots" / r["final_name"] / "src") if r["final_name"] else {}
    source, budget = [], MAX_SOURCE_LINES
    for path, text in final_files.items():
        lines = text.splitlines()
        source.append({"path": path, "text": "\n".join(lines[:budget]),
                       "note": None if len(lines) <= budget else f"只显示前 {budget} 行"})
        budget = max(0, budget - len(lines))
    return {"snapshots": snaps, "diffs": diffs, "source": source}


# ------------------------------------------------------------------ distributions

def cdf_points(hist, per_octave):
    total = sum(hist or [])
    if not total:
        return None
    pts, cum = [], 0
    for i, c in enumerate(hist):
        if not c:
            continue
        lo = 2 ** (i // per_octave) * (1 + (i % per_octave) / per_octave)
        if not pts:
            pts.append([lo / 1000, 0.0])
        cum += c
        nxt = i + 1
        hi = 2 ** (nxt // per_octave) * (1 + (nxt % per_octave) / per_octave)
        pts.append([hi / 1000, cum / total])
    return pts


def dist_series(m):
    """Latency CDFs (microseconds) and the throughput timeline of one measurement."""
    d = (m or {}).get("dist")
    if not d:
        return None
    hist = d.get("histogram") or {}
    per = hist.get("buckets_per_octave", 4)
    tl = (d.get("timeline") or {}).get("completed") or []
    reps = sorted(x["throughput"] for x in m.get("repeats") or []) or [m.get("throughput")]
    thr = reps[(len(reps) - 1) // 2] or 0
    total = sum(tl)
    bins = len(tl)
    timeline = [[round(100 * (i + 0.5) / bins, 2), c * bins / total * thr] for i, c in enumerate(tl)] if total else None
    return {"query": cdf_points(hist.get("query"), per), "book": cdf_points(hist.get("book"), per),
            "timeline": timeline}


def measurement_rows(r, season):
    f = r["final"]
    anchors = season["anchors"]
    rows = []
    for k in KEYS:
        m = f.get("measurements", {}).get(k) or {}
        sc = f["scores"]["measurements"].get(k) or {}
        b, s = anchors["baseline"].get(k) or {}, anchors["strong"].get(k) or {}
        rows.append({
            "key": k, "status": measurement_status(f, k), "score": sc.get("score", 0.0),
            "thr_score": sc.get("throughput_score", 0.0), "lat_score": sc.get("latency_score", 0.0),
            "throughput": m.get("throughput"), "query_p99_us": (m.get("query_p99_ns") or 0) / 1000 or None,
            "book_p99_us": (m.get("book_p99_ns") or 0) / 1000 or None, "init_s": m.get("init_s"),
            "maxrss_mb": m.get("maxrss_mb"), "mem_limit_mb": m.get("mem_limit_mb"), "runs": m.get("runs"),
            "repeats": m.get("repeats"), "timeout_s": m.get("timeout_s"),
            "base": {"throughput": b.get("throughput"), "query_p99_us": (b.get("query_p99_ns") or 0) / 1000,
                     "book_p99_us": (b.get("book_p99_ns") or 0) / 1000},
            "strong": {"throughput": s.get("throughput"), "query_p99_us": (s.get("query_p99_ns") or 0) / 1000,
                       "book_p99_us": (s.get("book_p99_ns") or 0) / 1000},
            "dist": {"sub": dist_series(m), "base": dist_series(b), "strong": dist_series(s)},
        })
    return rows


def correctness(f):
    out = []
    for sc in SCENARIOS:
        info = f.get("scenarios", {}).get(sc) or {}
        out.append({"scenario": sc, "failure": info.get("failure"), "error_counts": info.get("error_counts"),
                    "errors": (info.get("errors") or [])[:20]})
    logs = {}
    for k in ("compile_log", "tsan_report"):
        if f.get(k):
            logs[k] = f[k][-6000:]
    for k, v in (f.get("stderr") or {}).items():
        logs[f"stderr {k}"] = v[-3000:]
    return {"scenarios": out, "logs": logs, "flags": f.get("flags") or []}


def preference(f, season):
    pt = (f.get("scores") or {}).get("preference_threads", 1)
    rows = []
    for sc in SCENARIOS:
        k = f"{sc}/t{pt}"
        q = (f.get("measurements", {}).get(k) or {}).get("quiet")
        sq = (season["anchors"]["strong"].get(k) or {}).get("quiet")
        rows.append({"scenario": sc, "window": q and q.get("window_rate"), "together": q and q.get("together_rate"),
                     "strong_window": sq and sq.get("window_rate"), "strong_together": sq and sq.get("together_rate")})
    return {"threads": pt, "rows": rows}


# ------------------------------------------------------------------ pages

def run_id(r, used):
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", r["dir"].name).strip("-") or "run"
    rid, n = base, 2
    while rid in used:
        rid, n = f"{base}-{n}", n + 1
    used.add(rid)
    return rid


def run_meta(r):
    run = r["run"]
    usage = run.get("usage") or {}
    return {
        "id": r["id"], "label": r["label"], "category": r["category"], "reasons": r["reasons"],
        "dir": r["dir"].name, "model": run.get("model"), "variant": run.get("variant"),
        "tool": run.get("tool"), "tool_name": taskinfo.tool_name(run.get("tool")), "tool_version": run.get("tool_version"),
        "manual": bool(run.get("manual")), "mode": run.get("mode"), "minutes": run.get("minutes"),
        "start": run.get("start"), "elapsed_min": run.get("elapsed_min"), "end_reason": run.get("end_reason"),
        "resumes": run.get("resumes"), "usage": usage, "cost": usage.get("cost"),
        "tokens": (usage.get("input") or 0) + (usage.get("output") or 0) + (usage.get("cache_read") or 0)
        if usage else None,
        "slot": r.get("slot"), "page": f"runs/{r['id']}.html",
        "final": final_summary(r), "curve": score_curve(r),
    }


def build_index(season, runs):
    ranked = [r for r in runs if r["category"] == "ranked"]
    groups = {}
    for r in ranked:
        groups.setdefault(r["key"], []).append(r)
    rows = []
    for key, rs in groups.items():
        finals = [final_summary(r) for r in rs]
        totals = [f["total"] for f in finals]
        costs = [r["run"].get("usage", {}).get("cost") if r["run"].get("usage") else None for r in rs]
        costs = [c for c in costs if c]
        tokens = [run_meta(r)["tokens"] for r in rs]
        versions = sorted({str(r["run"].get("tool_version")) for r in rs if r["run"].get("tool_version")})
        ends = Counter(r["run"].get("end_reason") for r in rs)
        curves = [score_curve(r) for r in rs]
        grid = [i * CURVE_STEP_MIN for i in range(STANDARD_MINUTES // CURVE_STEP_MIN + 1)]
        rows.append({
            "key": list(key), "label": group_label(key, NAMES), "manual": key[3], "n": len(rs),
            "mean": mean(totals), "min": min(totals), "max": max(totals),
            "perf": mean(f["performance"] for f in finals), "pref": mean(f["preference"] for f in finals),
            "pass_rate": mean(1 - f["gated"] / len(KEYS) for f in finals),
            "elapsed": mean(r["run"].get("elapsed_min") for r in rs),
            "ends": dict(ends), "cost": mean(costs) if costs else None,
            "tokens": mean(t for t in tokens if t), "versions": versions,
            "per_usd": (mean(totals) / mean(costs)) if costs and mean(costs) else None,
            "heat": {k: mean(f["scores"][k] for f in finals) for k in KEYS},
            "heat_failed": {k: sum(1 for f in finals if f["statuses"][k] not in ("ok", "timeout")) for k in KEYS},
            "curve": [[t, mean(step_value(c, t) for c in curves)] for t in grid],
            "runs": [{"id": r["id"], "page": f"runs/{r['id']}.html", "total": f["total"], "failed": f["failed"],
                      "start": r["run"].get("start")} for r, f in zip(rs, finals)],
        })
    rows.sort(key=lambda g: -g["mean"])
    for i, g in enumerate(rows):
        g["slot"] = i + 1 if i < 8 else 0
        for rr in groups[tuple(g["key"])]:
            rr["slot"] = g["slot"]
    a = season["anchors"]
    anchor_rows = [{"key": k, **{f"{e}_{m}": (a[e].get(k) or {}).get(m) for e in ("baseline", "strong")
                                 for m in ("throughput", "query_p99_ns", "book_p99_ns", "wall_s")}} for k in KEYS]
    meta = season["meta"]
    return {
        "page": "index",
        "season": {"name": season["name"], "fingerprint": season["fingerprint"], "created": meta.get("created"),
                   "task_version": meta.get("task_version"), "machine": meta.get("machine"),
                   "sandbox": meta.get("sandbox"), "cpus": meta.get("cpus"), "world": season["world"],
                   "intents": season["intents"], "anchors": anchor_rows,
                   "harness_mb": a.get("harness_mb")},
        "groups": rows,
        "others": [run_meta(r) for r in runs if r["category"] != "ranked"],
        "keys": KEYS,
    }


def build_run_page(r, season, full):
    page = {"page": "run", "season": {"name": season["name"]}, "keys": KEYS, "run": run_meta(r),
            "events": r["run"].get("events") or [], "machine": r["run"].get("machine")}
    if r["final"]:
        page["measurements"] = measurement_rows(r, season)
        page["correctness"] = correctness(r["final"])
        page["preference"] = preference(r["final"], season)
        page["build"] = r["final"].get("build")
        page["eval_machine"] = r["final"].get("machine")
    t = load_transcript(r["dir"])
    page["activity"] = analyze_transcript(t, r["run"], full) if t else None
    page["code"] = code_evolution(r)
    return page


# ------------------------------------------------------------------ output

class Redactor:
    def __init__(self, env_files, runs):
        secrets = set()
        for f in env_files:
            for line in Path(f).read_text().splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    v = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if len(v) >= 6:
                        secrets.add(v)
        for r in runs:
            web = r["dir"] / "web.txt"
            try:
                for line in web.read_text().splitlines():
                    if line.startswith("password:"):
                        v = line.split(":", 1)[1].strip()
                        if len(v) >= 6:
                            secrets.add(v)
            except OSError:
                pass
        self.secrets = sorted(secrets, key=len, reverse=True)
        self.patterns = [re.compile(p) for p in KEY_PATTERNS]
        self.count = 0

    def __call__(self, text):
        for s in self.secrets:
            for form in {s, json.dumps(s)[1:-1]}:
                if form in text:
                    self.count += text.count(form)
                    text = text.replace(form, "***")
        for p in self.patterns:
            text, n = p.subn("***", text)
            self.count += n
        return text


def render(page_data, title, assets, redact):
    data = redact(json.dumps(page_data, ensure_ascii=False, separators=(",", ":"), default=str))
    data = data.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="generator" content="LLMBench report.py">
<title>{html.escape(title)}</title>
<script>try{{var t=localStorage.getItem("llmbench-theme");if(t)document.documentElement.dataset.theme=t;}}catch(e){{}}</script>
<style>{assets['css']}</style>
</head>
<body>
<div class="wrap" id="app"><noscript>这份报告需要启用 JavaScript 才能显示。</noscript></div>
<script type="application/json" id="data">{data}</script>
<script>{assets['js']}</script>
</body>
</html>
"""


NAMES = {}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--season", required=True)
    ap.add_argument("--runs", required=True, nargs="+")
    ap.add_argument("--out", required=True)
    ap.add_argument("--names", help="JSON file mapping model IDs to display names")
    ap.add_argument("--env-file", action="append", default=[], help="redact every value in this KEY=VALUE file")
    ap.add_argument("--full-transcript", action="store_true", help="keep tool outputs whole")
    a = ap.parse_args()

    NAMES.update(load_json(a.names, {}) if a.names else {})
    season = load_season(a.season)
    dirs = discover(a.runs)
    if not dirs:
        sys.exit("no run directories (with run.json) found")
    runs, used = [], set()
    for d in dirs:
        r = load_run(d, season, NAMES)
        r["id"] = run_id(r, used)
        runs.append(r)
    index = build_index(season, runs)
    assets = {"css": (HERE / "report.css").read_text(), "js": (HERE / "report.js").read_text()}
    redact = Redactor(a.env_file, runs)

    out = Path(a.out)
    (out / "runs").mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(render(index, f"LLMBench · {season['name']}", assets, redact))
    for r in runs:
        page = build_run_page(r, season, a.full_transcript)
        page["index"] = "../index.html"
        (out / "runs" / f"{r['id']}.html").write_text(
            render(page, f"{r['label']} · {r['dir'].name}", assets, redact))
    cats = Counter(r["category"] for r in runs)
    print(f"report: {out / 'index.html'}  ({len(runs)} runs: {cats.get('ranked', 0)} ranked, "
          f"{cats.get('nonstandard', 0)} non-standard, {cats.get('reference', 0)} reference, "
          f"{cats.get('unscored', 0)} unscored; {redact.count} secret(s) redacted)", file=sys.stderr)
    for r in runs:
        if r["category"] == "unscored":
            print(f"  not scored yet: {r['dir']}\n    python3 {TASK / 'eval/railway_eval.py'} snapshots --season "
                  f"{season['dir']} --snapshots {r['dir'] / 'snapshots'} --out {r['dir'] / 'results'}",
                  file=sys.stderr)


if __name__ == "__main__":
    main()
