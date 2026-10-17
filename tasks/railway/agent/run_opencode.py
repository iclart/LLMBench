#!/usr/bin/env python3
"""Runs one OpenCode agent on the railway task under the official rules.

  run_opencode.py --workspace DIR|TAR.GZ --out RUN_DIR --model provider/model
                  [--minutes 150] [--snapshot-every 30] [--user NAME] [--env-file FILE]
                  [--opencode PATH] [--variant V] [--max-resumes 20] [--opencode-config FILE]

- Copies the packaged workspace to RUN_DIR/workspace and starts
  `opencode run` there with PROMPT.md, non-interactively.
- Snapshots src/ every --snapshot-every minutes into RUN_DIR/snapshots/NNN.
- Early submission: when the agent runs `make submit` (the workspace gets a
  .submit file), the agent is stopped and the final snapshot taken.
- Forced submission: at the deadline every process of the agent is killed
  first, then the final snapshot is taken, so src/ cannot change in between.
- If the agent stops before the deadline without submitting, the session is
  resumed with a "N minutes left" message (up to --max-resumes times).
- Afterwards the session is exported and token usage and cost are summarised
  in RUN_DIR/run.json.

Models: --model is a `provider/model` id from `opencode models`. It is also
written to the run's config as both `model` and `small_model`, so OpenCode
never falls back to another provider's model for titles or summaries. The
agent runs with its own HOME, so credentials saved by `opencode providers
login` are not visible to it: pass API keys with --env-file. A custom
provider (e.g. an OpenAI-compatible endpoint) goes in a JSON file given to
--opencode-config, which is merged into the run's config; its permission
settings cannot be loosened that way.

With --user (requires root) the agent runs as that dedicated account, so it
cannot read anything that account cannot (keep the LLMBench checkout out of
its reach) and vm_netguard.sh can restrict its network access. Tested with
OpenCode 1.19.3.
"""
import argparse
import json
import os
import pwd
import shutil
import signal
import subprocess
import sys
import tarfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
QUICK_EXIT_S = 30           # a run shorter than this counts as a failed start
MAX_QUICK_EXITS = 3
POLL_S = 2
TITLE = "llmbench-railway"

# Non-interactive permissions; web access is denied here and blocked by
# vm_netguard.sh at the network level.
OPENCODE_CONFIG = {
    "$schema": "https://opencode.ai/config.json",
    "autoupdate": False,
    "share": "disabled",
    "permission": {
        "edit": "allow",
        "bash": "allow",
        "webfetch": "deny",
        "websearch": "deny",
        "external_directory": "deny",
        "doom_loop": "allow",
    },
}


def log(msg):
    print(time.strftime("[%H:%M:%S] ") + msg, file=sys.stderr, flush=True)


def read_env_file(path):
    env = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


class Agent:
    def __init__(self, a, ws, home, out):
        self.a, self.ws, self.home, self.out = a, ws, home, out
        self.proc = None
        self.started = 0.0
        self.session = None
        self.events = open(out / "logs/events.jsonl", "ab")
        self.stderr = open(out / "logs/opencode.log", "ab")
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "LANG": "C.UTF-8",
                    "TERM": "dumb"}
        if a.env_file:
            self.env.update(read_env_file(a.env_file))
        self.user = pwd.getpwnam(a.user) if a.user else None

    def _preexec(self):
        os.setsid()
        if self.user:
            os.setgroups([])
            os.setgid(self.user.pw_gid)
            os.setuid(self.user.pw_uid)

    def opencode(self, *args, **kw):
        return subprocess.run([self.a.opencode, *args], env=self.env, cwd=self.ws, preexec_fn=self._preexec,
                              capture_output=True, text=True, **kw)

    def start(self, message):
        cmd = [self.a.opencode, "run", "--dir", str(self.ws), "--model", self.a.model, "--format", "json",
               "--title", TITLE, "--auto"]
        if self.a.variant:
            cmd += ["--variant", self.a.variant]
        if self.session:
            cmd += ["--session", self.session]
        cmd.append(message)
        self.events.write(f'{{"llmbench": "start", "time": {time.time():.3f}, "resume": {json.dumps(bool(self.session))}}}\n'.encode())
        self.events.flush()
        self.proc = subprocess.Popen(cmd, env=self.env, cwd=self.ws, stdout=self.events, stderr=self.stderr,
                                     stdin=subprocess.DEVNULL, preexec_fn=self._preexec)
        self.started = time.monotonic()

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def kill_all(self):
        """Stops the agent and everything it started, including escaped processes."""
        if self.proc is not None:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(self.proc.pid, sig)
                except ProcessLookupError:
                    break
                try:
                    self.proc.wait(timeout=10)
                    break
                except subprocess.TimeoutExpired:
                    continue
        if self.user:  # anything the account still runs, e.g. a daemonised build
            subprocess.run(["pkill", "-KILL", "-u", str(self.user.pw_uid)], capture_output=True)
            time.sleep(1)

    def find_session(self):
        if self.session:
            return self.session
        try:
            for line in (self.out / "logs/events.jsonl").read_text(errors="replace").splitlines():
                sid = find_key(safe_json(line), "sessionID")
                if sid:
                    self.session = sid
                    break
        except OSError:
            pass
        return self.session


def safe_json(line):
    try:
        return json.loads(line)
    except ValueError:
        return None


def find_key(obj, key):
    if isinstance(obj, dict):
        if isinstance(obj.get(key), str):
            return obj[key]
        for v in obj.values():
            r = find_key(v, key)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_key(v, key)
            if r:
                return r
    return None


def summarise_usage(export):
    """Sums tokens and cost over assistant messages of an `opencode export` dump."""
    total = {"cost": 0.0, "input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0, "messages": 0}

    def visit(o):
        if isinstance(o, dict):
            if o.get("role") == "assistant" and ("cost" in o or "tokens" in o):
                total["messages"] += 1
                total["cost"] += float(o.get("cost") or 0)
                t = o.get("tokens") or {}
                for k in ("input", "output", "reasoning"):
                    total[k] += int(t.get(k) or 0)
                cache = t.get("cache") or {}
                total["cache_read"] += int(cache.get("read") or 0)
                total["cache_write"] += int(cache.get("write") or 0)
                return
            for v in o.values():
                visit(v)
        elif isinstance(o, list):
            for v in o:
                visit(v)

    visit(export)
    return total


def snapshot_name(minutes):
    """030 for whole minutes, 000.5 otherwise; names sort in time order."""
    return f"{int(minutes):03d}" if float(minutes).is_integer() else f"{minutes:05.1f}"


def snapshot(ws, dest):
    if dest.exists():
        shutil.rmtree(dest)
    if (ws / "src").is_dir():
        shutil.copytree(ws / "src", dest / "src", symlinks=False, ignore_dangling_symlinks=True)
    else:  # the agent removed src/: an empty submission
        (dest / "src").mkdir(parents=True)


def opencode_config(a):
    cfg = json.loads(json.dumps(OPENCODE_CONFIG))
    if a.opencode_config:
        extra = json.loads(Path(a.opencode_config).read_text())
        extra.pop("permission", None)  # the rules are not negotiable
        cfg.update(extra)
    cfg["model"] = a.model
    cfg["small_model"] = a.model
    return cfg


def prepare(a, out):
    if out.exists() and any(out.iterdir()):
        sys.exit(f"{out} is not empty")
    for d in ("logs", "snapshots", "home/.config/opencode"):
        (out / d).mkdir(parents=True, exist_ok=True)
    ws = out / "workspace"
    src = Path(a.workspace)
    if src.is_dir():
        shutil.copytree(src, ws)
    else:
        with tarfile.open(src) as tar:
            tar.extractall(out, filter="data")
        if not ws.exists():
            sys.exit("the archive must contain a top-level workspace/ directory")
    home = out / "home"
    (home / ".config/opencode/opencode.json").write_text(json.dumps(opencode_config(a), indent=2))
    if a.user:
        u = pwd.getpwnam(a.user)
        for p in [ws, home, *ws.rglob("*"), *home.rglob("*")]:
            os.chown(p, u.pw_uid, u.pw_gid)
        # The agent may not loosen its own configuration.
        cfg = home / ".config/opencode/opencode.json"
        os.chown(cfg, 0, 0)
        os.chmod(cfg, 0o444)
    return ws, home


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", required=True, help="provider/model, see `opencode models`")
    ap.add_argument("--variant", help="provider-specific reasoning effort, passed to opencode --variant")
    ap.add_argument("--minutes", type=float, default=150)
    ap.add_argument("--snapshot-every", type=float, default=30)
    ap.add_argument("--user", help="run the agent as this account (requires root)")
    ap.add_argument("--env-file", help="KEY=VALUE lines, e.g. the provider API key")
    ap.add_argument("--opencode", default=shutil.which("opencode") or "opencode")
    ap.add_argument("--max-resumes", type=int, default=20)
    ap.add_argument("--opencode-config", help="JSON merged into the run's opencode config, e.g. a custom provider")
    a = ap.parse_args()
    if a.user and os.geteuid() != 0:
        sys.exit("--user requires running this script as root")

    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    ws, home = prepare(a, out)
    prompt = (HERE / "PROMPT.md").read_text()
    agent = Agent(a, ws, home, out)
    version = agent.opencode("--version").stdout.strip()

    start_wall = time.time()
    t0 = time.monotonic()
    deadline = t0 + a.minutes * 60
    (ws / ".deadline").write_text(f"{int(start_wall + a.minutes * 60)}\n")
    marks = []
    k = 1
    while round(k * a.snapshot_every, 6) < a.minutes:
        marks.append(round(k * a.snapshot_every, 6))
        k += 1

    log(f"opencode {version}, model {a.model}, {a.minutes:g} min, snapshots at {marks}")
    agent.start(prompt)
    resumes, quick_exits, end_reason = 0, 0, None
    while True:
        now = time.monotonic()
        if (ws / ".submit").exists():
            end_reason = "submitted"
            break
        if now >= deadline:
            end_reason = "deadline"
            break
        while marks and now - t0 >= marks[0] * 60:
            snapshot(ws, out / f"snapshots/{snapshot_name(marks[0])}")
            log(f"snapshot {snapshot_name(marks[0])}")
            marks.pop(0)
        if not agent.running():
            ran = now - agent.started
            log(f"agent stopped after {ran:.0f} s (exit {agent.proc.returncode})")
            quick_exits = quick_exits + 1 if ran < QUICK_EXIT_S else 0
            if quick_exits >= MAX_QUICK_EXITS:
                end_reason = "agent_failed"
                break
            if resumes >= a.max_resumes:
                end_reason = "resume_limit"
                break
            if not agent.find_session():
                end_reason = "no_session"
                break
            left = max(1, int((deadline - now) / 60))
            resumes += 1
            agent.start(f"（系统消息）距离强制收卷还有 {left} 分钟。请继续：先确保 `make check` 全部通过，再优化性能。"
                        f"确认完成后可以运行 `make submit` 提前交卷。")
        time.sleep(POLL_S)

    agent.kill_all()
    elapsed = time.monotonic() - t0
    final = f"{int(elapsed // 60):03d}_final"
    snapshot(ws, out / f"snapshots/{final}")
    log(f"{end_reason}: final snapshot {final} after {elapsed / 60:.1f} min")

    usage, session = None, agent.find_session()
    if session:
        exp = agent.opencode("export", session, timeout=300)
        if exp.returncode == 0:
            (out / "logs/session_export.json").write_text(exp.stdout)
            data = safe_json(exp.stdout[exp.stdout.find("{"):]) if "{" in exp.stdout else None
            usage = summarise_usage(data) if data is not None else None
    run = {
        "tool": "opencode", "tool_version": version, "model": a.model, "variant": a.variant,
        "minutes": a.minutes, "start": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_wall)),
        "elapsed_min": round(elapsed / 60, 2), "end_reason": end_reason,
        "submitted": end_reason == "submitted", "resumes": resumes, "session": session, "usage": usage,
        "snapshots": sorted(p.name for p in (out / "snapshots").iterdir()),
    }
    (out / "run.json").write_text(json.dumps(run, indent=2, ensure_ascii=False))
    log(json.dumps({k: run[k] for k in ("end_reason", "elapsed_min", "resumes", "usage")}, ensure_ascii=False))
    sys.exit(0 if end_reason in ("submitted", "deadline") else 1)


if __name__ == "__main__":
    main()
