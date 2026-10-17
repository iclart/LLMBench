#!/usr/bin/env python3
"""Evaluation orchestration for the LLMBench railway task (docs/DESIGN.md).

  railway_eval.py prepare   --season DIR --configs DIR --seed N [--set key=value]...
                            [--anchor-repeats N] [--baseline-repeats N]
  railway_eval.py evaluate  --season DIR --src DIR --out RESULT.json [--lite]
  railway_eval.py snapshots --season DIR --snapshots DIR --out DIR

A season directory holds everything one evaluation round needs: generated
world and workloads, reference query answers, the harness's own memory
footprint, and the baseline/strong anchors. `evaluate` scores one submission
(a directory of .cpp/.h files implementing include/ticket_system.h) against
it. Common options: --build DIR (CMake build tree with the tools),
--sandbox bwrap|none, --cpus LIST.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
import time
from pathlib import Path

TASK = Path(__file__).resolve().parents[1]
REPO = TASK.parents[1]
sys.path.insert(0, str(TASK))
sys.path.insert(0, str(TASK / "eval"))
import scoring  # noqa: E402
import taskinfo  # noqa: E402
from scoring import SCENARIOS, measurement_score  # noqa: E402

THREADS = [1, 4, 16]
LITE_THREADS = [4]
MEM_BUDGET_MB = 1536          # contestant budget (TASK.md 8)
MEM_MARGIN_MB = 64            # on top of the harness's measured footprint
MEM_HARD_LIMIT_MB = 8192      # address-space cap that only protects the machine
INIT_LIMIT_S = 30
MAX_REPEATS = 5
REPEAT_CUTOFF_S = 120         # no further repeats after a run this long
TIMEOUT_FACTOR, TIMEOUT_SLACK_S = 3.0, 30  # slower than the baseline still scores (negative)
TSAN_INTENTS = 4000
TSAN_EXIT = 66

# Official contestant compile flags (TASK.md 1).
CXX = os.environ.get("CXX", "g++")
CXXFLAGS = ["-O2", "-std=c++20", "-pthread"]
COMMON_SOURCES = sorted((TASK / "tools/common").glob("*.cpp"))
BENCH_MAIN, RUN_MAIN = TASK / "tools/bench/main.cpp", TASK / "tools/run/main.cpp"
FUNCTIONAL_TIMEOUT_S = 300   # one railrun func process
SUITE_PER_CATEGORY = 20

# Static scan: constructs a ticketing engine has no business using. A hit
# does not fail the submission; it marks it for manual review (DESIGN.md 5).
SUSPICIOUS = [
    (r"\b(asm|__asm__|__asm)\b", "inline assembly"),
    (r"\bsyscall\s*\(", "raw system call"),
    (r"\b(dlopen|dlsym|dladdr)\b", "dynamic symbol lookup"),
    (r"/proc/|/sys/|/dev/", "reads system files"),
    (r"\b(mprotect|mmap|process_vm_readv|ptrace)\b", "memory mapping or inspection"),
    (r"\b(fopen|freopen|open|creat|ifstream|ofstream|fstream|filesystem)\b", "file I/O"),
    (r"\b(socket|connect|getaddrinfo)\b", "network access"),
    (r"\b(fork|vfork|exec[lv]p?e?|system|popen|posix_spawn)\b", "spawns processes"),
    (r"\b(getenv|environ)\b", "reads the environment"),
    (r"\b(railtools|BenchLog|SeatBlock|Chunked|OrderPool|Player)\b", "refers to harness internals"),
    (r"__builtin_frame_address|__builtin_return_address|backtrace", "stack inspection"),
]


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def run_checked(argv, **kw):
    p = subprocess.run([str(a) for a in argv], capture_output=True, text=True, **kw)
    if p.returncode != 0:
        raise RuntimeError(f"command failed ({p.returncode}): {' '.join(map(str, argv))}\n{p.stderr[-3000:]}")
    return p


class Tools:
    def __init__(self, build):
        self.dir = Path(build).resolve() / "tasks/railway"
        for name in ("railgen", "railjudge", "railexec"):
            if not (self.dir / name).exists():
                raise SystemExit(f"{self.dir / name} not found; build the project first")

    def __getattr__(self, name):
        return self.dir / name

    def engine(self, kind, engine):
        path = self.dir / f"{kind}_{engine}"
        if not path.exists():
            raise SystemExit(f"{path} not found (strong reference needs the private submodule)")
        return path


# ------------------------------------------------------------------ running

class Run:
    """Outcome of one sandboxed process."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def run_sandboxed(argv, *, cpus, sandbox, ro_paths, rw_path, timeout, tools, env=None, proc=False, cap_memory=True):
    """Runs argv under railexec (inside bwrap if enabled), pinned to `cpus`.

    The sandbox has no network, no /proc (unless `proc`, which the TSan
    runtime needs), a private /tmp, and read-only access to `ro_paths`.
    `cap_memory` limits the address space to protect the machine; TSan
    needs it off because it reserves terabytes of shadow address space.

    Returns Run(exit, signal, maxrss_mb, wall_s, timed_out, stderr)."""
    r, w = os.pipe()
    inner = [str(tools.railexec), str(w)] + [str(a) for a in argv]
    if sandbox == "bwrap":
        cmd = ["bwrap", "--unshare-all", "--die-with-parent", "--new-session", "--dev", "/dev", "--tmpfs", "/tmp"]
        for top in ("usr", "lib", "lib64", "lib32", "bin", "sbin"):
            p = Path("/") / top
            if p.is_symlink():
                cmd += ["--symlink", os.readlink(p), str(p)]
            elif p.exists():
                cmd += ["--ro-bind", str(p), str(p)]
        for path in sorted({str(Path(x).resolve()) for x in ro_paths}):
            cmd += ["--ro-bind", path, path]
        if proc:
            cmd += ["--proc", "/proc"]
        cmd += ["--bind", str(rw_path), str(rw_path), "--chdir", str(rw_path)]
        cmd += inner
    elif sandbox == "none":
        cmd = inner
    else:
        raise ValueError(sandbox)

    def preexec():
        os.sched_setaffinity(0, cpus)
        if cap_memory:
            import resource
            limit = MEM_HARD_LIMIT_MB << 20
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))

    start = time.monotonic()
    proc = subprocess.Popen(cmd, pass_fds=(w,), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            text=True, preexec_fn=preexec, start_new_session=True, env=env, cwd=rw_path)
    os.close(w)
    timed_out = threading.Event()

    def kill():
        timed_out.set()
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    timer = threading.Timer(timeout, kill) if timeout else None
    if timer:
        timer.start()
    report_chunks = []
    reader = threading.Thread(target=lambda: report_chunks.append(os.read(r, 4096)), daemon=True)
    reader.start()
    stderr = proc.communicate()[1]
    if timer:
        timer.cancel()
    reader.join(timeout=5)
    os.close(r)
    wall = time.monotonic() - start

    report = b"".join(report_chunks).decode().split()
    fields = dict(zip(report[0::2], report[1::2]))
    if "exit" in fields:
        code, sig, rss = int(fields["exit"]), int(fields["signal"]), int(fields["maxrss_kb"]) / 1024
    else:  # railexec itself was killed (timeout) or failed to start
        code, sig, rss = -1, signal.SIGKILL if timed_out.is_set() else 0, 0.0
    return Run(exit=code, signal=sig, maxrss_mb=rss, wall_s=wall, timed_out=timed_out.is_set(), stderr=stderr)


def parse_cpus(text):
    cpus = set()
    for part in text.split(","):
        a, _, b = part.partition("-")
        cpus.update(range(int(a), int(b or a) + 1))
    return cpus


# ------------------------------------------------------------------ prepare

def season_paths(season):
    s = Path(season).resolve()
    return {
        "dir": s,
        "world": s / "world.world",
        "load": {sc: s / f"{sc}.load" for sc in SCENARIOS},
        "answers": {sc: s / f"{sc}.answers" for sc in SCENARIOS},
        "tsan_load": {sc: s / f"tsan_{sc}.load" for sc in SCENARIOS},
        "tsan_answers": {sc: s / f"tsan_{sc}.answers" for sc in SCENARIOS},
        "anchors": s / "anchors.json",
        "suite": s / "functional.suite",
        "meta": s / "season.json",
    }


def bench_once(tools, bench, sp, scenario, threads, args, *, timeout, workdir, load=None, answers=None):
    """Runs railbench + railjudge once. Returns (Run, verdict or None)."""
    load = load or sp["load"][scenario]
    answers = answers or sp["answers"][scenario]
    logf = Path(workdir) / f"{scenario}_t{threads}.log"
    run = run_sandboxed([bench, sp["world"], load, threads, logf], cpus=parse_cpus(args.cpus),
                        sandbox=args.sandbox, ro_paths=[sp["dir"], bench, tools.railexec],
                        rw_path=workdir, timeout=timeout, tools=tools)
    verdict = None
    if run.exit == 0 and logf.exists():
        j = subprocess.run([tools.railjudge, sp["world"], load, answers, logf, "--json", str(logf) + ".json"],
                           capture_output=True, text=True)
        if j.returncode in (0, 1):
            verdict = json.loads(Path(str(logf) + ".json").read_text())
        else:
            raise RuntimeError(f"railjudge failed: {j.stderr}")
    if logf.exists():
        logf.unlink()
    return run, verdict


SCALARS = ("throughput", "query_p99_ns", "book_p99_ns", "main_s", "init_s", "wall_s", "maxrss_mb")


def metrics_of(verdict, run):
    lat = verdict["latency"]
    return {
        "throughput": verdict["throughput"],
        "query_p99_ns": lat["query"]["p99_ns"],
        "book_p99_ns": lat["book"]["p99_ns"],
        "main_s": verdict["main_s"],
        "init_s": verdict["init_s"],
        "wall_s": run.wall_s,
        "maxrss_mb": run.maxrss_mb,
        "quiet": verdict["quiet"],
        "quality": verdict["quality"],
        # For the report: latency percentiles, histograms and the throughput timeline.
        "dist": {"latency": lat, "histogram": verdict.get("histogram"), "timeline": verdict.get("timeline")},
    }


def median_metrics(runs):
    out = {}
    for key in SCALARS:
        out[key] = statistics.median(r[key] for r in runs)
    out["quiet"] = runs[0]["quiet"]  # the 1-thread run is deterministic
    out["quality"] = runs[0]["quality"]
    out["runs"] = len(runs)
    out["repeats"] = [{key: r[key] for key in SCALARS} for r in runs]
    # Distributions of the repeat whose throughput is the (lower) median.
    order = sorted(range(len(runs)), key=lambda i: runs[i]["throughput"])
    out["dist"] = runs[order[(len(runs) - 1) // 2]]["dist"]
    return out


def season_fingerprint(sp):
    return hashlib.sha256(sp["anchors"].read_bytes()).hexdigest()[:16]


def cmd_prepare(args):
    tools = Tools(args.build)
    sp = season_paths(args.season)
    sp["dir"].mkdir(parents=True, exist_ok=True)
    configs = Path(args.configs).resolve()
    sets = sum((["--set", s] for s in args.set), [])
    world_sets = sum((["--set", s] for s in args.world_set), [])

    # Nothing before the anchors is timed, so it runs in parallel.
    def parallel(commands):
        with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as pool:
            for f in [pool.submit(run_checked, c) for c in commands]:
                f.result()

    log(f"generating world and workloads (seed {args.seed})")
    run_checked([tools.railgen, "world", configs / "world.cfg", args.seed, sp["world"]] + world_sets)
    gen = []
    for i, sc in enumerate(SCENARIOS):
        gen.append([tools.railgen, "load", sp["world"], configs / f"{sc}.cfg", args.seed + 1 + i, sp["load"][sc]] + sets)
        gen.append([tools.railgen, "load", sp["world"], configs / f"{sc}.cfg", args.seed + 11 + i, sp["tsan_load"][sc],
                    "--set", f"intents={TSAN_INTENTS}", "--set", "quiet_intents=500"])
    parallel(gen)

    log("reference answers")
    answer_engine = tools.engine("railrun", args.answers_engine)
    parallel([[answer_engine, "query", sp["world"], sp[load][sc], sp[ans][sc]]
              for sc in SCENARIOS for load, ans in (("load", "answers"), ("tsan_load", "tsan_answers"))])

    log("functional suite")
    run_checked([sys.executable, TASK / "eval/make_suite.py", "--world", sp["world"],
                 "--reference", answer_engine, "--seed", args.seed + 21, "--out", sp["suite"],
                 "--per-category", SUITE_PER_CATEGORY])

    anchors = {"harness_mb": {}, "baseline": {}, "strong": {}}
    with tempfile.TemporaryDirectory(dir=sp["dir"]) as work:
        stub = tools.engine("railbench", "stub")
        for sc in SCENARIOS:
            run, _ = bench_once(tools, stub, sp, sc, max(THREADS), args, timeout=None, workdir=work)
            anchors["harness_mb"][sc] = run.maxrss_mb
            log(f"harness footprint {sc}: {run.maxrss_mb:.0f} MB")
        # The baseline serializes every call, so it varies little between runs and
        # a run takes minutes; the strong reference takes seconds.
        for engine, impl, repeats in (("baseline", "baseline", args.baseline_repeats),
                                      ("strong", args.strong_engine, args.anchor_repeats)):
            bench = tools.engine("railbench", impl)
            for sc in SCENARIOS:
                for t in THREADS:
                    runs = []
                    for _ in range(repeats):
                        run, verdict = bench_once(tools, bench, sp, sc, t, args, timeout=None, workdir=work)
                        if run.exit != 0 or not verdict or not verdict["ok"]:
                            raise SystemExit(f"anchor {engine} {sc} t={t} failed: exit {run.exit}, "
                                             f"{verdict and verdict['errors'][:3]}\n{run.stderr[-2000:]}")
                        runs.append(metrics_of(verdict, run))
                    m = median_metrics(runs)
                    anchors[engine][f"{sc}/t{t}"] = m
                    log(f"anchor {engine:8} {sc:11} t={t:<2} {m['throughput']:>10.0f} calls/s  "
                        f"query p99 {m['query_p99_ns'] / 1e3:8.1f} us  book p99 {m['book_p99_ns'] / 1e3:8.1f} us  "
                        f"wall {m['wall_s']:.1f} s")
    for key, what, span in scoring.narrow_spans(anchors):
        log(f"WARNING: {key} {what}: the strong reference is only {span:.1f}x the baseline; scored as if "
            f"{scoring.MIN_SPAN:.0f}x (is this machine too small for the season?)")
    sp["anchors"].write_text(json.dumps(anchors, indent=2))
    sp["meta"].write_text(json.dumps({
        "name": sp["dir"].name, "fingerprint": season_fingerprint(sp),
        "task_version": taskinfo.task_version(), "prompt_sha": taskinfo.prompt_sha(),
        "machine": taskinfo.machine_info(),
        "seed": args.seed, "configs": str(configs), "set": args.set, "world_set": args.world_set,
        "answers_engine": args.answers_engine, "anchor_repeats": args.anchor_repeats,
        "baseline_repeats": args.baseline_repeats,
        "strong_engine": args.strong_engine,
        "sandbox": args.sandbox, "cpus": args.cpus, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
    }, indent=2))
    log(f"season ready: {sp['dir']}")


# ------------------------------------------------------------------ evaluate

def scan_sources(src):
    flags = []
    for f in sorted(Path(src).rglob("*")):
        if f.suffix not in (".cpp", ".h", ".hpp", ".cc", ".cxx", ".hh", ".inc"):
            continue
        text = f.read_text(errors="replace")
        # Ignore comments and string literals' contents only loosely: we flag, humans decide.
        for lineno, line in enumerate(text.splitlines(), 1):
            code = line.split("//", 1)[0]
            for pattern, why in SUSPICIOUS:
                if re.search(pattern, code):
                    flags.append({"file": str(f.relative_to(src)), "line": lineno, "reason": why,
                                  "text": line.strip()[:160]})
    return flags


def compile_submission(src, out, workdir, tsan=False, main=BENCH_MAIN):
    """Compiles the submission with the official flags plus the harness. Returns (ok, log)."""
    sources = sorted(p for p in Path(src).rglob("*.cpp"))
    if not sources:
        return False, "no .cpp files in the submission", 0.0
    # A local copy of the interface header would silently replace the official one.
    shadowing = [p.name for p in Path(src).rglob("*") if p.name == "ticket_system.h"]
    if shadowing:
        return False, "the submission must not contain its own ticket_system.h", 0.0
    flags = CXXFLAGS + (["-fsanitize=thread", "-g"] if tsan else [])
    objdir = Path(workdir) / (("obj_tsan" if tsan else "obj") + ("" if main == BENCH_MAIN else "_run"))
    objdir.mkdir(exist_ok=True)
    objs = []
    for i, s in enumerate([main] + COMMON_SOURCES):
        o = objdir / f"harness_{i}.o"
        run_checked([CXX] + flags + [f"-I{TASK / 'include'}", f"-I{TASK / 'tools/common'}", "-c", s, "-o", o])
        objs.append(o)
    cmd = [CXX] + flags + [f"-I{TASK / 'include'}", f"-I{src}"] + sources + objs + ["-o", out]
    start = time.monotonic()
    p = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    compile_s = time.monotonic() - start
    return p.returncode == 0, p.stderr[-6000:], compile_s


def evaluate(args, src, out_path, lite):
    tools = Tools(args.build)
    sp = season_paths(args.season)
    anchors = json.loads(sp["anchors"].read_text())
    if any("quality" not in m for m in anchors["strong"].values()):
        raise SystemExit(f"season {sp['dir']} was prepared by an older version without booking quality; "
                         "prepare it again")
    src = Path(src).resolve()
    threads = LITE_THREADS if lite else THREADS
    max_repeats = 1 if lite else MAX_REPEATS
    meta = json.loads(sp["meta"].read_text())
    result = {"src": str(src), "mode": "lite" if lite else "full", "flags": scan_sources(src),
              "season": {"name": meta.get("name", sp["dir"].name), "fingerprint": season_fingerprint(sp)},
              "evaluated": time.strftime("%Y-%m-%d %H:%M:%S"), "machine": taskinfo.machine_info(),
              "sandbox": args.sandbox, "measurements": {}, "scenarios": {}, "fatal": None}

    with tempfile.TemporaryDirectory(dir=sp["dir"]) as work:
        bench = Path(work) / "railbench_submission"
        ok, clog, compile_s = compile_submission(src, bench, work)
        result["build"] = {"compile_s": round(compile_s, 2),
                           "binary_bytes": bench.stat().st_size if ok else None}
        if not ok:
            result["fatal"] = "compile_error"
            result["compile_log"] = clog
            return finish(result, anchors, out_path, threads)

        rrun = Path(work) / "railrun_submission"
        ok, clog, _ = compile_submission(src, rrun, work, main=RUN_MAIN)
        if not ok:
            result["fatal"] = "compile_error"
            result["compile_log"] = clog
            return finish(result, anchors, out_path, threads)
        result["functional"] = functional(tools, rrun, sp, args, work)

        if not lite:
            tbench = Path(work) / "railbench_submission_tsan"
            ok, clog, _ = compile_submission(src, tbench, work, tsan=True)
            if not ok:
                result["fatal"] = "compile_error"
                result["compile_log"] = clog
                return finish(result, anchors, out_path, threads)
            for sc in SCENARIOS:
                log(f"TSan check: {sc}")
                env = dict(os.environ, TSAN_OPTIONS=f"halt_on_error=1 exitcode={TSAN_EXIT}")
                run = run_sandboxed([tbench, sp["world"], sp["tsan_load"][sc], 4, Path(work) / "tsan.log"],
                                    cpus=parse_cpus(args.cpus), sandbox=args.sandbox,
                                    ro_paths=[sp["dir"], tbench, tools.railexec], rw_path=work,
                                    timeout=600, tools=tools, env=env, proc=True, cap_memory=False)
                if "WARNING: ThreadSanitizer:" in run.stderr:
                    result["fatal"] = "data_race"
                elif run.exit != 0:
                    result["fatal"] = "crash_under_tsan"
                if result["fatal"]:
                    result["tsan_report"] = run.stderr[-6000:]
                    return finish(result, anchors, out_path, threads)

        for sc in SCENARIOS:
            failure = None
            for t in threads:
                key = f"{sc}/t{t}"
                base, strong = anchors["baseline"][key], anchors["strong"][key]
                timeout = TIMEOUT_FACTOR * base["wall_s"] + TIMEOUT_SLACK_S
                mem_limit = MEM_BUDGET_MB + anchors["harness_mb"][sc] + MEM_MARGIN_MB
                runs, status = [], "ok"
                while len(runs) < max_repeats:
                    log(f"{key} run {len(runs) + 1}")
                    run, verdict = bench_once(tools, bench, sp, sc, t, args, timeout=timeout, workdir=work)
                    if run.timed_out:
                        status = "timeout"
                        break
                    if run.exit != 0:
                        failure = f"crash (exit {run.exit}, signal {run.signal}) at t={t}"
                        result.setdefault("stderr", {})[key] = run.stderr[-3000:]
                        break
                    if run.maxrss_mb > mem_limit:
                        failure = f"memory {run.maxrss_mb:.0f} MB over the {mem_limit:.0f} MB limit at t={t}"
                        break
                    if not verdict["ok"]:
                        failure = f"wrong results at t={t}"
                        result["scenarios"][sc] = {"errors": verdict["errors"], "error_counts": verdict["error_counts"]}
                        break
                    if verdict["init_s"] > INIT_LIMIT_S:
                        result["fatal"] = "init_too_slow"
                        return finish(result, anchors, out_path, threads)
                    runs.append(metrics_of(verdict, run))
                    score, _, _ = measurement_score(runs[-1], base, strong)
                    if score <= 0 or run.wall_s > REPEAT_CUTOFF_S:
                        break
                if failure:
                    break
                entry = {"status": status, "timeout_s": round(timeout, 1), "mem_limit_mb": round(mem_limit)}
                if runs and status == "ok":
                    entry.update(median_metrics(runs))
                result["measurements"][key] = entry
            result["scenarios"].setdefault(sc, {})["failure"] = failure
    return finish(result, anchors, out_path, threads)


def functional(tools, rrun, sp, args, work):
    """Runs the season's functional suite, resuming after crashes (scoring.run_functional)."""
    if not sp["suite"].exists():
        raise SystemExit(f"season {sp['dir']} has no functional suite; prepare it again")
    log("functional suite")
    trace = Path(work) / "functional.trace"

    def launch(start):
        run = run_sandboxed([rrun, "func", sp["world"], sp["suite"], "--from", start, "--trace", trace],
                            cpus=parse_cpus(args.cpus), sandbox=args.sandbox, ro_paths=[sp["dir"], rrun, tools.railexec],
                            rw_path=work, timeout=FUNCTIONAL_TIMEOUT_S, tools=tools)
        if run.timed_out:
            return "timeout"
        if run.exit in (0, 1):
            return None
        return f"signal {run.signal}" if run.signal else f"exit {run.exit}"

    res = scoring.run_functional(sp["suite"], trace, launch)
    log(f"functional {scoring.functional_score(res):.1f}" + (f", {res['crashes']} crash(es)" if res["crashes"] else ""))
    return res


def finish(result, anchors, out_path, threads):
    """Computes the scores (scoring.py) and writes the result."""
    failed = [sc for sc in SCENARIOS if result["scenarios"].get(sc, {}).get("failure") is not None]
    result["scores"] = scoring.score(result["measurements"], anchors, threads, failed, result["fatal"] is not None,
                                     result.get("functional"))
    result["needs_review"] = bool(result["flags"])
    Path(out_path).write_text(json.dumps(result, indent=2))
    log(summary(result))
    return result


def summary(result):
    s = result["scores"]
    lines = [f"total {s['total']:.1f}  (performance {s['performance']:.1f}, functional {s['functional']:.1f}, "
             f"transport {s['transport']:.1f}, preference {s['preference']:.1f}, memory {s['memory']:.1f})  "
             f"mode={result['mode']}"]
    for cat, c in (result.get("functional") or {}).get("categories", {}).items():
        if c["passed"] < c["total"]:
            lines.append(f"  functional {cat}: {c['passed']}/{c['total']}, e.g. {c['failures'][0].splitlines()[0]}")
    if result["fatal"]:
        lines.append(f"FATAL: {result['fatal']}")
    for sc in SCENARIOS:
        f = result["scenarios"].get(sc, {}).get("failure")
        g = s["gates"].get(sc)
        if f:
            lines.append(f"{sc}: FAILED, {f}")
        elif g and not g["passed"]:
            lines.append(f"{sc}: scores 0, satisfaction {100 * g['satisfaction']:.1f}% is "
                         f"{100 * g['ratio']:.1f}% of the strong reference's (at least "
                         f"{100 * scoring.SATISFACTION_GATE:.0f}% needed)")
    for key, v in s["measurements"].items():
        m = result["measurements"].get(key, {})
        lines.append(f"  {key:16} {v['score']:6.1f}  (throughput {v['throughput_score']:5.1f}, latency "
                     f"{v['latency_score']:5.1f}) {m.get('status', '-')}")
    for sc in SCENARIOS:
        g = s["gates"].get(sc)
        if g:
            mm = s["memory_mb"][sc]
            if mm["engine"] is not None and mm["strong"]:
                lines.append(f"  {sc:16} memory {s['memory_by_scenario'][sc]:5.1f} ({mm['engine']:.0f} MB, strong "
                             f"reference {mm['strong']:.0f} MB)")
            lines.append(f"  {sc:16} transport {s['transport_by_scenario'][sc]:5.1f} (passenger-minutes "
                         f"{100 * s['transport_ratio'][sc]:5.1f}%, satisfaction {100 * g['ratio']:5.1f}% of the "
                         f"strong reference's)")
    if result["flags"]:
        lines.append(f"NEEDS REVIEW: {len(result['flags'])} suspicious construct(s), e.g. "
                     f"{result['flags'][0]['file']}:{result['flags'][0]['line']} ({result['flags'][0]['reason']})")
    return "\n".join(lines)


def cmd_evaluate(args):
    res = evaluate(args, args.src, args.out, args.lite)
    sys.exit(0 if res["fatal"] is None else 1)


def cmd_snapshots(args):
    """Evaluates every snapshot directory (sorted by name); the last is the final version."""
    snaps = sorted(p for p in Path(args.snapshots).iterdir() if p.is_dir())
    if not snaps:
        raise SystemExit("no snapshot directories found")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    curve = []
    for i, snap in enumerate(snaps):
        final = i == len(snaps) - 1
        src = snap / "src" if (snap / "src").is_dir() else snap
        log(f"== snapshot {snap.name} ({'full' if final else 'lite'})")
        res = evaluate(args, src, out / f"{snap.name}.json", lite=not final)
        curve.append({"snapshot": snap.name, "mode": res["mode"], "total": res["scores"]["total"],
                      "fatal": res["fatal"], "needs_review": res["needs_review"]})
    (out / "curve.json").write_text(json.dumps(curve, indent=2))
    for c in curve:
        log(f"{c['snapshot']:>12} {c['mode']:4} {c['total']:6.1f} {c['fatal'] or ''}")
    log(f"report: python3 {TASK / 'report/report.py'} --season {args.season} --runs {out.parent.parent} "
        f"--out <report dir>")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build", default=str(REPO / "build"))
    ap.add_argument("--sandbox", choices=["bwrap", "none"], default="bwrap" if shutil.which("bwrap") else "none")
    ap.add_argument("--cpus", default="0-3", help="CPUs the measured process may use, e.g. 0-3")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("prepare")
    p.add_argument("--season", required=True)
    p.add_argument("--configs", required=True, help="directory with world.cfg and <scenario>.cfg")
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--set", action="append", default=[], help="workload config override, key=value")
    p.add_argument("--world-set", action="append", default=[], help="world config override, key=value")
    p.add_argument("--anchor-repeats", type=int, default=3, help="runs per strong anchor (median)")
    p.add_argument("--baseline-repeats", type=int, default=1, help="runs per baseline anchor (median)")
    p.add_argument("--answers-engine", default="strong", choices=["strong", "baseline"])
    p.add_argument("--strong-engine", default="strong",
                   help="engine for the 100-point anchor; only smoke tests without the private submodule "
                        "should use anything but strong")
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("evaluate")
    p.add_argument("--season", required=True)
    p.add_argument("--src", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--lite", action="store_true", help="4 threads, one run, no TSan (snapshots)")
    p.set_defaults(func=cmd_evaluate)

    p = sub.add_parser("snapshots")
    p.add_argument("--season", required=True)
    p.add_argument("--snapshots", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_snapshots)

    args = ap.parse_args()
    if args.cmd != "prepare":
        sp = season_paths(args.season)
        if not sp["meta"].exists():
            raise SystemExit(f"season {sp['dir']} is not prepared (no season.json); run "
                             f"`railway_eval.py prepare --season {args.season} ...` first, or check the path")
    if args.sandbox == "none":
        log("warning: running without a sandbox (results are not official)")
    args.func(args)


if __name__ == "__main__":
    main()
