"""A full train followed by SoldOut within one coarse clock tick is valid."""
import argparse
import json
from pathlib import Path
import struct
import subprocess


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--judge", required=True)
    p.add_argument("--work", type=Path, required=True)
    a = p.parse_args(); a.work.mkdir(parents=True, exist_ok=True)
    world, load, answers, log, result = [a.work / n for n in ("clock.world", "clock.load", "clock.answers", "clock.log", "clock.json")]
    world.write_text("RAILWORLD 2\nSTATIONS 2\nA\nB\nLINES 1\nL 2 0 1\nTRAINS 1\nT 1 0 2 0 360 360 1 400 400\nEND\n")
    load.write_text("RAILLOAD 1\nSCENARIO daily\nINTENTS 0\nQUIET 0\nEND\n")
    answers.write_text("")
    header = struct.pack("<8sII9Q", b"RAILLOG1", 1, 1, 1, 0, 0, 0, 0, 0, 25, 24, 0)
    events, seats = [], []
    for i in range(24):
        events.append(struct.pack("<QIIQIBBBB", 100, 0, 0, i + 1, i, 1, 0, 1, 2))
        seats.append(bytes((1, i // 3 + 1, ord("ACF"[i % 3]))) + bytes(42))
    events.append(struct.pack("<QIIQIBBBB", 100, 0, 0, 0, 0xffffffff, 1, 1, 1, 2))
    log.write_bytes(header + b"".join(events) + b"".join(seats) + struct.pack("<5I", 0, 0, 0, 1, 0))
    run = subprocess.run([a.judge, str(world), str(load), str(answers), str(log), "--json", str(result)], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr + result.read_text()
    assert json.loads(result.read_text())["ok"]
    print("same-tick full-capacity audit accepted")


if __name__ == "__main__":
    main()
