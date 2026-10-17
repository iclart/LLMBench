#!/usr/bin/env python3
"""Smoke test for report/report.py.

Prepares a tiny season, fabricates runs with snapshots and session logs (an
OpenCode run, a manual Claude Code run, a trial run, a reference run and an
unscored one), scores them with railway_eval.py, builds the report and
checks it: every page exists and embeds valid JSON, nothing loads from the
network, the planted secrets are redacted, and the runs land in the right
sections. --keep leaves the work directory for a look in a browser.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK))
import taskinfo  # noqa: E402

SECRET = "sk-test-0123456789abcdefghijklmnop"
ENV_SECRET = "plain-secret-value-42"
START = 1792000000.0


def opencode_messages(t0_ms):
    """OpenCode-style session messages: edits, a failing then passing make check."""
    def tool(i, t, name, inp, out, exit_code=0, dur=2000):
        return {"type": "tool", "id": f"call_{i}", "name": name,
                "state": {"status": "completed", "input": inp, "content": [{"type": "text", "text": out}],
                          "metadata": {"exit": exit_code}},
                "time": {"created": t, "ran": t + 5, "completed": t + dur}}

    def step(i, t, content, inp=20000, out=800, cost=0.05):
        return {"id": f"msg_a{i}", "type": "assistant", "time": {"created": t, "completed": t + 60000},
                "model": {"providerID": "fake", "id": "model-a"}, "content": content, "cost": cost,
                "tokens": {"input": inp, "output": out, "reasoning": 100, "cache": {"read": inp * 3, "write": 500}}}

    m = t0_ms
    check_fail = ("== daily: query answers vs reference\nsrc/solution.cpp:12:5: error: 'foo' was not declared in this scope\n"
                  "make check: FAILED\n10 min 0 s left before the forced submission\n")
    check_ok = "\n".join(f"== {s}: 4 threads, judged\nrailjudge: ok, {n} calls/s, p99 80.5 us"
                         for s, n in (("daily", 120000), ("spring", 210000), ("refund_wave", 150000)))
    check_ok += "\nmake check: all scenarios passed\n90 min 0 s left before the forced submission\n"
    return [
        {"id": "msg_u1", "type": "user", "time": {"created": m}, "text": "（提示词）"},
        step(1, m + 1000, [{"type": "reasoning", "text": "先读规格。"}, {"type": "text", "text": "我先读 TASK.md。"},
                           tool(1, m + 30000, "read", {"filePath": "TASK.md"}, "# 任务\n" * 50)]),
        step(2, m + 600000, [{"type": "text", "text": "写第一版。"},
                             tool(2, m + 620000, "write", {"filePath": "src/solution.cpp", "content": "int x;\n" * 120}, "ok"),
                             tool(3, m + 700000, "bash", {"command": "make check", "description": "test"}, check_fail, 1, 20000)]),
        step(3, m + 1500000, [{"type": "text", "text": f"不小心打印了密钥 {SECRET} 和 {ENV_SECRET}"},
                              tool(4, m + 1510000, "edit", {"filePath": "src/solution.cpp", "oldString": "int x;", "newString": "long x;"}, "ok"),
                              tool(5, m + 1600000, "bash", {"command": "make check"}, check_ok, 0, 45000)], inp=60000),
        {"id": "msg_u2", "type": "user", "time": {"created": m + 1800000}, "text": "（系统消息）现在是 10:30，距离强制收卷还有 120 分钟。"},
        step(4, m + 1810000, [{"type": "text", "text": "继续优化。"},
                              tool(6, m + 1820000, "bash", {"command": "make submit"}, "Submitted.")], inp=40000),
    ]


def claude_code_log(t0_ms):
    iso = lambda ms: time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ms / 1000)) + f".{ms % 1000:03d}Z"  # noqa: E731
    usage = {"input_tokens": 5, "output_tokens": 400, "cache_read_input_tokens": 30000, "cache_creation_input_tokens": 2000,
             "output_tokens_details": {"thinking_tokens": 50}}
    lines = [
        {"type": "user", "timestamp": iso(t0_ms), "message": {"role": "user", "content": "（提示词）"}},
        {"type": "assistant", "timestamp": iso(t0_ms + 5000), "message": {"id": "m1", "model": "claude-x", "role": "assistant", "usage": usage,
                                                                          "content": [{"type": "thinking", "thinking": "读规格"}]}},
        {"type": "assistant", "timestamp": iso(t0_ms + 6000), "message": {"id": "m1", "model": "claude-x", "role": "assistant", "usage": usage,
                                                                          "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "make check"}}]}},
        {"type": "user", "timestamp": iso(t0_ms + 40000), "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "is_error": False,
             "content": "== daily: 4 threads, judged\nrailjudge: ok, 99000 calls/s, p99 90.0 us\nmake check: all scenarios passed\n"}]}},
        {"type": "assistant", "timestamp": iso(t0_ms + 50000), "message": {"id": "m2", "model": "claude-x", "role": "assistant", "usage": usage,
                                                                           "content": [{"type": "tool_use", "id": "t2", "name": "Edit",
                                                                                        "input": {"file_path": "src/a.cpp", "old_string": "a", "new_string": "b"}}]}},
        {"type": "user", "timestamp": iso(t0_ms + 51000), "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t2", "content": "edited"}]}},
        {"type": "cost-state", "totalCostUSD": 3.5},
    ]
    return "\n".join(json.dumps(x) for x in lines) + "\n"


def make_run(root, name, run, snapshots, transcript=None, messages=None):
    d = root / name
    (d / "logs").mkdir(parents=True)
    for snap, src in snapshots:
        (d / "snapshots" / snap / "src").mkdir(parents=True)
        shutil.copy(src, d / "snapshots" / snap / "src" / "solution.cpp")
    (d / "run.json").write_text(json.dumps(run))
    if messages is not None:
        (d / "logs/messages.json").write_text(json.dumps(messages))
    if transcript is not None:
        (d / "logs/session.jsonl").write_text(transcript)
    (d / "web.txt").write_text("user: opencode\npassword: web-password-xyz\n")
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--strong", action="store_true", help="the strong reference is available")
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()
    work = Path(a.work)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    py = sys.executable
    ev = [py, str(TASK / "eval/railway_eval.py"), "--build", a.build, "--sandbox", "none"]
    engine = "strong" if a.strong else "baseline"

    def run(argv, what, ok=(0,)):
        p = subprocess.run([str(x) for x in argv], capture_output=True, text=True)
        if p.returncode not in ok:
            sys.exit(f"{what} failed ({p.returncode}):\n{p.stdout[-2000:]}\n{p.stderr[-4000:]}")
        return p

    season = work / "season"
    run(ev + ["prepare", "--season", season, "--configs", TASK / "configs/public", "--seed", "7",
              "--set", "intents=1500", "--set", "quiet_intents=150", "--anchor-repeats", "1",
              "--answers-engine", engine, "--strong-engine", engine], "prepare")

    stub, base = TASK / "stub/solution.cpp", TASK / "reference/baseline/baseline.cpp"
    good = base  # never the strong reference: the report embeds the submitted source
    ver = {"task_version": taskinfo.task_version(), "prompt_sha": taskinfo.prompt_sha(), "minutes": taskinfo.MINUTES}
    runs = work / "runs"
    t0 = int(START * 1000)
    common = {**ver, "start": "2026-10-17 10:00:00", "start_epoch": START, "resumes": 1,
              "events": [{"t_min": 30, "kind": "snapshot", "name": "030"}, {"t_min": 30, "kind": "notice", "left_min": 75},
                         {"t_min": 31, "kind": "resume", "n": 1}, {"t_min": 31, "kind": "submitted"}]}
    make_run(runs, "oc-model-a-1", {**common, "tool": "opencode", "tool_version": "2.0.30", "model": "fake/model-a",
                                    "mode": "strict", "elapsed_min": 31, "end_reason": "submitted",
                                    "usage": {"cost": 0.2, "input": 140000, "output": 3200}},
             [("030", stub), ("031_final", good)], messages=opencode_messages(t0))
    make_run(runs, "oc-model-a-2", {**common, "tool": "opencode", "tool_version": "2.0.31", "model": "fake/model-a",
                                    "mode": "strict", "elapsed_min": 105, "end_reason": "deadline",
                                    "usage": {"cost": 0.3, "input": 150000, "output": 4000}},
             [("030", base), ("105_final", stub)], messages=opencode_messages(t0))
    cc = make_run(runs, "cc-model-b", {**common, "tool": "claude-code", "tool_version": "2.1.0", "manual": True,
                                       "model": "anthropic/model-b", "variant": "high", "mode": "strict",
                                       "elapsed_min": 90, "end_reason": "submitted", "usage": None},
                  [("030", base), ("090_final", base)], transcript=claude_code_log(t0))
    make_run(runs, "trial-model-c", {**common, "tool": "opencode", "model": "fake/model-c", "mode": "trial",
                                     "elapsed_min": 105, "end_reason": "deadline"}, [("105_final", base)])
    make_run(runs, "unscored-model-d", {**common, "tool": "opencode", "model": "fake/model-d", "mode": "strict",
                                        "elapsed_min": 105, "end_reason": "deadline"}, [("105_final", base)])
    make_run(runs, "ref-baseline", {**ver, "tool": "reference", "engine": "baseline", "model": "reference/baseline",
                                    "mode": "reference", "minutes": None, "elapsed_min": 0.1, "end_reason": "submitted"},
             [("000", stub), ("001_final", base)])

    run([py, TASK / "agent/run_manual.py", "attach", "--out", cc, "--transcript", "claude-code", cc / "logs/session.jsonl"],
        "attach")
    # Score each distinct source once (lite for intermediate snapshots, full for
    # the last), as `railway_eval.py snapshots` would, and reuse the results.
    scored = {}
    for d in sorted(runs.iterdir()):
        if d.name == "unscored-model-d":
            continue
        snaps = sorted(p.name for p in (d / "snapshots").iterdir())
        (d / "results").mkdir()
        for i, snap in enumerate(snaps):
            src = d / "snapshots" / snap / "src"
            lite = i < len(snaps) - 1
            key = ((src / "solution.cpp").read_bytes(), lite)
            if key not in scored:
                out = work / f"result_{len(scored)}.json"
                run(ev + ["evaluate", "--season", season, "--src", src, "--out", out] + (["--lite"] if lite else []),
                    f"scoring {d.name}/{snap}", ok=(0, 1))  # 1: a fatal result, still a result
                res = json.loads(out.read_text())
                res["sandbox"] = "bwrap"  # stands in for the official sandbox, which CI may lack
                scored[key] = res
            (d / "results" / f"{snap}.json").write_text(json.dumps(scored[key]))

    env = work / "keys.env"
    env.write_text(f"FAKE_API_KEY={ENV_SECRET}\n")
    report = work / "report"
    p = run([py, TASK / "report/report.py", "--season", season, "--runs", runs, "--out", report, "--env-file", env],
            "report.py")
    print(p.stderr.strip())

    pages = [report / "index.html"] + sorted((report / "runs").glob("*.html"))
    if len(pages) != 7:
        sys.exit(f"expected 7 pages, got {[p.name for p in pages]}")
    for page in pages:
        text = page.read_text()
        for bad in (SECRET, ENV_SECRET, "web-password-xyz"):
            if bad in text:
                sys.exit(f"{page.name}: secret {bad!r} not redacted")
        if re.search(r"<(script|link|img|iframe)[^>]+(src|href)=", text):
            sys.exit(f"{page.name}: loads an external resource")
        m = re.search(r'<script type="application/json" id="data">(.*?)</script>', text, re.S)
        if not m:
            sys.exit(f"{page.name}: no embedded data")
        json.loads(m.group(1))
    index = json.loads(re.search(r'id="data">(.*?)</script>', (report / "index.html").read_text(), re.S).group(1))
    labels = [g["label"] for g in index["groups"]]
    if len(labels) != 2 or not any("Claude Code" in x for x in labels) or not any("OpenCode" in x for x in labels):
        sys.exit(f"expected two ranked groups (OpenCode and manual Claude Code), got {labels}")
    oc = next(g for g in index["groups"] if "OpenCode" in g["label"])
    if oc["n"] != 2 or oc["versions"] != ["2.0.30", "2.0.31"]:
        sys.exit(f"OpenCode group should hold both runs and both versions: {oc}")
    cats = {r["dir"]: r["category"] for r in index["others"]}
    want = {"trial-model-c": "nonstandard", "unscored-model-d": "unscored", "ref-baseline": "reference"}
    if cats != want:
        sys.exit(f"other runs misplaced: {cats}")
    run_page = json.loads(re.search(r'id="data">(.*?)</script>', (report / "runs/oc-model-a-1.html").read_text(), re.S).group(1))
    st = run_page["activity"]["stats"]
    if st["checks"] != 2 or st["checks_passed"] != 1 or st["compile_fail_calls"] != 1 or abs(st["first_pass_min"] - 26.67) > 0.1:
        sys.exit(f"transcript analysis is off: {st}")
    cc_page = json.loads(re.search(r'id="data">(.*?)</script>', (report / "runs/cc-model-b.html").read_text(), re.S).group(1))
    if cc_page["run"]["usage"].get("cost") != 3.5 or cc_page["activity"]["stats"]["checks_passed"] != 1:
        sys.exit(f"Claude Code import is off: {cc_page['run']['usage']} {cc_page['activity']['stats']}")
    if shutil.which("node"):  # the page script at least parses
        run(["node", "-e", "new Function(require('fs').readFileSync(process.argv[1], 'utf8'))",
             TASK / "report/report.js"], "report.js syntax check")
    print(f"report smoke test passed: {report / 'index.html'}")
    if not a.keep:
        shutil.rmtree(work / "season", ignore_errors=True)


if __name__ == "__main__":
    main()
