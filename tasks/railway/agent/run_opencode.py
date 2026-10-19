#!/usr/bin/env python3
"""Runs one OpenCode agent on the railway task under the official rules, via
OpenCode's v2 HTTP API (`opencode serve`).

  run_opencode.py --workspace DIR|TAR.GZ --out RUN_DIR --model provider/model
                  [--minutes 105] [--recommended 90] [--snapshot-every 30] [--env-file FILE]
                  [--opencode-config FILE] [--port 4096] [--hostname 0.0.0.0]
                  [--password PW] [--strict [--user llmagent]]

What OpenCode handles: tool permissions (web access, questions to the user
and files outside the workspace are denied in the run's config) and the live
view: open http://<vm>:<port>/ in a browser (user `opencode`, the password is
printed at start and saved in RUN_DIR/web.txt).

What this script handles:
- time: the prompt states the deadline; at every snapshot mark and 10 and 2
  minutes before the end, the current time and the minutes left are steered
  into the running session; `make time` works in the workspace too;
- snapshots of src/ every --snapshot-every minutes into RUN_DIR/snapshots;
- early submission: `make submit` in the workspace ends the run;
- forced submission: at the deadline the session is interrupted and every
  agent process killed before the final snapshot is taken;
- resuming: if the agent goes idle without submitting, it is told the time
  left and asked to continue (up to --max-resumes times);
- usage: tokens and cost from the session's messages, in RUN_DIR/run.json,
  with the run's timeline (time notices, resumes, snapshots), the task
  version and the machine, for the report (report/report.py);
- secrets: the variables from --env-file reach the agent's shell too (OpenCode
  1.19.3's server mode neither runs plugin shell hooks nor uses $SHELL, and the
  agent can read its own processes' environment anyway); report.py redacts
  their values from published transcripts.

Modes. Trial (default): runs as the invoking user; only OpenCode's own
permission rules apply, but its bash tool can still reach the network and
read any file that user can. Strict (--strict, root): runs as the dedicated
account --user, which must not be able to read the LLMBench checkout, and
requires vm_netguard.sh to be active for it. Official results use --strict.

Works with OpenCode 1.x (tested with 1.19.3, npm `opencode-ai`) and 2.x
(tested with 2.0.30, npm `@opencode/cli`); the two differ in the config schema
and in a few API request shapes, chosen from `opencode --version`.
"""
import argparse
import base64
import json
import os
try:
    import pwd
except ImportError:  # local Windows trials; strict isolation remains Linux-only
    pwd = None
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import taskinfo  # noqa: E402
import transcript  # noqa: E402  (same directory)

POLL_S = 2
IDLE_GRACE_S = 5          # idle this long (and not submitted) before resuming
QUICK_IDLE_S = 30         # a resume that goes idle sooner counts as a stall
STARTUP_S = 120           # a prompt may take this long before the session shows as active
MAX_STALLS = 3
FINAL_NOTICES_MIN = (10, 2)
NETGUARD_TABLE = "llmbench_guard"


def provider_credential(auth_file, credential_db, provider):
    """Read only the selected provider; never migrate or edit a user's database."""
    if credential_db:
        uri = Path(credential_db).resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        try:
            rows = connection.execute(
                "SELECT value FROM credential WHERE integration_id=? ORDER BY time_updated DESC",
                (provider,)).fetchall()
        finally:
            connection.close()
        if not rows:
            raise RuntimeError("credential database has no entry for " + provider)
        credential = json.loads(rows[0][0])
    else:
        credential = json.loads(Path(auth_file).read_text(encoding="utf-8-sig"))[provider]
    if credential.get("type") not in ("api", "key") or not isinstance(credential.get("key"), str) or not credential["key"]:
        raise RuntimeError("disposable runs require an API-key credential")
    return {"type": "api", "key": credential["key"]}


def tool_version(executable, directory):
    """Probe in a disposable home; never touch the user's OpenCode logs/auth."""
    with tempfile.TemporaryDirectory(prefix="opencode-version-", dir=directory) as home:
        environment = os.environ.copy()
        environment.update(HOME=home, USERPROFILE=home,
                           XDG_CONFIG_HOME=str(Path(home)/"config"), XDG_DATA_HOME=str(Path(home)/"data"),
                           XDG_CACHE_HOME=str(Path(home)/"cache"), XDG_STATE_HOME=str(Path(home)/"state"))
        if os.name=="nt":
            environment.update(APPDATA=str(Path(home)/"AppData/Roaming"),LOCALAPPDATA=str(Path(home)/"AppData/Local"))
        ver=subprocess.run([executable,"--version"],env=environment,capture_output=True,text=True,timeout=30)
    version=ver.stdout.strip().removeprefix("opencode").strip().lstrip("v")
    if ver.returncode or not version[:1].isdigit():
        raise RuntimeError(f"`{executable} --version` failed:\n{ver.stdout[-500:]}{ver.stderr[-2000:]}")
    return version

# Tool permissions for the agent (OpenCode 1.x config schema, checked with 1.19.3).
PERMISSIONS = {
    "edit": "allow",
    "bash": "allow",
    "webfetch": "deny",
    "websearch": "deny",
    "question": "deny",
    "external_directory": "deny",
    "doom_loop": "allow",
}

# The same rules for OpenCode 2.x: ordered, the last matching rule wins. The
# catch-all allow also removes the default "ask" for .env files: nobody
# answers prompts here. 2.x allows paths outside the workspace by default.
PERMISSIONS_V2 = [
    {"action": "*", "resource": "*", "effect": "allow"},
    {"action": "webfetch", "resource": "*", "effect": "deny"},
    {"action": "websearch", "resource": "*", "effect": "deny"},
    {"action": "question", "resource": "*", "effect": "deny"},
    {"action": "external_directory", "resource": "*", "effect": "deny"},
]
# Built-in agents append their own rules after the global ones (build allows
# questions, explore allows web access), so each gets the denials again.
BUILTIN_AGENTS_V2 = ("build", "plan", "general", "explore")
# The agent's shell commands see only these variables (2.x can replace the
# shell environment per session, which keeps API keys out of it).
SHELL_ENV = ("PATH", "HOME", "LANG", "TERM", "USER", "LOGNAME", "TMPDIR")
if os.name == "nt":
    SHELL_ENV += ("SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA")


def log(msg):
    print(time.strftime("[%H:%M:%S] ") + msg, file=sys.stderr, flush=True)


def clock(t):
    return time.strftime("%H:%M", time.localtime(t))


# What the agent is told, word for word the same in every runner (run_manual.py too).
def start_message(prompt, start, end, minutes, recommended=None):
    rec = (f"推荐在 {clock(start + recommended * 60)}（{recommended:g} 分钟）前完成主要工作，"
           if recommended and recommended < minutes else "")
    return f"{prompt}\n（系统消息）现在是 {clock(start)}，{rec}强制收卷时间是 {clock(end)}，共 {minutes:g} 分钟。"


def notice_message(now, left_min, recommended=None):
    if recommended:
        return (f"（系统消息）现在是 {clock(now)}，已到推荐用时（{recommended:g} 分钟），距离强制收卷还有 {left_min} 分钟。"
                "请确保 src/ 随时可以交卷：剩余时间只做修错和小改进；确认已经完成可以运行 `make submit` 提前交卷。")
    return (f"（系统消息）现在是 {clock(now)}，距离强制收卷还有 {left_min} 分钟。"
            + ("请确保 src/ 始终处于能编译、能通过 `make check` 的状态。" if left_min <= 10 else ""))


def resume_message(now, left_min):
    return (f"（系统消息）你停下来了，但还没有交卷。现在是 {clock(now)}，距离强制收卷还有 {left_min} 分钟。"
            f"请继续：先确保 `make check` 全部通过，再优化性能。确认完成后可以运行 `make submit` 提前交卷。")


def notice_marks(minutes, snapshot_every, recommended=None):
    """Snapshot marks and time-notice marks, in minutes from the start."""
    marks, k = [], 1
    while round(k * snapshot_every, 6) < minutes:
        marks.append(round(k * snapshot_every, 6))
        k += 1
    notices = {*marks, *(minutes - m for m in FINAL_NOTICES_MIN if minutes - m > 0)}
    if recommended and recommended < minutes:
        notices.add(recommended)
    return marks, sorted(notices)


def read_env_file(path):
    env = {}
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


class Api:
    """Minimal client for OpenCode's v2 HTTP API."""

    def __init__(self, port, password, v2=False):
        self.v2 = v2
        self.base = f"http://127.0.0.1:{port}"
        self.auth = "Basic " + base64.b64encode(f"opencode:{password}".encode()).decode()

    def call(self, method, path, body=None, timeout=30):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", "Authorization": self.auth})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = r.read().decode()
        return json.loads(text) if text else None

    def healthy(self):
        try:  # 1.x has /api/health; 2.x answers /api/info once it serves requests
            if self.v2:
                return bool(self.call("GET", "/api/info", timeout=3).get("version"))
            return bool(self.call("GET", "/api/health", timeout=3).get("healthy"))
        except (OSError, ValueError, urllib.error.URLError):
            return False


class Run:
    def __init__(self, a, out, ws, home):
        self.a, self.out, self.ws, self.home = a, out, ws, home
        self.user = pwd.getpwnam(a.user) if a.strict else None
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "LANG": "C.UTF-8",
                    "TERM": "dumb", "OPENCODE_SERVER_PASSWORD": a.password}
        self.env.update(XDG_CONFIG_HOME=str(home/".config"),XDG_DATA_HOME=str(home/".local/share"),
                        XDG_CACHE_HOME=str(home/".cache"),XDG_STATE_HOME=str(home/".local/state"))
        if os.name == "nt":
            for key in ("SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP"):
                if key in os.environ:
                    self.env[key] = os.environ[key]
            self.env.update(USERPROFILE=str(home), APPDATA=str(home / "AppData/Roaming"), LOCALAPPDATA=str(home / "AppData/Local"))
        self.job = None
        self.imported_credentials = []
        if a.env_file:
            self.env.update(read_env_file(a.env_file))
        self.server = None
        self.v2 = a.major >= 2
        self.api = Api(a.port, a.password, self.v2)
        self.session = None
        self.prompted_at = 0.0
        self.started = False  # the session became active after the last prompt

    def _preexec(self):
        os.setsid()
        if self.user:
            os.setgroups([])
            os.setgid(self.user.pw_gid)
            os.setuid(self.user.pw_uid)

    def as_agent(self, argv, **kw):
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"preexec_fn": self._preexec}
        return subprocess.run(argv, env=self.env, cwd=self.ws, **options, capture_output=True,
                              text=True, **kw)

    def start_server(self):
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"preexec_fn": self._preexec}
        self.server = subprocess.Popen(
            [self.a.opencode, "serve", "--port", str(self.a.port), "--hostname", self.a.hostname],
            env=self.env, cwd=self.ws, **options, stdin=subprocess.DEVNULL,
            stdout=open(self.out / "logs/server.log", "ab"), stderr=subprocess.STDOUT)
        if os.name == "nt":
            from process_control import WindowsJob
            self.job = WindowsJob(); self.job.attach(self.server)
        for _ in range(120):
            if self.api.healthy():
                if (self.a.auth_file or self.a.credential_db) and self.v2:
                    provider=self.a.model.partition("/")[0]
                    credential=provider_credential(self.a.auth_file, self.a.credential_db, provider)
                    # V2 stores accounts in SQLite. Use its public connection
                    # API rather than depending on a legacy-file migration.
                    integrations=self.api.call("GET","/api/integration")["data"]
                    matches=[i["id"] for i in integrations if i["id"]==provider or any(i["id"].endswith(separator+provider) for separator in ("_",":","/"))]
                    if len(matches)!=1:
                        self.stop();raise RuntimeError("cannot identify integration for "+provider)
                    previous={c["id"] for c in self.api.call("GET","/api/credential")["data"]}
                    self.api.call("POST",f"/api/integration/{urllib.parse.quote(matches[0],safe='')}/connect/key",{"key":credential["key"],"label":"LLMBench disposable run"})
                    self.imported_credentials=[c["id"] for c in self.api.call("GET","/api/credential")["data"] if c["id"] not in previous]
                return
            if self.server.poll() is not None:
                break
            time.sleep(0.5)
        self.stop()
        sys.exit(f"opencode serve did not come up; see {self.out / 'logs/server.log'}")

    def server_alive(self):
        return self.server is not None and self.server.poll() is None

    def wait_for_model(self):
        """The model catalog loads lazily: a prompt sent before it is ready fails
        with "Model unavailable", so ask for it until the model shows up."""
        provider, _, model = self.a.model.partition("/")
        for _ in range(60):
            try:
                models = self.api.call("GET", "/api/model").get("data") or []
                info = next((m for m in models if m.get("providerID") == provider and m.get("id") == model), None)
                if info:
                    self.check_variant(info)
                    return
            except (OSError, ValueError, urllib.error.URLError):
                pass
            time.sleep(1)
        self.stop()
        sys.exit(f"OpenCode does not list {self.a.model}; see the server log. No model substitution was made.")

    def check_variant(self, info):
        """An unknown variant only fails at the first prompt, so check it up front."""
        if "variants" not in info:
            return
        names = [v.get("id") for v in info["variants"] or [] if v.get("id")]
        log(f"variants of {self.a.model}: {', '.join(names) or '(none)'}; using {self.a.variant or 'the default'}")
        if self.a.variant and self.a.variant not in names:
            self.stop()
            sys.exit(f"{self.a.model} has no variant {self.a.variant!r}; available: {', '.join(names) or 'none'}. "
                     f"Custom variants go in --opencode-config (see the README).")

    def create_session(self):
        provider, _, model = self.a.model.partition("/")
        ref = {"providerID": provider, "id": model}
        if self.a.variant:
            ref["variant"] = self.a.variant
        self.session = self.api.call("POST", "/api/session",
                                     {"model": ref, "location": {"directory": str(self.ws)}})["data"]["id"]
        if self.v2:  # keep the API keys out of the agent's shell commands
            shell_env = {k: self.env[k] for k in SHELL_ENV if k in self.env}
            self.api.call("PUT", f"/api/session/{self.session}/environment", {"variables": shell_env})

    def say(self, text, steer=False):
        body = {"text": text} if self.v2 else {"prompt": {"text": text}}
        if steer:
            body["delivery"] = "steer"
        self.api.call("POST", f"/api/session/{self.session}/prompt", body)
        if not steer:
            self.prompted_at, self.started = time.monotonic(), False

    def active(self):
        if self.session in self.api.call("GET", "/api/session/active")["data"]:
            self.started = True
            return True
        # Right after a prompt the session takes a moment to show as active.
        return not self.started and time.monotonic() - self.prompted_at < STARTUP_S

    def fatal_provider_error(self):
        if not self.v2:return None
        page=self.api.call("GET",f"/api/session/{self.session}/message?limit=20&order=desc")
        for message in page.get("data") or []:
            error=message.get("error") or {}
            if error.get("type")=="provider.auth" or error.get("status") in (401,402,403):
                return {"type":error.get("type"),"status":error.get("status"),"message":error.get("message")}
        return None

    def reject_questions(self):
        """Fallback: questions and permission prompts are denied by config, but
        never let one block the run (2.x: forms and permission requests)."""
        sid = self.session
        if not self.v2:
            for q in self.api.call("GET", f"/api/session/{sid}/question").get("data") or []:
                self.api.call("POST", f"/api/session/{sid}/question/{q['id']}/reject")
                log(f"rejected a question to the user ({q['id']})")
            return
        for f in self.api.call("GET", f"/api/session/{sid}/form").get("data") or []:
            self.api.call("DELETE", f"/api/session/{sid}/form/{f['id']}?message=" +
                          urllib.parse.quote("No one can answer: decide on your own."))
            log(f"cancelled a form for the user ({f['id']})")
        for r in self.api.call("GET", f"/api/session/{sid}/permission").get("data") or []:
            self.api.call("POST", f"/api/session/{sid}/permission/{r['id']}/reply", {"decision": "reject"})
            log(f"rejected a permission request ({r.get('action')} {r.get('resources')})")

    def stop(self):
        """Interrupts the session and kills every process of the agent."""
        if self.session and self.server_alive():
            try:
                self.api.call("POST", f"/api/session/{self.session}/interrupt", timeout=10)
            except (OSError, urllib.error.URLError):
                pass
        if self.server_alive():
            for credential in self.imported_credentials:
                try:self.api.call("DELETE",f"/api/credential/{credential}",timeout=3)
                except (OSError,ValueError,urllib.error.URLError):pass
            self.imported_credentials=[]
        if self.server is not None and os.name == "nt":
            if self.job:
                self.job.terminate(); self.job.close(); self.job = None
            if self.server.poll() is None:
                self.server.kill()
            self.server.wait(timeout=10)
        elif self.server is not None:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(self.server.pid, sig)
                except ProcessLookupError:
                    break
                try:
                    self.server.wait(timeout=10)
                    break
                except subprocess.TimeoutExpired:
                    continue
        if self.user:  # anything the account still runs, e.g. a daemonised build
            subprocess.run(["pkill", "-KILL", "-u", str(self.user.pw_uid)], capture_output=True)
            time.sleep(1)
        if self.a.auth_file or self.a.credential_db:
            (self.home/".local/share/opencode/auth.json").unlink(missing_ok=True)

    def usage(self):
        """Sums tokens and cost over the session's assistant messages."""
        total = {"cost": 0.0, "input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0,
                 "messages": 0, "tool_calls": 0}
        messages, cursor = [], None
        while True:
            # The cursor encodes the order; sending `order` again with it is rejected.
            q = f"/api/session/{self.session}/message?limit=200&" + (f"cursor={cursor}" if cursor else "order=asc")
            page = self.api.call("GET", q)
            messages += page.get("data") or []
            cursor = (page.get("cursor") or {}).get("next")
            if not cursor or not page.get("data"):
                break
        (self.out / "logs/messages.json").write_text(json.dumps(messages, ensure_ascii=False, indent=1),encoding="utf-8")
        (self.out / "logs/transcript.json").write_text(
            json.dumps(transcript.from_opencode(messages), ensure_ascii=False, indent=1),encoding="utf-8")
        for m in messages:
            if m.get("type") != "assistant":
                continue
            total["messages"] += 1
            total["cost"] += float(m.get("cost") or 0)
            t = m.get("tokens") or {}
            for k in ("input", "output", "reasoning"):
                total[k] += int(t.get(k) or 0)
            total["cache_read"] += int((t.get("cache") or {}).get("read") or 0)
            total["cache_write"] += int((t.get("cache") or {}).get("write") or 0)
            total["tool_calls"] += sum(1 for c in m.get("content") or [] if c.get("type") == "tool")
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
    cfg = {"$schema": "https://opencode.ai/config.json", "share": "disabled"}
    cfg["update" if a.major >= 2 else "autoupdate"] = "disable" if a.major >= 2 else False
    if a.opencode_config:
        extra = json.loads(Path(a.opencode_config).read_text())
        for key in ("permission", "permissions", "tools"):  # the rules are not negotiable
            extra.pop(key, None)
        for agent in list((extra.get("agents") or {}).values()) + list((extra.get("agent") or {}).values()):
            if isinstance(agent, dict):
                for key in ("permission", "permissions", "tools"):
                    agent.pop(key, None)
        cfg.update(extra)
    if a.major >= 2:
        cfg["permissions"] = list(PERMISSIONS_V2)
        agents = cfg.setdefault("agents", {})
        for name in BUILTIN_AGENTS_V2:
            agents.setdefault(name, {})["permissions"] = PERMISSIONS_V2[1:]
    else:
        cfg["permission"] = dict(PERMISSIONS)
    cfg["model"] = a.model
    cfg["small_model"] = a.model  # never fall back to another provider's model (2.x: the title agent)
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
    cfg = home / ".config/opencode/opencode.json"
    cfg.write_text(json.dumps(opencode_config(a), indent=2))
    if a.auth_file or a.credential_db:
        provider=a.model.partition("/")[0]
        credential=provider_credential(a.auth_file, a.credential_db, provider)
        if a.major<2:
            auth=home/".local/share/opencode/auth.json"
            auth.parent.mkdir(parents=True,exist_ok=True)
            auth.write_text(json.dumps({provider:credential}));auth.chmod(0o600)
    web = out / "web.txt"
    web.write_text(f"url: http://<this machine>:{a.port}/\nuser: opencode\npassword: {a.password}\n")
    web.chmod(0o600)
    if a.strict:
        u = pwd.getpwnam(a.user)
        for p in [ws, home, *ws.rglob("*"), *home.rglob("*")]:
            os.chown(p, u.pw_uid, u.pw_gid)
        os.chown(cfg, 0, 0)  # the agent may not loosen its own configuration
        os.chmod(cfg, 0o444)
    return ws, home


def check_strict(a, run):
    if os.geteuid() != 0:
        sys.exit("--strict requires running this script as root")
    if subprocess.run(["nft", "list", "table", "inet", NETGUARD_TABLE], capture_output=True).returncode != 0:
        sys.exit(f"--strict requires the network guard: sudo {HERE / 'vm_netguard.sh'} enable {a.user} <api hosts>")
    probe = run.as_agent(["sh", "-c", 'test -w "$1" && test -w "$2"', "sh", str(run.ws), str(run.home)])
    if probe.returncode != 0:
        sys.exit(f"{a.user} cannot write {run.ws} and {run.home}: every parent directory of --out must be "
                 f"traversable by it (chmod o+x), e.g. put runs under /srv/llmbench")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", required=True, help="provider/model or provider/model#variant, see `opencode models`")
    ap.add_argument("--variant", help="reasoning effort or other model variant, e.g. high (listed at start)")
    ap.add_argument("--minutes", type=float, default=taskinfo.MINUTES)
    ap.add_argument("--recommended", type=float, default=taskinfo.RECOMMENDED_MINUTES,
                    help="minute of the 'recommended time reached' notice; 0 disables it")
    ap.add_argument("--snapshot-every", type=float, default=taskinfo.SNAPSHOT_EVERY_MINUTES)
    ap.add_argument("--env-file", help="KEY=VALUE lines, e.g. the provider API key")
    auth_group=ap.add_mutually_exclusive_group()
    auth_group.add_argument("--auth-file",help="existing OpenCode auth.json; import only the requested provider")
    auth_group.add_argument("--credential-db",help="read-only OpenCode 2 SQLite source; import only the requested provider")
    ap.add_argument("--opencode-config", help="JSON merged into the run's opencode config, e.g. a custom provider")
    ap.add_argument("--opencode", default=shutil.which("opencode") or "opencode")
    ap.add_argument("--port", type=int, default=4096)
    ap.add_argument("--hostname", default="0.0.0.0", help="where the web UI listens")
    ap.add_argument("--password", default=None, help="web UI / API password (default: random)")
    ap.add_argument("--max-resumes", type=int, default=20)
    ap.add_argument("--strict", action="store_true", help="official mode: dedicated account + network guard")
    ap.add_argument("--user", default="llmagent", help="the dedicated account for --strict")
    a = ap.parse_args()
    if a.strict and os.name == "nt":
        sys.exit("--strict requires Linux; Windows runs are explicitly local trials")
    a.password = a.password or secrets.token_urlsafe(12)
    if "#" in a.model:  # OpenCode 2 style: provider/model#variant
        a.model, _, variant = a.model.partition("#")
        if a.variant and a.variant != variant:
            sys.exit(f"conflicting variants: #{variant} in --model and --variant {a.variant}")
        a.variant = variant

    Path(a.out).resolve().parent.mkdir(parents=True,exist_ok=True)
    version = tool_version(a.opencode,Path(a.out).resolve().parent)
    a.major = int(version.split(".")[0])

    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    ws, home = prepare(a, out)
    try:  # read before the agent can touch it
        workspace_version = json.loads((ws / taskinfo.VERSION_FILE).read_text())
    except (OSError, ValueError):
        workspace_version = {}
    trial_cpus=None
    if workspace_version.get("abi_version")==2:
        sys.path.insert(0,str(HERE.parent/"eval"))
        from compare_reference import pin_four_cores
        trial_cpus=pin_four_cores()
    run = Run(a, out, ws, home)
    if a.strict:
        check_strict(a, run)

    run.start_server()
    run.wait_for_model()
    run.create_session()
    start = time.time()
    t0 = time.monotonic()
    end_wall = start + a.minutes * 60
    deadline = t0 + a.minutes * 60
    (ws / ".deadline").write_text(f"{int(end_wall)}\n")
    marks, notices = notice_marks(a.minutes, a.snapshot_every, a.recommended)

    log(f"opencode {version}, model {a.model}, {'strict' if a.strict else 'trial'} mode, {a.minutes:g} min")
    log(f"live view: http://<this machine>:{a.port}/  user opencode  password {a.password}  (also in {out / 'web.txt'})")
    log(f"session {run.session}; forced submission at {clock(end_wall)}")
    prompt_path = ws / "PROMPT.md" if workspace_version.get("abi_version") == 2 else HERE / "PROMPT.md"
    prompt = prompt_path.read_text(encoding="utf-8")
    events = []

    def event(kind, **kw):
        events.append({"t_min": round((time.monotonic() - t0) / 60, 3), "kind": kind, **kw})

    run.say(start_message(prompt, start, end_wall, a.minutes, a.recommended))

    resumes, stalls, end_reason = 0, 0, None
    provider_error = None
    idle_since, last_resume = None, time.monotonic()
    while True:
        time.sleep(POLL_S)
        now = time.monotonic()
        if (ws / ".submit").exists():
            end_reason = "submitted"
            break
        if now >= deadline:
            end_reason = "deadline"
            break
        if not run.server_alive():
            end_reason = "server_died"
            break
        elapsed_min = (now - t0) / 60
        left = max(1, round((deadline - now) / 60))
        while marks and elapsed_min >= marks[0]:
            snapshot(ws, out / f"snapshots/{snapshot_name(marks[0])}")
            event("snapshot", name=snapshot_name(marks[0]))
            log(f"snapshot {snapshot_name(marks[0])}")
            marks.pop(0)
        try:
            if notices and elapsed_min >= notices[0]:
                rec = a.recommended if notices.pop(0) == a.recommended else None
                run.say(notice_message(time.time(), left, rec), steer=True)
                event("notice", left_min=left)
                log(f"time notice: {left} min left")
            run.reject_questions()
            active=run.active()
            if not active or not run.started:
                provider_error=run.fatal_provider_error()
                if provider_error:
                    end_reason="provider_unavailable";break
            if active:
                idle_since = None
                continue
            idle_since = idle_since or now
            if now - idle_since < IDLE_GRACE_S:
                continue
            stalls = stalls + 1 if now - last_resume < QUICK_IDLE_S else 0
            if stalls >= MAX_STALLS:
                end_reason = "agent_stalled"
                break
            if resumes >= a.max_resumes:
                end_reason = "resume_limit"
                break
            resumes += 1
            idle_since, last_resume = None, now
            run.say(resume_message(time.time(), left))
            event("resume", n=resumes)
            log(f"agent idle without submitting: resumed ({resumes})")
        except (OSError, ValueError, urllib.error.URLError) as e:
            log(f"api error: {e}")

    usage = None
    try:
        usage = run.usage()
    except (OSError, ValueError, urllib.error.URLError) as e:
        log(f"could not read usage: {e}")
    run.stop()
    event(end_reason)
    elapsed = time.monotonic() - t0
    final = f"{int(elapsed // 60):03d}_final"
    snapshot(ws, out / f"snapshots/{final}")
    log(f"{end_reason}: final snapshot {final} after {elapsed / 60:.1f} min")

    result = {
        "tool": "opencode", "tool_version": version, "api": "v2", "model": a.model, "variant": a.variant,
        "abi_version": workspace_version.get("abi_version", 1), "platform": os.name,
        "cpus":trial_cpus,
        "mode": "strict" if a.strict else "trial", "minutes": a.minutes, "recommended": a.recommended,
        "start": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start)), "start_epoch": start,
        "task_version": workspace_version.get("task_version"), "public_seed": workspace_version.get("public_seed"),
        "prompt_sha": taskinfo.prompt_sha(prompt), "machine": taskinfo.machine_info(), "events": events,
        "elapsed_min": round(elapsed / 60, 2), "end_reason": end_reason, "submitted": end_reason == "submitted",
        "resumes": resumes, "session": run.session, "usage": usage,
        "provider_error":provider_error,
        "snapshots": sorted(p.name for p in (out / "snapshots").iterdir()),
    }
    (out / "run.json").write_text(json.dumps(result, indent=2, ensure_ascii=False),encoding="utf-8")
    log(json.dumps({k: result[k] for k in ("end_reason", "elapsed_min", "resumes", "usage")}, ensure_ascii=False))
    sys.exit(0 if end_reason in ("submitted", "deadline") else 1)


if __name__ == "__main__":
    main()
