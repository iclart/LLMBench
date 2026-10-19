#!/usr/bin/env python3
"""Whitelist-only ABI 2 public workspace; never copies private references."""
import argparse
import json
import os
from pathlib import Path
import py_compile
import shutil
import subprocess
import sys
from evaluate import HERE, TASK, sha, validate_season


def package(season, build, out, tsan_build=None):
    manifest = validate_season(season)
    if manifest["visibility"] != "public":
        raise RuntimeError("only a generated public season may be packaged")
    out.mkdir(parents=True, exist_ok=False)
    for directory in ("include", "src", "bin", "lib", "data"):
        (out / directory).mkdir()
    copies = {"TASK.md": HERE / "TASK.md", "PROMPT.md": HERE / "PROMPT.md", "Makefile": HERE / "workspace/Makefile",
              "include/ticket_system.h": TASK / "include/ticket_system.h", "include/ticket_system_v2.h": HERE / "include/ticket_system_v2.h",
              "src/solution.cpp": HERE / "stub/solution.cpp", "bin/workspace.py": HERE / "workspace/workspace.py",
              "lib/librailv2_harness.a": build / "v2/librailv2_harness.a", "lib/librailv2_checks.a": build / "v2/librailv2_checks.a",
              "lib/librailcommon.a": build / "librailcommon.a"}
    for name in ("world.world", "daily.load", "spring.load", "refund_wave.load"):
        copies["data/" + name] = season / name
    if tsan_build:
        if os.name=="nt":raise RuntimeError("TSan packages require a Linux toolchain")
        root=next((p for p in (tsan_build,*tsan_build.parents) if (p/"CMakeCache.txt").exists()),None)
        if root is None or "RAIL_TSAN:BOOL=ON" not in (root/"CMakeCache.txt").read_text():
            raise RuntimeError("not a TSan build")
        (out/"lib/tsan").mkdir()
        for name in ("librailv2_harness.a","librailv2_checks.a"):
            copies["lib/tsan/"+name]=tsan_build/"v2"/name
        copies["lib/tsan/librailcommon.a"]=tsan_build/"librailcommon.a"
    for name, source in copies.items():
        shutil.copyfile(source, out / name)
    for name in ("model", "audit", "judge"):
        py_compile.compile(str(HERE / "tools" / (name + ".py")), cfile=str(out / "bin" / (name + ".pyc")),
                           dfile=name + ".py", doraise=True)
    allowed = set(copies) | {"bin/" + name + ".pyc" for name in ("model", "audit", "judge")}
    found = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()}
    if found != allowed:
        raise RuntimeError("package whitelist mismatch")
    metadata = {"abi_version": 2, "task_version": manifest["task_version"], "public_seed": manifest["seed"],
                "python_version": list(sys.version_info[:2]), "status": "experimental",
                "sanitizers":["tsan"] if tsan_build else [],
                "files": {name: sha(out / name) for name in sorted(allowed)}}
    (out / ".task_version").write_text(json.dumps(metadata, indent=2))
    return metadata


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("season", "build", "out"):
        p.add_argument("--" + name, type=lambda value: Path(value).resolve(), required=True)
    p.add_argument("--selftest", action="store_true")
    p.add_argument("--tsan-build",type=lambda p:Path(p).resolve())
    a = p.parse_args(); package(a.season, a.build, a.out,a.tsan_build)
    if a.selftest:
        subprocess.run([sys.executable, "bin/workspace.py", "build"], cwd=a.out, check=True)
    print(a.out)


if __name__ == "__main__":
    main()
