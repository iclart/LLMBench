#!/usr/bin/env python3
"""Manual mode: runs the railway task with any agent harness (Claude Code,
Codex CLI, Cursor, ...) driven by a person, under the same rules as
run_opencode.py.

  run_manual.py run    --workspace DIR|TAR.GZ --out RUN_DIR --tool NAME --model provider/model
                       [--tool-version V] [--variant V] [--minutes 150] [--snapshot-every 30]
                       [--strict [--user llmagent]] [--on-deadline CMD]
  run_manual.py attach --out RUN_DIR [--transcript FORMAT FILE] [--cost USD] [--input-tokens N] [--output-tokens N]

The runner keeps the rules; the operator relays text, word for word:
- start: the runner prepares the workspace and, when you press Enter, starts
  the clock and writes the prompt with the start and deadline times to
  RUN_DIR/prompt.txt (and the clipboard, if wl-copy/xclip/xsel/pbcopy exist).
  Open the workspace in the harness and paste it as the first message.
- time notices: at the same marks as run_opencode.py the runner rings and
  prints a notice (also copied); paste it into the session as it is.
- resuming: if the agent stops without submitting, type `r` + Enter here and
  paste the message it prints. Nothing else may be said to the agent.
- submission: `make submit` in the workspace ends the run; at the deadline the
  final snapshot is taken at once and is the result, whatever the harness
  does afterwards. --strict also kills every process of the agent account;
  otherwise --on-deadline runs a command of your choice (e.g. to close the
  harness). Then src/ is made read-only.
- afterwards: tokens and cost (optional), and a session log to import
  (`attach` does the same later). Formats: see transcript.py.

--strict (root): the harness must run as --user, started with the command the
runner prints, with vm_netguard.sh active and the harness's own API hosts
allowed. Log the harness in under that account beforehand.

Keys while running: r = resume message, n = repeat the last notice,
p = repeat the prompt, s = status, h = help.
"""
import argparse
import json
import os
import pwd
import select
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import taskinfo  # noqa: E402
import transcript  # noqa: E402
from run_opencode import (NETGUARD_TABLE, clock, log, notice_marks, notice_message, resume_message,  # noqa: E402
                          snapshot, snapshot_name, start_message)

MAX_RESUMES = 20
CLIPBOARD = (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"], ["pbcopy"])


def copy_to_clipboard(text):
    for cmd in CLIPBOARD:
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, input=text, text=True, timeout=5, check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            except (OSError, subprocess.SubprocessError):
                continue
    return False


def relay(title, text):
    """Shows text the operator must paste into the session as it is."""
    copied = copy_to_clipboard(text)
    bar = "=" * 72
    print(f"\a\n{bar}\n{title}{'（已复制到剪贴板）' if copied else ''}，原样粘贴进会话：\n{bar}\n{text}\n{bar}\n",
          flush=True)


def ask(question):
    if not sys.stdin.isatty():
        return ""
    try:
        return input(question).strip()
    except EOFError:
        return ""


def prepare(a, out):
    if out.exists() and any(out.iterdir()):
        sys.exit(f"{out} is not empty")
    for d in ("logs", "snapshots"):
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
    if a.strict:
        u = pwd.getpwnam(a.user)
        for p in [ws, *ws.rglob("*")]:
            os.chown(p, u.pw_uid, u.pw_gid)
    return ws


def check_strict(a, ws):
    if os.geteuid() != 0:
        sys.exit("--strict requires running this script as root")
    if subprocess.run(["nft", "list", "table", "inet", NETGUARD_TABLE], capture_output=True).returncode != 0:
        sys.exit(f"--strict requires the network guard: sudo {HERE / 'vm_netguard.sh'} enable {a.user} <api hosts>")
    probe = subprocess.run(["sudo", "-u", a.user, "test", "-w", str(ws)], capture_output=True)
    if probe.returncode != 0:
        sys.exit(f"{a.user} cannot write {ws}: every parent directory of --out must be traversable by it "
                 f"(chmod o+x), e.g. put runs under /srv/llmbench")


def finish_usage(out, run, transcript_spec=None, cost=None, input_tokens=None, output_tokens=None):
    """Adds an imported session log and/or hand-entered usage to run.json."""
    if transcript_spec:
        fmt, path = transcript_spec
        if fmt not in transcript.CONVERTERS:
            sys.exit(f"unknown transcript format {fmt}; known: {', '.join(transcript.CONVERTERS)}")
        t = transcript.CONVERTERS[fmt](path)
        (out / "logs/transcript.json").write_text(json.dumps(t, ensure_ascii=False, indent=1))
        run["transcript"] = {"format": fmt, "file": str(Path(path).resolve()), "events": len(t["events"])}
        u = t["usage"]
        run["usage"] = {"cost": u["cost"], "input": u["input"], "output": u["output"], "reasoning": u["reasoning"],
                        "cache_read": u["cache_read"], "cache_write": u["cache_write"], "messages": u["steps"],
                        "tool_calls": u["tool_calls"], "source": fmt}
        log(f"imported {len(t['events'])} events from {path}")
    if cost is not None or input_tokens is not None or output_tokens is not None:
        usage = run.get("usage") or {}
        for key, value in (("cost", cost), ("input", input_tokens), ("output", output_tokens)):
            if value is not None:
                usage[key] = value
        usage.setdefault("source", "manual")
        run["usage"] = usage


def number(text, kind):
    try:
        return kind(text) if text else None
    except ValueError:
        log(f"not a number: {text!r}, skipped")
        return None


def cmd_run(a):
    if a.tool not in taskinfo.TOOLS:
        known = ", ".join(taskinfo.TOOLS)
        if not a.tool_unlisted:
            sys.exit(f"--tool {a.tool!r} is not a known harness name ({known}).\n"
                     f"Use one of these, or pass --tool-unlisted if it really is a new harness.")
    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    ws = prepare(a, out)
    try:  # read before the agent can touch it
        workspace_version = json.loads((ws / taskinfo.VERSION_FILE).read_text())
    except (OSError, ValueError):
        workspace_version = {}
    if a.strict:
        check_strict(a, ws)

    name = taskinfo.tool_name(a.tool)
    print(f"\n工作区：{ws}\n工具：{name} {a.tool_version or ''}  模型：{a.model}{f' ({a.variant})' if a.variant else ''}"
          f"  模式：{'正式' if a.strict else '试跑'}  时限：{a.minutes:g} 分钟")
    if a.strict:
        print(f"以 {a.user} 身份在工作区启动工具，例如：\n  sudo -u {a.user} -H bash -c 'cd {ws} && <启动 {name} 的命令>'")
    else:
        print(f"在工作区启动 {name}（工作目录设为 {ws}）。")
    print("规则：只粘贴运行器给出的文本（提示词、时间提醒、续跑语），不做任何额外指导；工具里的权限设为自动批准，"
          "但禁止联网搜索和抓取网页。")
    ask("准备好后按 Enter 开始计时……")

    start = time.time()
    t0 = time.monotonic()
    end_wall = start + a.minutes * 60
    deadline = t0 + a.minutes * 60
    (ws / ".deadline").write_text(f"{int(end_wall)}\n")
    if a.strict:
        u = pwd.getpwnam(a.user)
        os.chown(ws / ".deadline", u.pw_uid, u.pw_gid)
    prompt = (HERE / "PROMPT.md").read_text()
    first = start_message(prompt, start, end_wall, a.minutes)
    (out / "prompt.txt").write_text(first)
    relay(f"提示词（{out / 'prompt.txt'}）", first)
    marks, notices = notice_marks(a.minutes, a.snapshot_every)
    events = []

    def event(kind, **kw):
        events.append({"t_min": round((time.monotonic() - t0) / 60, 3), "kind": kind, **kw})

    last_notice, resumes, end_reason = None, 0, None
    log(f"started; forced submission at {clock(end_wall)}. keys: r resume, n notice, p prompt, s status, h help")
    interactive = sys.stdin.isatty()
    while True:
        now = time.monotonic()
        if (ws / ".submit").exists():
            end_reason = "submitted"
            break
        if now >= deadline:
            end_reason = "deadline"
            break
        elapsed_min = (now - t0) / 60
        left = max(1, round((deadline - now) / 60))
        while marks and elapsed_min >= marks[0]:
            snapshot(ws, out / f"snapshots/{snapshot_name(marks[0])}")
            event("snapshot", name=snapshot_name(marks[0]))
            log(f"snapshot {snapshot_name(marks[0])}")
            marks.pop(0)
        if notices and elapsed_min >= notices[0]:
            notices.pop(0)
            last_notice = notice_message(time.time(), left)
            relay(f"时间提醒（还剩 {left} 分钟）", last_notice)
            event("notice", left_min=left)
        ready = select.select([sys.stdin], [], [], 1.0)[0] if interactive else time.sleep(1)
        if not ready:
            continue
        key = sys.stdin.readline().strip().lower()
        if key == "r":
            if resumes >= MAX_RESUMES:
                log(f"resume limit ({MAX_RESUMES}) reached: the run ends now")
                end_reason = "resume_limit"
                break
            resumes += 1
            event("resume", n=resumes)
            relay(f"续跑语（第 {resumes} 次）", resume_message(time.time(), left))
        elif key == "n":
            if last_notice:
                relay("上一条时间提醒", last_notice)
            else:
                log("no notice yet")
        elif key == "p":
            relay("提示词", first)
        elif key == "s":
            log(f"{elapsed_min:.1f} min elapsed, {left} min left, {resumes} resumes, next snapshot "
                f"{marks[0] if marks else '-'} min")
        elif key:
            log("keys: r resume message, n last notice, p prompt, s status")

    elapsed = time.monotonic() - t0
    final = f"{int(elapsed // 60):03d}_final"
    snapshot(ws, out / f"snapshots/{final}")
    event(end_reason)
    print(f"\a\n{'#' * 72}\n{'已交卷' if end_reason == 'submitted' else '已收卷'}：最终快照 {final}，"
          f"之后的任何改动都不计入成绩。请关闭 {name}。\n{'#' * 72}\n", flush=True)
    if a.strict:
        u = pwd.getpwnam(a.user)
        subprocess.run(["pkill", "-KILL", "-u", str(u.pw_uid)], capture_output=True)
        log(f"killed every process of {a.user}")
    elif a.on_deadline:
        subprocess.run(a.on_deadline, shell=True)
    subprocess.run(["chmod", "-R", "a-w", str(ws / "src")], capture_output=True)

    run = {
        "tool": a.tool, "tool_version": a.tool_version, "manual": True, "model": a.model, "variant": a.variant,
        "mode": "strict" if a.strict else "trial", "minutes": a.minutes,
        "start": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start)), "start_epoch": start,
        "elapsed_min": round(elapsed / 60, 2), "end_reason": end_reason, "submitted": end_reason == "submitted",
        "resumes": resumes, "usage": None,
        "task_version": workspace_version.get("task_version"), "public_seed": workspace_version.get("public_seed"),
        "prompt_sha": taskinfo.prompt_sha(prompt), "machine": taskinfo.machine_info(), "events": events,
        "snapshots": sorted(p.name for p in (out / "snapshots").iterdir()),
    }
    spec = None
    answer = ask(f"导入会话记录？输入“格式 路径”（格式：{', '.join(transcript.CONVERTERS)}），留空跳过：")
    if answer:
        fmt, _, path = answer.partition(" ")
        spec = (fmt, path.strip())
    cost = number(ask("费用（美元，留空跳过）："), float)
    tin = number(ask("输入 token（留空跳过）："), int)
    tout = number(ask("输出 token（留空跳过）："), int)
    finish_usage(out, run, spec, cost, tin, tout)
    (out / "run.json").write_text(json.dumps(run, indent=2, ensure_ascii=False))
    log(json.dumps({k: run[k] for k in ("end_reason", "elapsed_min", "resumes", "usage")}, ensure_ascii=False))
    log(f"score it: python3 {HERE.parent / 'eval/railway_eval.py'} snapshots --season <season> "
        f"--snapshots {out / 'snapshots'} --out {out / 'results'}")


def cmd_attach(a):
    out = Path(a.out).resolve()
    run = json.loads((out / "run.json").read_text())
    finish_usage(out, run, a.transcript, a.cost, a.input_tokens, a.output_tokens)
    (out / "run.json").write_text(json.dumps(run, indent=2, ensure_ascii=False))
    log(f"updated {out / 'run.json'}: usage {run.get('usage')}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="run the task with a harness driven by you")
    p.add_argument("--workspace", required=True, help="packaged workspace: directory or .tar.gz")
    p.add_argument("--out", required=True, help="run directory (must be empty)")
    p.add_argument("--tool", required=True, help=f"harness: {', '.join(taskinfo.TOOLS)}")
    p.add_argument("--tool-unlisted", action="store_true", help="accept a --tool name that is not in the list")
    p.add_argument("--tool-version")
    p.add_argument("--model", required=True, help="provider/model, as the harness names it")
    p.add_argument("--variant", help="reasoning effort or similar, e.g. high")
    p.add_argument("--minutes", type=float, default=150)
    p.add_argument("--snapshot-every", type=float, default=30)
    p.add_argument("--strict", action="store_true", help="official mode: dedicated account + network guard")
    p.add_argument("--user", default="llmagent")
    p.add_argument("--on-deadline", help="shell command to run when the run ends (trial mode)")
    p.set_defaults(func=cmd_run)
    p = sub.add_parser("attach", help="add a session log or usage to a finished run")
    p.add_argument("--out", required=True)
    p.add_argument("--transcript", nargs=2, metavar=("FORMAT", "FILE"))
    p.add_argument("--cost", type=float)
    p.add_argument("--input-tokens", type=int)
    p.add_argument("--output-tokens", type=int)
    p.set_defaults(func=cmd_attach)
    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
