"""Portable contestant commands; trusted engine/judge sources are not shipped."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
SUFFIX = ".exe" if os.name == "nt" else ""


def build(sanitize=False):
    sources = sorted((ROOT / "src").glob("*.cpp"))
    if not sources:
        raise RuntimeError("no src/*.cpp files")
    if sanitize and (os.name=="nt" or not (ROOT/"lib/tsan/librailv2_harness.a").exists()):
        raise RuntimeError("TSan requires the Linux sanitizer libraries; this package cannot claim that check passed")
    destination = ROOT / ("build-tsan" if sanitize else "build"); destination.mkdir(exist_ok=True)
    cxx = os.environ.get("CXX", "g++")
    content = (cxx+str(sanitize)).encode() + b"".join(p.name.encode() + p.read_bytes() for p in sorted((ROOT / "src").iterdir()) if p.suffix in (".cpp", ".h", ".hpp"))
    stamp = hashlib.sha256(content).hexdigest()
    binaries = [destination / (name + SUFFIX) for name in ("railv2", "checks")]
    marker = destination / ".sources"
    if marker.exists() and marker.read_text() == stamp and all(p.exists() for p in binaries):
        return binaries
    for binary, library in zip(binaries, ("librailv2_harness.a", "librailv2_checks.a")):
        prefix="lib/tsan/" if sanitize else "lib/"
        command = [cxx, "-O2", "-DNDEBUG", "-std=c++20", "-pthread", "-Iinclude", *map(str, sources), prefix + library]
        if library == "librailv2_harness.a":
            command.append(prefix+"librailcommon.a")
        if sanitize:command.extend(("-fsanitize=thread","-g"))
        if os.name == "nt":
            command.append("-static")
        subprocess.run([*command, "-o", str(binary)], cwd=ROOT, check=True)
    marker.write_text(stamp)
    return binaries


def bench(scenario, threads, sanitize=False):
    engine, _ = build(sanitize)
    trace = ROOT / ("build-tsan" if sanitize else "build") / f"{scenario}.t{threads}.jsonl"
    result = trace.with_suffix(".json")
    environment=os.environ.copy()
    if sanitize:environment["TSAN_OPTIONS"]="halt_on_error=1"
    subprocess.run([str(engine), "data/world.world", f"data/{scenario}.load", str(threads), str(trace)], cwd=ROOT, env=environment, check=True)
    subprocess.run([sys.executable, "bin/judge.pyc", "data/world.world", str(trace), "--out", str(result)], cwd=ROOT, check=True)
    return json.loads(result.read_text())


def main():
    metadata = json.loads((ROOT / ".task_version").read_text())
    if list(sys.version_info[:2]) != metadata["python_version"]:
        raise RuntimeError("compiled tools require Python " + ".".join(map(str, metadata["python_version"])))
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("build", "check", "bench", "score", "time", "submit", "tsan"))
    parser.add_argument("--scenario", choices=("daily", "spring", "refund_wave"), default="daily")
    parser.add_argument("--threads", type=int, default=4)
    a = parser.parse_args()
    if a.command == "build":
        build()
    elif a.command == "check":
        _, checks = build()
        subprocess.run([str(checks), "--json", str(ROOT / "build/functional.json")], cwd=ROOT, check=True)
        for scenario in ("daily", "spring", "refund_wave"):
            bench(scenario, a.threads)
    elif a.command == "bench":
        print(json.dumps(bench(a.scenario, a.threads), indent=2))
    elif a.command == "score":
        values = {s: bench(s, a.threads) for s in ("daily", "spring", "refund_wave")}
        print(json.dumps({"abi_version": 2, "total": None, "status": "experimental",
                          "reason": "No validated management/pilot/platform gate: no formal total", "measurements": values}, indent=2))
    elif a.command == "time":
        deadline = ROOT / ".deadline"
        print(f"{max(0,int(float(deadline.read_text())-time.time()))} seconds left" if deadline.exists() else "no deadline set")
    elif a.command == "submit":
        (ROOT / ".submit").write_text(str(time.time()) + "\n")
        print("submitted")
    else:
        _, checks=build(True)
        subprocess.run([str(checks)],cwd=ROOT,env={**os.environ,"TSAN_OPTIONS":"halt_on_error=1"},check=True)
        for scenario in ("daily","spring","refund_wave"):bench(scenario,a.threads,True)


if __name__ == "__main__":
    main()
