"""Real eight-call exchange/refund/query histories, checked exhaustively."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"v2/tools"))
from model import load_world
from judge import check

p=argparse.ArgumentParser();p.add_argument("--engine",action="append",type=Path,required=True);args=p.parse_args()
world_text="""RAILWORLD 2
STATIONS 2
A
B
LINES 1
L 2 0 1
TRAINS 2
T0 1 0 2 0 360 360 1 400 400
T1 1 0 2 0 360 360 1 400 400
END
"""
prefill=["B 1 1 0 0 1 0 5 0 0"]+["B 1 1 0 0 1 0 1 0 0"]*19+["B 1 0 0 0 1 0 1 0 0"]*23
main=["E 0 1 1 0 0 0 1 0 1 0 0", "Q 0 1 360 2 0 1", "R 0 1", "R 0 8",
      "E 0 64 1 0 0 0 1 1 1 0 0", "R 0 64", "E 0 4608 1 0 0 0 1 1 2 0 0", "R 0 4096"]
with tempfile.TemporaryDirectory() as directory:
    root=Path(directory);world=root/"world.world";world.write_text(world_text);parsed=load_world(world)
    load=root/"test.load"
    load.write_text("RAILV2 2\nOPTIONS 0 0\nPREFILL 43\n"+"\n".join(prefill)+"\nMAIN 8\n"+"\n".join(main)+"\nQUIET 1\nQ 0 1 360 2 0 1\nEND\n")
    for engine in args.engine:
        for threads in (1,4,16):
            for repeat in range(4):
                trace=root/"trace.jsonl"
                subprocess.run([str(engine.resolve()),str(world),str(load),str(threads),str(trace)],check=True,capture_output=True)
                rows=[json.loads(line) for line in trace.read_text().splitlines()]
                verdict=check(parsed,rows[0],rows[1:])
                if not verdict["ok"] or not verdict["complete_linearizability_proof"]:
                    raise AssertionError((str(engine),threads,repeat,verdict))
print(f"{len(args.engine)*12} complete observed histories passed (8 concurrent calls each)")

# The short Windows pilot exposed two useful specification regressions:
# malformed replacement passenger counts take precedence over unknown orders,
# and transfer-arrival canonicalization is not the global query tie breaker.
tie_world="""RAILWORLD 2
STATIONS 4
A
B
C
D
LINES 1
L 4 0 1 2 3
TRAINS 4
T0 1 0 2 0 360 360 1 400 400
T1 1 0 2 1 450 450 3 500 500
T2 1 0 2 0 360 360 2 390 390
T3 1 0 2 2 450 450 3 500 500
END
"""
with tempfile.TemporaryDirectory() as directory:
    root=Path(directory);world=root/'world.world';world.write_text(tie_world);parsed=load_world(world)
    load=root/'test.load'
    load.write_text('RAILV2 2\nOPTIONS 0 0\nPREFILL 0\nMAIN 1\nE 0 73 1 0 0 0 1 0 1 0 0\nQUIET 1\nQ 0 3 360 2 0 1\nEND\n')
    for engine in args.engine:
        trace=root/'trace.jsonl'
        subprocess.run([str(engine.resolve()),str(world),str(load),'1',str(trace)],check=True,capture_output=True)
        rows=[json.loads(line) for line in trace.read_text().splitlines()]
        assert rows[1]['status']==2, ('malformed exchange must be Invalid before order lookup',rows[1])
        assert [p['legs'][0][0] for p in rows[2]['result']]==[0,2], ('global sequence tie breaker',rows[2])
        verdict=check(parsed,rows[0],rows[1:])
        if not verdict['ok'] or not verdict['complete_linearizability_proof']:
            raise AssertionError((str(engine),'semantic regressions',verdict))
print(f'{len(args.engine)*2} semantic assertions passed (status precedence and query tie ordering)')
