#!/usr/bin/env python3
"""Three clean, sequential 105-minute trials, with exact tool identity preflight.

Does not silently substitute a newer tool/model. Windows trials are recorded
as such; Linux isolation and sanitizer evidence remain independent gates.
"""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
from evaluate import TASK
sys.path.insert(0,str(TASK/"agent"))
from run_opencode import tool_version

EXPECTED={"model":"opencode-go/deepseek-v4.1-flash","variant":"max","tool_version":"2.0.32","minutes":105}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workspace",type=Path,required=True)
    p.add_argument("--out",type=Path,required=True)
    p.add_argument("--opencode",default=shutil.which("opencode") or "opencode")
    p.add_argument("--env-file",type=Path)
    p.add_argument("--auth-file",type=Path)
    p.add_argument("--opencode-config",type=Path)
    p.add_argument("--execute",action="store_true",help="run after preflight; otherwise only record availability")
    p.add_argument("--strict",action="store_true")
    a=p.parse_args();a.out.mkdir(parents=True,exist_ok=False)
    result={"schema_version":2,"expected":EXPECTED,"trials":[],"gate_passed":False}
    try:
        version=tool_version(a.opencode,a.out)
        result["actual_tool_version"]=version
        if version!=EXPECTED["tool_version"]:
            result["status"]="tool_version_differs"
        elif not a.execute:
            result["status"]="preflight_only"
        else:
            result["status"]="running"
            for i in range(3):
                command=[sys.executable,str(TASK/"agent/run_opencode.py"),"--workspace",str(a.workspace.resolve()),
                         "--out",str((a.out/f"run-{i+1}").resolve()),"--opencode",a.opencode,
                         "--model",EXPECTED["model"],"--variant",EXPECTED["variant"],"--minutes","105",
                         "--hostname","127.0.0.1"]
                if a.env_file:command.extend(("--env-file",str(a.env_file.resolve())))
                if a.auth_file:command.extend(("--auth-file",str(a.auth_file.resolve())))
                if a.opencode_config:command.extend(("--opencode-config",str(a.opencode_config.resolve())))
                if a.strict:command.append("--strict")
                run=subprocess.run(command)
                result["trials"].append({"id":i+1,"runner_exit":run.returncode,"path":f"run-{i+1}/run.json"})
                (a.out/"preflight.json").write_text(json.dumps(result,indent=2))
                if run.returncode:
                    result["status"]="runner_failed";break
            else:result["status"]="needs_frozen_evaluation"
    except (OSError,RuntimeError,subprocess.TimeoutExpired) as error:
        result.update(status="unavailable",reason=str(error))
    (a.out/"preflight.json").write_text(json.dumps(result,indent=2))
    print(json.dumps(result,indent=2))


if __name__=="__main__":main()
