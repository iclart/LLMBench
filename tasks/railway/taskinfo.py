"""Task version and machine description shared by the railway scripts.

The task version is a hash of everything that defines the task for the
agent: the spec, the prompt, the interface, the workspace Makefile and the
public configs. A season records the version it was prepared with; a run
records the version of the workspace it was given and of the prompt it
was sent. The report only ranks runs whose versions match their season.
"""
import hashlib
import os
import platform
import subprocess
from pathlib import Path

TASK = Path(__file__).resolve().parent
PROMPT = TASK / "agent/PROMPT.md"
VERSION_FILE = ".task_version"   # written into packaged workspaces


def _task_files():
    files = [TASK / "TASK.md", PROMPT, TASK / "include/ticket_system.h", TASK / "agent/workspace/Makefile"]
    return files + sorted((TASK / "configs/public").glob("*.cfg"))


def _text_hash(data):
    # Line endings depend on the checkout (Windows); the content does not.
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def task_version():
    h = hashlib.sha256()
    for f in _task_files():
        h.update(f.relative_to(TASK).as_posix().encode() + b"\0" + _text_hash(f.read_bytes()).encode() + b"\0")
    return h.hexdigest()[:12]


def prompt_sha(text=None):
    data = PROMPT.read_bytes() if text is None else text.encode()
    return _text_hash(data)[:12]


def machine_info():
    info = {"os": platform.platform(), "kernel": platform.release(), "python": platform.python_version(),
            "logical_cpus": os.cpu_count()}
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                info["cpu"] = line.split(":", 1)[1].strip()
                break
    except OSError:
        info["cpu"] = platform.processor() or None
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                info["distro"] = line.split("=", 1)[1].strip('"')
    except OSError:
        pass
    cxx = os.environ.get("CXX", "g++")
    try:
        v = subprocess.run([cxx, "-dumpfullversion"], capture_output=True, text=True, timeout=10).stdout.strip()
        info["compiler"] = f"{cxx} {v}" if v else None
    except (OSError, subprocess.SubprocessError):
        info["compiler"] = None
    return info


# Canonical names of agent harnesses: run.json stores the key, the report
# shows the value. run_manual.py warns about names not listed here, so one
# harness does not end up under several spellings.
TOOLS = {
    "opencode": "OpenCode",
    "claude-code": "Claude Code",
    "codex": "Codex CLI",
    "gemini-cli": "Gemini CLI",
    "qwen-code": "Qwen Code",
    "kimi-cli": "Kimi CLI",
    "cursor": "Cursor",
    "windsurf": "Windsurf",
    "trae": "Trae",
    "copilot": "GitHub Copilot",
    "cline": "Cline",
    "roo-code": "Roo Code",
    "kilo-code": "Kilo Code",
    "aider": "Aider",
    "amp": "Amp",
    "goose": "Goose",
    "crush": "Crush",
}


def tool_name(key):
    return TOOLS.get(key, key)
