#!/usr/bin/env python3
"""Smoke test for eval/railway_eval.py on a tiny season.

Prepares a season, then checks that the baseline submission evaluates
cleanly, that the do-nothing stub is rejected scenario by scenario, and that a
submission shadowing ticket_system.h does not compile.
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

TASK = Path(__file__).resolve().parents[1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--strong", action="store_true", help="the strong reference is available")
    a = ap.parse_args()
    work = Path(a.work)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    ev = [sys.executable, str(TASK / "eval/railway_eval.py"), "--build", a.build, "--sandbox", "none"]
    engine = "strong" if a.strong else "baseline"

    def run(*args, expect_ok=True):
        p = subprocess.run(ev + list(args), capture_output=True, text=True)
        if expect_ok and p.returncode != 0:
            sys.exit(f"railway_eval {' '.join(args[:1])} failed ({p.returncode}):\n{p.stderr[-4000:]}")
        return p

    season = work / "season"
    run("prepare", "--season", str(season), "--configs", str(TASK / "configs/public"), "--seed", "5",
        "--set", "intents=2000", "--set", "quiet_intents=200", "--anchor-repeats", "1",
        "--answers-engine", engine, "--strong-engine", engine)

    subs = work / "subs"
    (subs / "baseline").mkdir(parents=True)
    shutil.copy(TASK / "reference/baseline/baseline.cpp", subs / "baseline")
    (subs / "stub").mkdir()
    shutil.copy(TASK / "stub/solution.cpp", subs / "stub")
    (subs / "shadow").mkdir()
    shutil.copy(TASK / "stub/solution.cpp", subs / "shadow")
    shutil.copy(TASK / "include/ticket_system.h", subs / "shadow")

    run("evaluate", "--season", str(season), "--src", str(subs / "baseline"), "--out", str(work / "baseline.json"),
        "--lite")
    r = json.loads((work / "baseline.json").read_text())
    if r["fatal"] or any(s.get("failure") for s in r["scenarios"].values()):
        sys.exit(f"baseline submission should pass: {r['fatal']} {r['scenarios']}")

    run("evaluate", "--season", str(season), "--src", str(subs / "stub"), "--out", str(work / "stub.json"), "--lite")
    r = json.loads((work / "stub.json").read_text())
    failed = [sc for sc, s in r["scenarios"].items() if s.get("failure")]
    if len(failed) != 3 or r["scores"]["total"] != 0:
        sys.exit(f"stub should fail every scenario: {r['scenarios']}")

    run("evaluate", "--season", str(season), "--src", str(subs / "shadow"), "--out", str(work / "shadow.json"),
        "--lite", expect_ok=False)
    r = json.loads((work / "shadow.json").read_text())
    if r["fatal"] != "compile_error":
        sys.exit(f"a submission with its own ticket_system.h must be rejected, got {r['fatal']}")
    print("railway_eval smoke test passed")


if __name__ == "__main__":
    main()
