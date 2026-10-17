#!/usr/bin/env python3
"""Estimates your score on the public workloads (`make score`, TASK.md 9).

Runs build/railbench on every public scenario at the thread counts in
data/anchors.json, judges each run with bin/railjudge, and scores the runs
with the official formula (bin/scoring.py) against the baseline and strong
reference measured on this machine when the workspace was packaged.

It is an estimate: the official evaluation uses hidden workloads of the
same kind, 1, 4 and 16 threads, the median of repeated runs, a sandbox,
a memory limit and a TSan check (`make tsan`).
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

WS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WS / "bin"))
import scoring  # noqa: E402

TIMEOUT_FACTOR, TIMEOUT_SLACK_S = 1.2, 30


def run_bench(sc, t, timeout):
    """Runs and judges one measurement. Returns (metrics or None, message)."""
    log = WS / f"build/score_{sc}_t{t}.log"
    verdict_path = WS / f"build/score_{sc}_t{t}.json"
    argv = [WS / "build/railbench", WS / "data/world.world", WS / f"data/{sc}.load", str(t), log]
    start = time.monotonic()
    proc = subprocess.Popen([str(a) for a in argv], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        _, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return None, f"timed out after {timeout:.0f} s"
    wall = time.monotonic() - start
    if proc.returncode != 0:
        return None, f"railbench failed (exit {proc.returncode}): {err.strip()[-300:]}"
    j = subprocess.run([str(WS / "bin/railjudge"), str(WS / "data/world.world"), str(WS / f"data/{sc}.load"),
                        str(WS / f"data/{sc}.answers"), str(log), "--json", str(verdict_path)],
                       capture_output=True, text=True)
    log.unlink(missing_ok=True)
    if j.returncode not in (0, 1):
        return None, f"railjudge failed: {j.stderr.strip()[-300:]}"
    v = json.loads(verdict_path.read_text())
    if not v["ok"]:
        return None, f"wrong results: {v['errors'][:2]} (see {verdict_path.relative_to(WS)})"
    lat = v["latency"]
    return {"status": "ok", "throughput": v["throughput"], "query_p99_ns": lat["query"]["p99_ns"],
            "book_p99_ns": lat["book"]["p99_ns"], "quiet": v["quiet"], "quality": v["quality"],
            "wall_s": wall}, ""


def main():
    anchors_path = WS / "data/anchors.json"
    if not anchors_path.exists():
        sys.exit("make score: no data/anchors.json in this workspace (it was packaged without reference "
                 "measurements); use `make check` and `make bench` instead")
    anchors = json.loads(anchors_path.read_text())
    threads = anchors["threads"]
    measurements, failed = {}, []
    for sc in scoring.SCENARIOS:
        for t in threads:
            key = f"{sc}/t{t}"
            timeout = TIMEOUT_FACTOR * anchors["baseline"][key]["wall_s"] + TIMEOUT_SLACK_S
            print(f"== {key}: running (timeout {timeout:.0f} s)", flush=True)
            m, why = run_bench(sc, t, timeout)
            if m is None:
                print(f"   FAILED: {why}")
                failed.append(sc)
                break
            measurements[key] = m
            b, s = anchors["baseline"][key], anchors["strong"][key]
            print(f"   {m['throughput']:10.0f} calls/s (baseline {b['throughput']:.0f}, strong {s['throughput']:.0f}); "
                  f"p99 query {m['query_p99_ns'] / 1e3:.1f} us, book {m['book_p99_ns'] / 1e3:.1f} us; "
                  f"{m['wall_s']:.1f} s", flush=True)
    res = scoring.score(measurements, anchors, threads, failed)

    print("\nestimated score on the public workloads (official: hidden workloads, 1/4/16 threads, medians)")
    for key, v in res["measurements"].items():
        print(f"  {key:16} {v['score']:6.1f}  (throughput {v['throughput_score']:5.1f}, "
              f"latency {v['latency_score']:5.1f})")
    for sc in scoring.SCENARIOS:
        g = res["gates"].get(sc)
        if sc in failed:
            print(f"  {sc:16} FAILED: scores 0")
        elif g is None:
            print(f"  {sc:16} not measured")
        else:
            q = measurements[f"{sc}/t{res['quality_threads']}"]["quality"]
            state = "ok" if g["passed"] else f"BELOW {100 * scoring.SATISFACTION_GATE:.0f}%: the scenario scores 0"
            print(f"  {sc:16} satisfaction {100 * g['satisfaction']:5.1f}% = {100 * g['ratio']:5.1f}% of the "
                  f"strong reference's ({state}); transport {res['transport_by_scenario'][sc]:5.1f} "
                  f"({q['passenger_minutes']} passenger-minutes, {100 * res['transport_ratio'][sc]:.1f}% of the "
                  f"strong reference's); preference "
                  f"{res['preference_by_scenario'][sc]:5.1f}")
    print(f"\n  performance {res['performance']:6.1f} x {scoring.PERF_WEIGHT:.2f}")
    print(f"  transport   {res['transport']:6.1f} x {scoring.TRANSPORT_WEIGHT:.2f}")
    print(f"  preference  {res['preference']:6.1f} x {scoring.PREF_WEIGHT:.2f}")
    print(f"  estimated total {res['total']:.1f}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
