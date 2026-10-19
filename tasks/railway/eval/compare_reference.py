#!/usr/bin/env python3
"""Freeze and compare private v1 candidates. Never modifies season anchors.

Run after building: --build build-win/tasks/railway --configs PRIVATE/hidden
--out REPORT_DIR. One invocation owns the machine during timed measurements.
"""
import argparse
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import statistics
import subprocess
import sys

SCENARIOS = ("daily", "spring", "refund_wave")


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for data in iter(lambda: f.read(1024 * 1024), b""):
            h.update(data)
    return h.hexdigest()


def pin_four_cores():
    if os.name != "nt":
        allowed = sorted(os.sched_getaffinity(0))
        # Keep one logical CPU per physical core where sysfs is available.
        cores, selected = set(), []
        for cpu in allowed:
            root = Path(f"/sys/devices/system/cpu/cpu{cpu}/topology")
            key = tuple((root / n).read_text().strip() for n in ("physical_package_id", "core_id"))
            if key not in cores:
                selected.append(cpu)
                cores.add(key)
        if len(selected) < 4:
            raise RuntimeError("four physical cores are required")
        os.sched_setaffinity(0, selected[:4])
        return selected[:4]
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.GetCurrentProcess.restype = ctypes.c_void_p
    k.SetProcessAffinityMask.argtypes = (ctypes.c_void_p, ctypes.c_size_t)
    size = ctypes.c_ulong()
    k.GetLogicalProcessorInformationEx(0, None, ctypes.byref(size))
    buffer = ctypes.create_string_buffer(size.value)
    if not k.GetLogicalProcessorInformationEx(0, buffer, ctypes.byref(size)):
        raise ctypes.WinError(ctypes.get_last_error())
    raw, offset, cores = buffer.raw, 0, []
    while offset < size.value:
        record_size = int.from_bytes(raw[offset + 4:offset + 8], "little")
        if record_size < 32:
            raise RuntimeError("invalid Windows processor topology")
        groups = int.from_bytes(raw[offset + 30:offset + 32], "little")
        for i in range(groups):
            base = offset + 32 + 16 * i
            mask = int.from_bytes(raw[base:base + 8], "little")
            group = int.from_bytes(raw[base + 8:base + 10], "little")
            if group == 0 and mask:
                cores.append((raw[offset + 9], mask & -mask))
        offset += record_size
    cores.sort(reverse=True)  # Prefer the higher performance class on hybrid CPUs.
    if len(cores) < 4:
        raise RuntimeError("four physical cores in processor group 0 are required")
    mask = sum(c[1] for c in cores[:4])
    if not k.SetProcessAffinityMask(k.GetCurrentProcess(), mask):
        raise ctypes.WinError(ctypes.get_last_error())
    return [i for i in range(64) if mask >> i & 1]


def gate(rows):
    ratios = []
    failures = []
    for key, pair in rows.items():
        old, new = pair["strong"], pair["candidate"]
        t = new["throughput"] / old["throughput"]
        q = new["query_p99_ns"] / old["query_p99_ns"]
        b = new["book_p99_ns"] / old["book_p99_ns"]
        ratios.append((t, b))
        if t < .95 or q > 1.05:
            failures.append(key + ": per-group regression")
    gm = lambda xs: math.exp(statistics.mean(math.log(x) for x in xs))
    throughput = gm([r[0] for r in ratios])
    book = gm([r[1] for r in ratios])
    if throughput < 1.2:
        failures.append("throughput improvement below 20%")
    if book > .7:
        failures.append("book p99 reduction below 30%")
    return {"passed": not failures, "throughput_ratio": throughput,
            "book_p99_ratio": book, "failures": failures}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--build", type=Path, required=True)
    ap.add_argument("--configs", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--seed", type=int, default=20261018)
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--scale", type=float, default=1., help="smaller values are exploratory only")
    ap.add_argument("--frozen-inputs", type=Path, help="reuse verified data from an earlier freeze")
    a = ap.parse_args()
    if a.repeats < 3 or not 0 < a.scale <= 1:
        ap.error("at least three repeats and 0 < scale <= 1 required")
    a.out.mkdir(parents=True, exist_ok=False)
    cpus = pin_four_cores()
    suffix = ".exe" if os.name == "nt" else ""
    tools = {n: (a.build / (n + suffix)).resolve() for n in
             ("railgen", "railjudge", "railrun_strong", "railrun_candidate", "railbench_strong", "railbench_candidate")}
    manifest = {"schema_version": 1, "platform": platform.platform(), "cpus": cpus,
                "seed": a.seed, "scale": a.scale, "repeats": a.repeats,
                "binaries": {n: digest(p) for n, p in tools.items()}, "files": {}}
    root=next((p for p in (a.build,*a.build.parents) if (p/"CMakeCache.txt").exists()),None)
    if root:
        cache=(root/"CMakeCache.txt").read_text()
        keys=("CMAKE_CXX_COMPILER","CMAKE_CXX_FLAGS","CMAKE_CXX_FLAGS_RELEASE","CMAKE_BUILD_TYPE","RAIL_STRONG_CANDIDATE")
        manifest["build"]={key:match[1] for key in keys if (match:=re.search(r"(?m)^"+key+r":[^=]*=(.*)$",cache))}
        compiler=manifest["build"].get("CMAKE_CXX_COMPILER")
        if compiler:
            manifest["compiler"]=subprocess.run([compiler,"--version"],capture_output=True,text=True,check=True).stdout.splitlines()[0]
    def run(name, *args, stdout=None):
        return subprocess.run([str(tools[name]), *map(str, args)], check=True,
                              stdout=stdout or subprocess.PIPE, stderr=subprocess.PIPE)
    world = a.out / "frozen.world"
    previous = None
    if a.frozen_inputs:
        previous = json.loads((a.frozen_inputs / "frozen.json").read_text())
        if previous["seed"] != a.seed or previous["scale"] != a.scale:
            raise RuntimeError("frozen input seed/scale mismatch")
        manifest["input_manifest_sha256"] = digest(a.frozen_inputs / "frozen.json")
        for name in ["frozen.world"] + [sc + ext for sc in SCENARIOS for ext in (".cfg", ".load", ".answers", ".strong.tickets")]:
            original = a.frozen_inputs / name
            if digest(original) != previous["files"][name]:
                raise RuntimeError("frozen input changed: " + name)
            os.link(original, a.out / name)
    else:
        run("railgen", "world", a.configs / "world.cfg", a.seed, world)
    for sc in SCENARIOS:
        cfg = a.out / (sc + ".cfg")
        text = (a.configs / (sc + ".cfg")).read_text()
        text = re.sub(r"(?m)^(intents\s*=\s*)(\d+)", lambda m: m[1] + str(max(1000, int(int(m[2]) * a.scale))), text)
        if not previous:
            cfg.write_text(text)
        load, answers = a.out / (sc + ".load"), a.out / (sc + ".answers")
        if not previous:
            run("railgen", "load", world, cfg, a.seed + 1, load)
            run("railrun_strong", "query", world, load, answers)
        run("railrun_candidate", "query", world, load, a.out / (sc + ".candidate.answers"), "--expect", answers)
        # Full deterministic ticket replay, excluding only timing summaries.
        hashes = []
        for engine in ("strong", "candidate"):
            ticket_file = a.out / f"{sc}.{engine}.tickets"
            if not (previous and engine == "strong"):
                with ticket_file.open("wb") as f:
                    run("railrun_" + engine, "tickets", world, load, "--limit", 18446744073709551615, stdout=f)
            h = hashlib.sha256()
            with ticket_file.open("rb") as f:
                for line in f:
                    if b" s in engine" not in line:
                        h.update(line)
            hashes.append(h.hexdigest())
        if hashes[0] != hashes[1]:
            raise RuntimeError(sc + ": deterministic ticket/quality mismatch")
        print(sc + ": queries, tickets and quality match", flush=True)
    manifest["files"] = {p.name: digest(p) for p in a.out.iterdir() if p.is_file()}
    (a.out / "frozen.json").write_text(json.dumps(manifest, indent=2))
    rows = {}
    for sc in SCENARIOS:
        for threads in (1, 4, 16):
            key = f"{sc}/t{threads}"
            raw = {"strong": [], "candidate": []}
            for repeat in range(a.repeats):
                for engine in (("strong", "candidate") if repeat % 2 == 0 else ("candidate", "strong")):
                    name = f"{sc}.t{threads}.{engine}.{repeat}"
                    log, result = a.out / (name + ".log"), a.out / (name + ".json")
                    run("railbench_" + engine, world, a.out / (sc + ".load"), threads, log)
                    run("railjudge", world, a.out / (sc + ".load"), a.out / (sc + ".answers"), log, "--json", result)
                    v = json.loads(result.read_text())
                    if not v["ok"]:
                        raise RuntimeError(name + ": judge failed")
                    raw[engine].append({"throughput": v["throughput"],
                                        "query_p99_ns": v["latency"]["query"]["p99_ns"],
                                        "book_p99_ns": v["latency"]["book"]["p99_ns"]})
                    print(name, raw[engine][-1], flush=True)
            rows[key] = {e: {m: statistics.median(v[m] for v in values) for m in values[0]}
                         for e, values in raw.items()}
            rows[key]["raw"] = raw
            (a.out / "progress.json").write_text(json.dumps(rows, indent=2))
    verdict = gate(rows)
    verdict.update(schema_version=1, platform=platform.platform(), exploratory=a.scale != 1, anchors_replaced=False)
    (a.out / "comparison.json").write_text(json.dumps({"gate": verdict, "measurements": rows}, indent=2))
    print(json.dumps(verdict, indent=2))


if __name__ == "__main__":
    main()
