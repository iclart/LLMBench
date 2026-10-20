#!/usr/bin/env python3
"""ABI 2 experimental preparation/evaluation. Never publishes a season.

Windows jobs cap commit memory and terminate child trees. Windows runs are
local trials, not the Linux isolation/TSan validation required for release.
"""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import statistics
import subprocess
import sys
import threading
import time

HERE = Path(__file__).resolve().parent
TASK = HERE.parent
sys.path.insert(0, str(HERE / "tools"))
sys.path.insert(0, str(TASK / "agent"))
from generate import generate
from model import load_world
from judge import check
import scoring

SCENARIOS = ("daily", "spring", "refund_wave")
FUNCTIONAL_CASES = ("realtime_query", "partial_and_exchange", "reuse_and_rollback", "multileg_rollback", "cross_day_and_fragments", "concurrency")
COMPILE_FLAGS = ["-O2", "-DNDEBUG", "-std=c++20", "-pthread"] + (["-static"] if os.name == "nt" else [])
SUFFIX = ".exe" if os.name == "nt" else ""


def sha(path):
    h = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def task_version():
    digest = hashlib.sha256()
    for path in sorted(p for p in HERE.rglob("*") if p.is_file() and p.suffix in (".py", ".cpp", ".h", ".md") and "__pycache__" not in p.parts):
        digest.update(path.relative_to(HERE).as_posix().encode() + b"\0" + path.read_bytes().replace(b"\r\n", b"\n"))
    digest.update((TASK / "include/ticket_system.h").read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()[:16]


def validate_season(season):
    manifest = json.loads((season / "season.json").read_text())
    if manifest["abi_version"] != 2 or manifest["task_version"] != task_version():
        raise RuntimeError("season task/ABI version differs; prepare a new season")
    for name, expected in manifest["files"].items():
        if sha(season / name) != expected:
            raise RuntimeError("frozen input changed: " + name)
    return manifest


RAILEXEC = None  # Linux: build/tasks/railway/railexec, set by main()


def run_process(command, directory, timeout, memory_bytes):
    from process_control import WindowsJob, peak_working_set
    report_r = report_w = None
    if os.name != "nt":
        # railexec forks the measured program and reports its own peak RSS.
        # Waiting on it directly would count this (large) Python process too:
        # Linux keeps the pre-exec peak of a forked child.
        report_r, report_w = os.pipe()
        command = [RAILEXEC, report_w, *command]
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    if os.name != "nt":
        def limit():
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        options["preexec_fn"] = limit
        options["pass_fds"] = (report_w,)
    start = time.monotonic()
    proc = subprocess.Popen(list(map(str, command)), cwd=directory, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, **options)
    if report_w is not None:
        os.close(report_w)
    job = WindowsJob(memory_bytes) if os.name == "nt" else None
    if job:
        job.attach(proc)
    if os.name != "nt":
        import signal
        timed_out = False
        try:
            _, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(proc.pid, signal.SIGKILL)
            _, stderr = proc.communicate()
        with os.fdopen(report_r) as source:
            report = source.read().split()
        exit_code, maxrss = proc.returncode, None
        if len(report) >= 6 and report[0] == "exit":  # railexec finished: the program's own status
            exit_code = -int(report[3]) if int(report[3]) else int(report[1])
            maxrss = int(report[5]) / 1024
        return {"exit": exit_code, "timed_out": timed_out, "wall_s": time.monotonic() - start,
                "maxrss_mb": maxrss, "stderr": stderr[-4000:].decode(errors="replace"), "sandbox": "posix_resource_trial"}
    stop = threading.Event(); peaks = []
    def monitor():
        while not stop.is_set():
            peaks.append(peak_working_set(proc))
            stop.wait(.02)
    observer = threading.Thread(target=monitor, daemon=True); observer.start()
    timed_out = False
    try:
        _, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        job.terminate()
        _, stderr = proc.communicate()
    finally:
        stop.set(); observer.join()
        peaks.append(peak_working_set(proc))
        job.close()
    return {"exit": proc.returncode, "timed_out": timed_out, "wall_s": time.monotonic() - start,
            "maxrss_mb": max(peaks, default=0) / 2**20,
            "stderr": stderr[-4000:].decode(errors="replace"), "sandbox": "windows_job_trial" if job else "posix_resource_trial"}


def prepare(a):
    a.season.mkdir(parents=True, exist_ok=False)
    world_path = a.season / "world.world"
    if a.public:
        generator = a.build / ("railgen" + SUFFIX)
        subprocess.run([str(generator), "world", str(TASK / "configs/public/world.cfg"), str(a.seed), str(world_path)], check=True)
    elif a.world:
        shutil.copyfile(a.world, world_path)
    else:
        raise RuntimeError("hidden preparation requires --world")
    world = load_world(world_path)
    for scenario in SCENARIOS:
        (a.season / (scenario + ".load")).write_text(generate(world, a.seed + 1, scenario, a.count), newline="\n")
    result = {"abi_version": 2, "schema_version": 2, "task_version": task_version(), "seed": a.seed,
              "visibility": "public" if a.public else "hidden", "count": a.count,
              "platform": platform.platform(), "status": "experimental", "published": False,
              "files": {p.name: sha(p) for p in a.season.iterdir() if p.is_file()}}
    (a.season / "season.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


def evaluate(a):
    manifest = validate_season(a.season)
    a.out.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(TASK / "eval"))
    from compare_reference import pin_four_cores
    cpus = pin_four_cores()
    if a.src:
        executable = a.out / ("submission" + SUFFIX)
        sources = sorted(a.src.glob("*.cpp"))
        if not sources:
            raise RuntimeError("no src/*.cpp files")
        command = [a.cxx, "-O2", "-DNDEBUG", "-std=c++20", "-pthread", "-I" + str(HERE / "include"),
                   "-I" + str(TASK / "include"), *map(str, sources), str(a.build / "v2/librailv2_harness.a"),
                   str(a.build / "librailcommon.a"), "-o", str(executable)]
        if os.name == "nt":
            command.insert(1, "-static")
        compiled = subprocess.run(command, capture_output=True, text=True, timeout=120)
        (a.out / "compile.txt").write_text(compiled.stdout + compiled.stderr)
        if compiled.returncode:
            raise RuntimeError("submission compilation failed")
        checks_exe = a.out / ("checks" + SUFFIX)
        command[command.index(str(a.build / "v2/librailv2_harness.a"))] = str(a.build / "v2/librailv2_checks.a")
        command[-1] = str(checks_exe)
        compiled = subprocess.run(command, capture_output=True, text=True, timeout=120)
        (a.out / "compile-checks.txt").write_text(compiled.stdout + compiled.stderr)
        if compiled.returncode:
            raise RuntimeError("functional harness compilation failed")
    else:
        executable = a.build / "v2" / ("railv2_run_" + a.engine + SUFFIX)
        checks_exe = a.build / "v2" / ("railv2_tests_" + a.engine + SUFFIX)
    world = load_world(a.season / "world.world")
    result = {"schema_version": 2, "abi_version": 2, "task_version": manifest["task_version"],
              "platform": platform.platform(), "binary_sha256": sha(executable), "measurements": {},
              "status": "experimental", "published": False, "total": None}
    result["cpus"] = cpus
    result["compile_flags"] = COMPILE_FLAGS
    result["inputs_sha256"] = sha(a.season / "season.json")
    result["compiler"] = subprocess.run([a.cxx,"--version"],capture_output=True,text=True,check=True).stdout.splitlines()[0]
    calibration = json.loads(a.calibration.read_text()) if a.calibration else None
    if calibration and any(calibration.get(k)!=result[k] for k in ("inputs_sha256","task_version","platform","cpus")):
        raise RuntimeError("harness calibration differs from this season/platform")
    result["calibration_sha256"] = sha(a.calibration) if calibration else None
    functional={"categories":{},"crashes":0}
    for case in FUNCTIONAL_CASES:
        output=a.out/("functional."+case+".json")
        process=run_process([checks_exe,"--case",case,"--json",output],a.out,60,1536*2**20)
        passed=process["exit"]==0 and not process["timed_out"] and output.exists()
        functional["categories"][case]={"passed":int(passed),"total":1,"process":process}
        functional["crashes"]+=int(process["exit"] not in (0,1) or process["timed_out"])
    result["functional"]=functional
    for scenario in SCENARIOS:
        for threads in (1, 4, 16):
            raw = []
            for repeat in range(a.repeats):
                name = f"{scenario}.t{threads}.{repeat}"
                trace = a.out / (name + ".jsonl")
                process = run_process([executable, a.season / "world.world", a.season / (scenario + ".load"), threads, trace],
                                      a.out, a.timeout, int((1536 + (calibration["harness_mb"][f"{scenario}/t{threads}"] if calibration else a.harness_mb)) * 2**20))
                if process["exit"] or process["timed_out"]:
                    verdict = {"ok": False, "errors": ["process failed"], "process": process}
                else:
                    try:
                        with trace.open() as source:
                            header = json.loads(next(source)); events = [json.loads(line) for line in source]
                        verdict = check(world, header, events)
                    except (ValueError,KeyError,TypeError,IndexError,StopIteration) as error:
                        verdict={"ok":False,"errors":[{"kind":"invalid_log","detail":str(error)}]}
                    verdict["process"] = process
                (a.out / (name + ".json")).write_text(json.dumps(verdict, indent=2))
                raw.append(verdict)
                print(name, "ok" if verdict["ok"] else verdict["errors"], flush=True)
                if not verdict["ok"]:
                    break
            key = f"{scenario}/t{threads}"
            if all(r["ok"] for r in raw):
                result["measurements"][key] = {
                    "ok": True, "throughput": statistics.median(r["throughput"] for r in raw),
                    "latency": {op: {"count": min(r["latency"][op]["count"] for r in raw),
                                      "p99_ns": statistics.median(r["latency"][op]["p99_ns"] or 0 for r in raw),
                                      "sample_sufficient": all(r["latency"][op]["sample_sufficient"] for r in raw)} for op in scoring.OPS},
                    "quiet": {k:statistics.mean(r["quiet"][k] for r in raw) for k in raw[0]["quiet"]},
                    "quality": {k:statistics.mean(r["quality"][k] for r in raw) for k in raw[0]["quality"]},
                    "maxrss_mb":max((r["process"]["maxrss_mb"] or 0 for r in raw),default=0) or None,
                    "raw": raw}
                peak=result["measurements"][key]["maxrss_mb"]
                overhead=calibration["harness_mb"][key] if calibration else None
                result["measurements"][key]["engine_mb"]=max(1.,peak-overhead) if peak and overhead is not None else None
            else:
                result["measurements"][key] = {"ok": False, "raw": raw}
            (a.out / "result.json").write_text(json.dumps(result, indent=2))
    result["correctness_passed"] = all(c["passed"]==c["total"] for c in functional["categories"].values()) and all(v["ok"] for v in result["measurements"].values())
    result["sample_sufficient"] = result["correctness_passed"] and all(
        v["latency"][op]["sample_sufficient"] for v in result["measurements"].values() for op in scoring.OPS)
    result["release"] = scoring.release_gate({"correctness": result["correctness_passed"]})
    (a.out / "result.json").write_text(json.dumps(result, indent=2))
    return result


def calibrate(a):
    manifest=validate_season(a.season)
    a.out.mkdir(parents=True,exist_ok=False)
    sys.path.insert(0,str(TASK/"eval"))
    from compare_reference import pin_four_cores
    result={"schema_version":2,"abi_version":2,"task_version":manifest["task_version"],
            "inputs_sha256":sha(a.season/"season.json"),"platform":platform.platform(),
            "cpus":pin_four_cores(),"harness_mb":{}}
    for scenario in SCENARIOS:
        for threads in (1,4,16):
            values=[]
            for repeat in range(a.repeats):
                trace=a.out/f"{scenario}.t{threads}.{repeat}.jsonl"
                process=run_process([a.build/"v2"/("railv2_run_stub"+SUFFIX),a.season/"world.world",a.season/(scenario+".load"),threads,trace],a.out,600,1536*2**20)
                if process["exit"] or process["timed_out"] or not process["maxrss_mb"]:
                    raise RuntimeError("harness memory unavailable: "+str(process))
                values.append(process["maxrss_mb"])
            result["harness_mb"][f"{scenario}/t{threads}"]=statistics.median(values)
    (a.out/"calibration.json").write_text(json.dumps(result,indent=2))
    print(json.dumps(result,indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--season", type=lambda p: Path(p).resolve(), required=True)
    prep.add_argument("--build", type=lambda p: Path(p).resolve(), required=True, help="build/tasks/railway")
    prep.add_argument("--world", type=Path)
    prep.add_argument("--public", action="store_true")
    prep.add_argument("--seed", type=int, default=20261018)
    prep.add_argument("--count", type=int, default=100000)
    ev = sub.add_parser("evaluate")
    for name in ("season", "build", "out"):
        ev.add_argument("--" + name, type=lambda p: Path(p).resolve(), required=True)
    source = ev.add_mutually_exclusive_group(required=True)
    source.add_argument("--src", type=lambda p: Path(p).resolve())
    source.add_argument("--engine", choices=("baseline", "strong", "stub"))
    ev.add_argument("--cxx", default="g++")
    ev.add_argument("--repeats", type=int, default=5)
    ev.add_argument("--timeout", type=float, default=600)
    ev.add_argument("--harness-mb", type=float, default=0, help="measured stub footprint; do not guess for scored runs")
    ev.add_argument("--calibration",type=Path,help="same-season calibrate/calibration.json")
    cal=sub.add_parser("calibrate",help="measure stub harness memory independently of engine anchors")
    for name in ("season","build","out"):
        cal.add_argument("--"+name,type=lambda p:Path(p).resolve(),required=True)
    cal.add_argument("--repeats",type=int,default=3)
    score_parser=sub.add_parser("score",help="compare ABI 2 results without reusing ABI 1 anchors")
    for name in ("measured","baseline","strong","out"):
        score_parser.add_argument("--"+name,type=Path,required=True)
    score_parser.add_argument("--management",type=Path)
    score_parser.add_argument("--checks",type=Path)
    a = parser.parse_args()
    global RAILEXEC
    if getattr(a, "build", None):
        RAILEXEC = a.build / "railexec"
    if a.command == "prepare":
        if a.count < 1:
            parser.error("count must be positive")
        prepare(a)
    elif a.command=="calibrate":
        if a.repeats<1: parser.error("repeats must be positive")
        calibrate(a)
    elif a.command=="score":
        read=lambda p:json.loads(p.read_text()) if p else None
        a.out.write_text(json.dumps(scoring.score(read(a.measured),read(a.baseline),read(a.strong),read(a.management),read(a.checks)),indent=2))
    else:
        if a.repeats < 1 or a.timeout <= 0 or a.harness_mb < 0:
            parser.error("invalid measurement limits")
        evaluate(a)


if __name__ == "__main__":
    main()
