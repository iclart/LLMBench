#!/usr/bin/env python3
"""Builds the workspace an agent receives (DESIGN.md 6).

  package_workspace.py --out DIR [--build BUILD] [--seed N] [--answers-engine strong|baseline] [--selftest]

Run it on the machine that will host the agent, from an LLMBench checkout
with a CMake build tree: the harness ships as static libraries compiled with
the local compiler, so they link against the same libstdc++.

The workspace contains only whitelisted files: TASK.md, the interface header,
the stub in src/, the Makefile, prebuilt harness libraries and tools, the
public configs and the public data with reference answers. No harness or
reference source is included; the script fails if any source slips in.
"""
import argparse
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

TASK = Path(__file__).resolve().parents[1]
REPO = TASK.parents[1]
CXX = "g++"
CXXFLAGS = ["-O2", "-std=c++20", "-pthread"]
SCENARIOS = ["daily", "spring", "refund_wave"]
COMMON = sorted((TASK / "tools/common").glob("*.cpp"))
PUBLIC_SEED = 20261017


def run(argv, **kw):
    p = subprocess.run([str(a) for a in argv], capture_output=True, text=True, **kw)
    if p.returncode != 0:
        sys.exit(f"failed: {' '.join(map(str, argv))}\n{p.stderr[-3000:]}")
    return p


def static_lib(out, main_source, objdir):
    objs = []
    for i, src in enumerate([main_source] + COMMON):
        o = objdir / f"{out.stem}_{i}.o"
        run([CXX] + CXXFLAGS + [f"-I{TASK / 'include'}", f"-I{TASK / 'tools/common'}", "-c", src, "-o", o])
        objs.append(o)
    run(["ar", "rcs", out] + objs)
    run(["strip", "--strip-debug", out])


def check_whitelist(ws):
    """No source code other than the interface header and the stub."""
    allowed = {Path("include/ticket_system.h"), Path("src/solution.cpp")}
    leaked = [p.relative_to(ws) for p in ws.rglob("*")
              if p.suffix in (".cpp", ".cc", ".h", ".hpp", ".py", ".inc") and p.relative_to(ws) not in allowed]
    if leaked:
        sys.exit(f"refusing to package source files: {leaked}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="output directory; also writes <out>.tar.gz")
    ap.add_argument("--build", default=str(REPO / "build"))
    ap.add_argument("--seed", type=int, default=PUBLIC_SEED)
    ap.add_argument("--answers-engine", default="strong", choices=["strong", "baseline"])
    ap.add_argument("--selftest", action="store_true",
                    help="build the workspace with the stub, then pass `make check` with the baseline")
    a = ap.parse_args()

    tools = Path(a.build).resolve() / "tasks/railway"
    ref = tools / f"railrun_{a.answers_engine}"
    for t in (tools / "railgen", tools / "railjudge", ref):
        if not t.exists():
            sys.exit(f"{t} not found; build the project first")

    ws = Path(a.out).resolve()
    if ws.exists():
        shutil.rmtree(ws)
    for d in ("include", "src", "lib", "bin", "data", "configs"):
        (ws / d).mkdir(parents=True)

    shutil.copy(TASK / "TASK.md", ws)
    shutil.copy(TASK / "include/ticket_system.h", ws / "include")
    shutil.copy(TASK / "stub/solution.cpp", ws / "src")
    shutil.copy(TASK / "agent/workspace/Makefile", ws)
    for cfg in (TASK / "configs/public").glob("*.cfg"):
        shutil.copy(cfg, ws / "configs")
    for t in ("railjudge", "railgen"):
        shutil.copy(tools / t, ws / "bin")
        run(["strip", ws / "bin" / t])

    with tempfile.TemporaryDirectory() as tmp:
        static_lib(ws / "lib/librailbench.a", TASK / "tools/bench/main.cpp", Path(tmp))
        static_lib(ws / "lib/librailrun.a", TASK / "tools/run/main.cpp", Path(tmp))

    world = ws / "data/world.world"
    run([tools / "railgen", "world", TASK / "configs/public/world.cfg", a.seed, world])
    for i, sc in enumerate(SCENARIOS):
        load = ws / f"data/{sc}.load"
        run([tools / "railgen", "load", world, TASK / f"configs/public/{sc}.cfg", a.seed + 1 + i, load])
        run([ref, "query", world, load, ws / f"data/{sc}.answers"])
    check_whitelist(ws)

    if a.selftest:
        with tempfile.TemporaryDirectory() as tmp:
            trial = Path(tmp) / "ws"
            shutil.copytree(ws, trial)
            run(["make", "-C", trial])
            (trial / "src/solution.cpp").unlink()
            shutil.copy(TASK / "reference/baseline/baseline.cpp", trial / "src")
            p = subprocess.run(["make", "-C", trial, "check"], capture_output=True, text=True)
            if p.returncode != 0:
                sys.exit(f"selftest: `make check` with the baseline failed\n{p.stdout[-3000:]}\n{p.stderr[-3000:]}")
            print("selftest passed: stub builds, baseline passes make check", file=sys.stderr)

    archive = ws.with_suffix(".tar.gz")
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(ws, arcname="workspace")
    print(f"workspace: {ws}\narchive:   {archive}", file=sys.stderr)


if __name__ == "__main__":
    main()
