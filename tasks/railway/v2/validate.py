#!/usr/bin/env python3
"""Record real CTest/sanitizer evidence and compare platform generator hashes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
from evaluate import TASK, HERE, task_version
from tools.model import load_world
from tools.generate import generate


def digest(path):
    return hashlib.sha256(path.read_bytes().replace(b"\r\n",b"\n")).hexdigest()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ("build","out"):
        p.add_argument("--"+name,type=lambda s:Path(s).resolve(),required=True)
    p.add_argument("--compare",type=Path)
    a=p.parse_args();a.out.mkdir(parents=True,exist_ok=False)
    root=next((d for d in (a.build,*a.build.parents) if (d/"CMakeCache.txt").exists()),None)
    if root is None:raise RuntimeError("CMake build root not found")
    cache=(root/"CMakeCache.txt").read_text()
    sanitizer="tsan" if "RAIL_TSAN:BOOL=ON" in cache else "asan_ubsan" if "RAIL_ASAN_UBSAN:BOOL=ON" in cache else "none"
    with (a.out/"ctest.txt").open("w") as output:
        run=subprocess.run(["ctest","--test-dir",str(root),"--output-on-failure","--output-junit",str(a.out/"ctest.xml"),"-j1"],stdout=output,stderr=subprocess.STDOUT)
    world=a.out/"generator.world"
    subprocess.run([str(a.build/("railgen.exe" if os.name=="nt" else "railgen")),"world",str(TASK/"configs/public/world.cfg"),"20261018",str(world)],check=True,capture_output=True)
    hashes={"world":digest(world)}
    for scenario in ("daily","spring","refund_wave"):
        path=a.out/(scenario+".load")
        path.write_text(generate(load_world(world),20261019,scenario,10000),newline="\n")
        hashes[scenario]=digest(path)
    result={"schema_version":2,"abi_version":2,"task_version":task_version(),"platform":platform.platform(),
            "os":os.name,"sanitizer":sanitizer,"tests_passed":run.returncode==0,"generator_hashes":hashes,
            "cross_platform_generator":False,"correctness":run.returncode==0,
            "asan_ubsan":run.returncode==0 and sanitizer=="asan_ubsan",
            "tsan":run.returncode==0 and sanitizer=="tsan"}
    if a.compare:
        prior=json.loads(a.compare.read_text())
        result["cross_platform_generator"]=(prior["os"]!=os.name and prior["task_version"]==result["task_version"] and prior["generator_hashes"]==hashes)
        result["compared_platform"]=prior["platform"]
    (a.out/"validation.json").write_text(json.dumps(result,indent=2))
    print(json.dumps(result,indent=2))
    raise SystemExit(run.returncode)


if __name__=="__main__":main()
