#!/usr/bin/env python3
"""Builds the workspace an agent receives (DESIGN.md 6).

  package_workspace.py --out DIR [--build BUILD] [--seed N] [--answers-engine strong|baseline]
                       [--anchors measure|none] [--intents N] [--selftest]

Run it on the machine that will host the agent, from an LLMBench checkout
with a CMake build tree: the harness ships as static libraries compiled with
the local compiler, so they link against the same libstdc++.

The workspace contains only whitelisted files: TASK.md, the interface header,
the stub in src/, the Makefile, prebuilt harness libraries (plain and TSan)
and tools, the scoring scripts behind `make score`, the public configs and
the public data with reference answers. No harness or reference source is
included; the script fails if any source slips in.

`make score` needs reference measurements: packaging runs the baseline and
the strong reference on the public workloads at 1 and 4 threads on this
machine (about 10 minutes, mostly the baseline) and ships only the numbers,
in data/anchors.json. --anchors none skips that; --intents shrinks the public
workloads (tests only).
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

TASK = Path(__file__).resolve().parents[1]
REPO = TASK.parents[1]
sys.path.insert(0, str(TASK))
import taskinfo  # noqa: E402
CXX = "g++"
CXXFLAGS = ["-O2", "-std=c++20", "-pthread"]
SCENARIOS = ["daily", "spring", "refund_wave"]
COMMON = sorted((TASK / "tools/common").glob("*.cpp"))
PUBLIC_SEED = 20261017
SCORE_THREADS = [1, 4]
TSAN_INTENTS, TSAN_QUIET = 4000, 500  # as railway_eval.py's TSan check
# Scripts that ship on purpose: the scoring formula is public (TASK.md 9).
SCRIPTS = {Path("bin/railscore.py"): TASK / "agent/workspace/railscore.py",
           Path("bin/scoring.py"): TASK / "eval/scoring.py"}
SUITE_PER_CATEGORY = 10


def run(argv, **kw):
    p = subprocess.run([str(a) for a in argv], capture_output=True, text=True, **kw)
    if p.returncode != 0:
        sys.exit(f"failed: {' '.join(map(str, argv))}\n{p.stderr[-3000:]}")
    return p


def static_lib(out, main_source, objdir, extra_flags=()):
    objs = []
    for i, src in enumerate([main_source] + COMMON):
        o = objdir / f"{out.stem}_{i}.o"
        run([CXX] + CXXFLAGS + list(extra_flags) +
            [f"-I{TASK / 'include'}", f"-I{TASK / 'tools/common'}", "-c", src, "-o", o])
        objs.append(o)
    run(["ar", "rcs", out] + objs)
    run(["strip", "--strip-debug", out])


def check_whitelist(ws):
    """No source code other than the interface header and the stub."""
    allowed = {Path("include/ticket_system.h"), Path("src/solution.cpp")} | set(SCRIPTS)
    leaked = [p.relative_to(ws) for p in ws.rglob("*")
              if p.suffix in (".cpp", ".cc", ".h", ".hpp", ".py", ".inc") and p.relative_to(ws) not in allowed]
    if leaked:
        sys.exit(f"refusing to package source files: {leaked}")


def run_rss(argv):
    """Runs argv; returns its peak resident memory in MB."""
    p = subprocess.Popen([str(a) for a in argv], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    _, status, usage = os.wait4(p.pid, 0)
    err = p.stderr.read().decode(errors="replace")
    p.stderr.close()
    if status != 0:
        sys.exit(f"failed: {' '.join(map(str, argv))}\n{err[-3000:]}")
    return usage.ru_maxrss / 1024


def timed(fn, *args):
    start = time.monotonic()
    out = fn(*args)
    return time.monotonic() - start, out


def measure_anchors(ws, tools):
    """Baseline and strong reference on the public workloads, for `make score`."""
    anchors = {"threads": SCORE_THREADS, "machine": taskinfo.machine_info(), "baseline": {}, "strong": {},
               "harness_mb": {}}
    world = ws / "data/world.world"
    with tempfile.TemporaryDirectory() as tmp:
        for sc in SCENARIOS:
            _, rss = timed(run_rss, [tools / "railbench_stub", world, ws / f"data/{sc}.load", max(SCORE_THREADS),
                                     Path(tmp) / "stub.log"])
            anchors["harness_mb"][sc] = round(rss, 1)
        for engine in ("baseline", "strong"):
            for sc in SCENARIOS:
                for t in SCORE_THREADS:
                    log, out = Path(tmp) / "a.log", Path(tmp) / "a.json"
                    wall, rss = timed(run_rss, [tools / f"railbench_{engine}", world, ws / f"data/{sc}.load", t, log])
                    p = subprocess.run([str(tools / "railjudge"), str(world), str(ws / f"data/{sc}.load"),
                                        str(ws / f"data/{sc}.answers"), str(log), "--json", str(out)],
                                       capture_output=True, text=True)
                    v = json.loads(out.read_text()) if out.exists() else None
                    if p.returncode != 0 or not v or not v["ok"]:
                        sys.exit(f"anchor {engine} {sc} t={t} failed: {p.stderr[-2000:]}")
                    lat = v["latency"]
                    anchors[engine][f"{sc}/t{t}"] = {
                        "throughput": v["throughput"], "query_p99_ns": lat["query"]["p99_ns"],
                        "book_p99_ns": lat["book"]["p99_ns"], "quiet": v["quiet"], "quality": v["quality"],
                        "wall_s": round(wall, 2), "maxrss_mb": round(rss, 1)}
                    print(f"anchor {engine:8} {sc:11} t={t} {v['throughput']:>10.0f} calls/s  {wall:6.1f} s",
                          file=sys.stderr)
    (ws / "data/anchors.json").write_text(json.dumps(anchors, indent=1))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="output directory; also writes <out>.tar.gz")
    ap.add_argument("--build", default=str(REPO / "build"))
    ap.add_argument("--seed", type=int, default=PUBLIC_SEED)
    ap.add_argument("--answers-engine", default="strong", choices=["strong", "baseline"])
    ap.add_argument("--anchors", default="measure", choices=["measure", "none"],
                    help="measure the references for `make score` (needs the strong reference)")
    ap.add_argument("--intents", type=int, help="intents per public workload (tests only)")
    ap.add_argument("--selftest", action="store_true",
                    help="build the workspace with the stub, then pass `make check`, `make tsan` and "
                         "(with anchors) `make score` with the baseline")
    a = ap.parse_args()

    tools = Path(a.build).resolve() / "tasks/railway"
    ref = tools / f"railrun_{a.answers_engine}"
    needed = [tools / "railgen", tools / "railjudge", ref]
    if a.anchors == "measure":
        needed += [tools / "railbench_baseline", tools / "railbench_strong", tools / "railbench_stub"]
    for t in needed:
        if not t.exists():
            sys.exit(f"{t} not found; build the project first"
                     + (" (or pass --anchors none)" if "railbench" in t.name else ""))

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
    for dst, src in SCRIPTS.items():
        shutil.copy(src, ws / dst)

    with tempfile.TemporaryDirectory() as tmp:
        static_lib(ws / "lib/librailbench.a", TASK / "tools/bench/main.cpp", Path(tmp))
        static_lib(ws / "lib/librailrun.a", TASK / "tools/run/main.cpp", Path(tmp))
        static_lib(ws / "lib/librailbench_tsan.a", TASK / "tools/bench/main.cpp", Path(tmp),
                   ["-fsanitize=thread", "-g"])

    world = ws / "data/world.world"
    run([tools / "railgen", "world", TASK / "configs/public/world.cfg", a.seed, world])
    sizes = ["--set", f"intents={a.intents}"] if a.intents else []
    for i, sc in enumerate(SCENARIOS):
        cfg = TASK / f"configs/public/{sc}.cfg"
        for load, seed, extra in ((ws / f"data/{sc}.load", a.seed + 1 + i, sizes),
                                  (ws / f"data/tsan_{sc}.load", a.seed + 11 + i,
                                   ["--set", f"intents={TSAN_INTENTS}", "--set", f"quiet_intents={TSAN_QUIET}"])):
            run([tools / "railgen", "load", world, cfg, seed, load] + extra)
            run([ref, "query", world, load, load.with_suffix(".answers")])
    run([sys.executable, TASK / "eval/make_suite.py", "--world", world, "--reference", ref, "--seed", a.seed + 21,
         "--out", ws / "data/functional.suite", "--per-category", SUITE_PER_CATEGORY])
    if a.anchors == "measure":
        measure_anchors(ws, tools)
    (ws / taskinfo.VERSION_FILE).write_text(json.dumps({"task_version": taskinfo.task_version(),
                                                       "public_seed": a.seed}) + "\n")
    check_whitelist(ws)

    if a.selftest:
        with tempfile.TemporaryDirectory() as tmp:
            trial = Path(tmp) / "ws"
            shutil.copytree(ws, trial)
            run(["make", "-C", trial])
            (trial / "src/solution.cpp").unlink()
            shutil.copy(TASK / "reference/baseline/baseline.cpp", trial / "src")
            shutil.rmtree(trial / "build")  # build the baseline from scratch
            targets = ["check", "tsan"] + (["score"] if a.anchors == "measure" else [])
            for target in targets:
                p = subprocess.run(["make", "-C", trial, target], capture_output=True, text=True)
                if p.returncode != 0:
                    sys.exit(f"selftest: `make {target}` with the baseline failed\n{p.stdout[-3000:]}\n"
                             f"{p.stderr[-3000:]}")
                if target == "score" and "estimated total" not in p.stdout:
                    sys.exit(f"selftest: `make score` printed no estimate\n{p.stdout[-3000:]}")
            print(f"selftest passed: stub builds, baseline passes make {', '.join(targets)}", file=sys.stderr)

    archive = ws.with_suffix(".tar.gz")
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(ws, arcname="workspace")
    print(f"workspace: {ws}\narchive:   {archive}", file=sys.stderr)


if __name__ == "__main__":
    main()
