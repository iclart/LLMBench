#!/usr/bin/env python3
"""Runs railbench then railjudge and checks the verdict.

--expect pass               the run must be judged correct
--expect fail:KIND[,KIND]   the judge must reject it with errors of every KIND
--expect bench-fail         railbench itself must fail (e.g. a TSan report)
"""
import argparse
import json
import os
import subprocess
import sys


def main():
    ap = argparse.ArgumentParser()
    for flag in ("bench", "judge", "world", "load", "answers", "threads", "log", "expect"):
        ap.add_argument("--" + flag, required=True)
    a = ap.parse_args()

    env = dict(os.environ, TSAN_OPTIONS="halt_on_error=1 exitcode=66 " + os.environ.get("TSAN_OPTIONS", ""))
    bench = subprocess.run([a.bench, a.world, a.load, a.threads, a.log], env=env, capture_output=True, text=True)
    if a.expect == "bench-fail":
        if bench.returncode == 0:
            sys.exit("expected railbench to fail, but it succeeded")
        print(f"railbench failed as expected (exit {bench.returncode})")
        return
    if bench.returncode != 0:
        sys.exit(f"railbench failed (exit {bench.returncode}):\n{bench.stderr[-4000:]}")

    out = a.log + ".json"
    judge = subprocess.run([a.judge, a.world, a.load, a.answers, a.log, "--json", out],
                           capture_output=True, text=True)
    if judge.returncode not in (0, 1):
        sys.exit(f"railjudge error (exit {judge.returncode}):\n{judge.stderr}")
    with open(out) as f:
        verdict = json.load(f)
    print(judge.stderr.strip())

    if a.expect == "pass":
        if not verdict["ok"]:
            sys.exit("expected a pass")
    elif a.expect.startswith("fail:"):
        if verdict["ok"]:
            sys.exit("expected the judge to reject this run, but it passed")
        missing = [k for k in a.expect[5:].split(",") if k not in verdict["error_counts"]]
        if missing:
            sys.exit(f"expected error kinds {missing}, got {verdict['error_counts']}")
    else:
        sys.exit("unknown --expect " + a.expect)


if __name__ == "__main__":
    main()
