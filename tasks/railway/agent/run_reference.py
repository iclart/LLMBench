#!/usr/bin/env python3
"""Dry run of the whole pipeline with a reference engine instead of an LLM.

  run_reference.py --workspace DIR|TAR.GZ --out RUN_DIR --engine baseline|strong [--skip-check]

Produces a run directory with the same layout as run_opencode.py: the
packaged workspace with the reference's source in src/, snapshots/000 (the
untouched stub) and snapshots/001_final (the reference), and run.json. Unless
--skip-check, it also runs `make check` in the workspace, which proves the
packaged toolchain works on this machine. Then score it like any run:

  railway_eval.py snapshots --season S --snapshots RUN_DIR/snapshots --out RUN_DIR/results

Expected: about 0 performance points for the baseline, about 100 for strong.
"""
import argparse
import json
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

TASK = Path(__file__).resolve().parents[1]
REPO = TASK.parents[1]
ENGINES = {
    "baseline": TASK / "reference/baseline/baseline.cpp",
    "strong": REPO / "private/railway/strong/strong.cpp",
}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--engine", required=True, choices=sorted(ENGINES))
    ap.add_argument("--skip-check", action="store_true", help="do not run `make check` (it takes a few minutes)")
    a = ap.parse_args()

    source = ENGINES[a.engine]
    if not source.exists():
        sys.exit(f"{source} not found (the strong reference needs the private submodule)")
    out = Path(a.out).resolve()
    if out.exists() and any(out.iterdir()):
        sys.exit(f"{out} is not empty")
    out.mkdir(parents=True, exist_ok=True)
    ws = out / "workspace"
    if Path(a.workspace).is_dir():
        shutil.copytree(a.workspace, ws)
    else:
        with tarfile.open(a.workspace) as tar:
            tar.extractall(out, filter="data")

    start = time.time()
    shutil.copytree(ws / "src", out / "snapshots/000/src")
    for f in (ws / "src").iterdir():
        f.unlink()
    shutil.copy(source, ws / "src")

    check = None
    if not a.skip_check:
        print(f"running `make check` with the {a.engine} reference...", file=sys.stderr)
        p = subprocess.run(["make", "-C", str(ws), "check"], capture_output=True, text=True)
        (out / "make_check.log").write_text(p.stdout + p.stderr)
        check = p.returncode == 0
        if not check:
            sys.exit(f"`make check` failed; see {out / 'make_check.log'}")
    shutil.copytree(ws / "src", out / "snapshots/001_final/src")

    run = {"tool": "reference", "engine": a.engine, "start": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start)),
           "elapsed_min": round((time.time() - start) / 60, 2), "end_reason": "submitted", "submitted": True,
           "make_check_passed": check, "snapshots": ["000", "001_final"]}
    (out / "run.json").write_text(json.dumps(run, indent=2))
    print(f"done: {out}\nscore it with: python3 {TASK / 'eval/railway_eval.py'} snapshots --season <season> "
          f"--snapshots {out / 'snapshots'} --out {out / 'results'}", file=sys.stderr)


if __name__ == "__main__":
    main()
