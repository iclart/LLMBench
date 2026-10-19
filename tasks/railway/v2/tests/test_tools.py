import argparse
import copy
from itertools import combinations
import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
from model import Model, linearize, load_world
from judge import check
from management import offline, frontier, generate as market, simulate, ride_minutes, export_episode
from generate import generate
import scoring

WORLD = """RAILWORLD 2
STATIONS 4
A
B
C
D
LINES 1
L 4 0 1 2 3
TRAINS 2
T0 1 0 3 0 360 360 1 400 402 2 430 430
T1 1 0 3 1 440 440 2 480 482 3 520 520
END
"""
BINARIES = []


class ToolsTest(unittest.TestCase):
    def test_query_pruning_matches_unbounded_enumeration(self):
        rng=random.Random(20261018)
        for seed in range(16):
            trains=[]
            for t in range(5):
                stations=rng.sample(range(5),4);minute=rng.randrange(1200,1500);stops=[]
                for station in stations:
                    minute+=rng.randrange(20,80);stops.append((station,minute,minute+2))
                trains.append(stops)
            model=Model({"stations":5,"trains":trains})
            # Some train-days have 0, 1, 3 or 24 free business seats.
            for t in range(5):
                for d in range(3):
                    for s in range(rng.choice((0,21,23,24))):
                        a=rng.randrange(3);b=rng.randrange(a+1,4)
                        model.holds[t,d,0,s]=[(a,b,1,0,0)]
            origin,destination=rng.sample(range(5),2);earliest=rng.randrange(1200,1440);pax=rng.randrange(1,6)
            found={}
            def visit(path,station,ready,nodes):
                for t,stops in enumerate(trains):
                    for a,stop in enumerate(stops[:-1]):
                        if stop[0]!=station:continue
                        for d in range(3):
                            depart=d*1440+stop[2]
                            if depart<ready or (not path and depart>=1440) or any(l[:2]==[t,d] for l in path):continue
                            for b in range(a+1,4):
                                target=stops[b][0]
                                if target in nodes or not model.available((t,d,a,b),0,pax):continue
                                leg=[t,d,station,target,depart,d*1440+stops[b][1]];new=path+[leg]
                                if target==destination:
                                    key=tuple(tuple(l[:2]) for l in new)
                                    arrivals=lambda p:tuple(l[5] for l in p[:-1])
                                    if key not in found or arrivals(new)<arrivals(found[key]):found[key]=new
                                elif len(new)<3:visit(new,target,leg[5]+30,nodes|{target})
            visit([],origin,earliest,{origin})
            ordered=sorted(found.values(),key=lambda p:(len(p),p[-1][5],-p[0][4],tuple(tuple(l[:2]) for l in p)))
            for k in range(1,6):self.assertEqual(model.query([origin,destination,earliest,k,0,pax]),(0,ordered[:k]),(seed,k))

    def test_generator_has_distinct_scenarios_and_valid_transactions(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"w";path.write_text(WORLD);world=load_world(path)
        loads={s:generate(world,123,s,1000) for s in ("daily","spring","refund_wave")}
        self.assertEqual(len(set(loads.values())),3)
        self.assertEqual(loads["spring"],generate(world,123,"spring",1000))
        model=Model(world)
        multi=0
        for load in loads.values():
            for line in load.splitlines():
                parts=line.split()
                if not parts or parts[0] not in ("B","E"):continue
                nums=list(map(int,parts[3:] if parts[0]=="E" else parts[1:]));n=nums[0]
                request={"legs":[nums[1+4*i:5+4*i] for i in range(n)],"class":nums[1+4*n],"pax":nums[2+4*n]}
                self.assertIsNotNone(model.validate_book(request),line)
                multi+=n>1
        self.assertGreater(multi,10)

    def test_four_latency_scores_and_publication_guards(self):
        one={"throughput":1000,"latency":{op:{"p99_ns":100,"sample_sufficient":True} for op in scoring.OPS}}
        self.assertAlmostEqual(scoring.measurement_score(one,one,one)["score"],100)
        slow=copy.deepcopy(one);slow["latency"]["exchange"]["p99_ns"]=400
        result=scoring.measurement_score(slow,one,one)
        self.assertLess(result["score"],100)
        self.assertEqual(result["latency_by_op"]["query"],100)
        slow["latency"]["refund"]["sample_sufficient"]=False
        self.assertIsNone(scoring.measurement_score(slow,one,one)["score"])
        self.assertIsNone(scoring.management_score(150,100,200,False))
        self.assertEqual(scoring.management_score(300,100,200,True),100)
        self.assertFalse(scoring.release_gate({"correctness":True})["publishable"])
        runs=[{"id":i,"model":"opencode-go/deepseek-v4.1-flash","variant":"max","tool_version":"2.0.32","minutes":105,"correctness_passed":i!=0,"performance":70+i} for i in range(3)]
        self.assertTrue(scoring.pilot_gate(runs)["passed"])
        runs[0]["tool_version"]="2.0.33"
        self.assertFalse(scoring.pilot_gate(runs)["passed"])

    def test_scores_require_compatible_anchors_and_all_gates(self):
        one={"ok":True,"throughput":1000,"latency":{op:{"p99_ns":100,"sample_sufficient":True} for op in scoring.OPS},
             "quiet":{"window_rate":1.,"together_rate":1.},"quality":{"satisfaction":1.},"engine_mb":10.}
        result={"abi_version":2,"task_version":"frozen","inputs_sha256":"same-inputs","platform":"unit-test","cpus":[0,1,2,3],
                "compile_flags":["-O2"],"correctness_passed":True,"functional":{"categories":{"a":{"passed":1,"total":1}}},
                "measurements":{f"{s}/t{t}":copy.deepcopy(one) for s in ("daily","spring","refund_wave") for t in (1,4,16)}}
        trial=scoring.score(result,result,result)
        self.assertIsNone(trial["total"])
        self.assertEqual(trial["performance"],100)
        business={"evidence_passed":True,"rows":[{"family":f,"lambda":l,"utility":200,"greedy":100,"reference":200} for f in ("one","two") for l in (0,10,100)]}
        checks={k:True for k in ("asan_ubsan","tsan","cross_platform_generator","pilots")}
        self.assertEqual(scoring.score(result,result,result,business,checks)["total"],100)
        changed=copy.deepcopy(result);changed["platform"]="other"
        with self.assertRaises(ValueError):scoring.score(changed,result,result)
        changed=copy.deepcopy(result)
        for value in changed["measurements"].values():value["engine_mb"]=40.
        self.assertEqual(scoring.score(changed,result,result)["memory"],0)
        changed["measurements"]["daily/t1"]["quality"]["satisfaction"]=.8
        self.assertEqual(scoring.score(changed,result,result)["measurements"]["daily/t4"]["score"],-100)

    def test_model_clone_does_not_publish_failed_search_branches(self):
        world={"stations":2,"trains":[[(0,360,360),(1,400,400)]]}
        original=Model(world)
        event={"op":"B","request":{"legs":[[0,0,0,1]],"class":0,"pax":1},"status":0,"order":1,"seats":[[[1,1,65]]]}
        self.assertTrue(original.apply(event))
        branch=original.clone()
        self.assertTrue(branch.apply({"op":"R","request":{"order":1,"cells":1},"status":0}))
        self.assertEqual(original.orders[1]["active"],1)
        self.assertTrue(any(original.holds.values()))

    def test_program_order_survives_equal_clock_ticks(self):
        world={"stations":2,"trains":[[(0,360,360),(1,400,400)]]}
        book={"op":"B","request":{"legs":[[0,0,0,1]],"class":0,"pax":1},"status":0,"order":1,
              "seats":[[[1,1,65]]],"start":10,"end":10,"index":0,"thread":1}
        bad={"op":"R","request":{"order":1,"cells":1},"status":3,"start":10,"end":10,"index":1,"thread":1}
        self.assertIsNone(linearize(Model(world),[book,bad])[0])
        bad["thread"]=2
        self.assertIsNotNone(linearize(Model(world),[book,bad])[0])

    def test_interval_flow_matches_bruteforce(self):
        rng = random.Random(39)
        for _ in range(30):
            requests = []
            for _ in range(8):
                a = rng.randrange(3); requests.append((0, a, rng.randrange(a + 1, 4)))
            episode = {"trains": 1, "capacity": 2, "durations": (2, 5, 3), "requests": requests}
            for weight in (0, 3, 9):
                best = 0
                for mask in range(1 << len(requests)):
                    selected = [r for i, r in enumerate(requests) if mask >> i & 1]
                    if all(sum(a <= segment < b for _, a, b in selected) <= 2 for segment in range(3)):
                        best = max(best, sum(ride_minutes(episode["durations"],a,b) + weight for _, a, b in selected))
                self.assertEqual(offline(episode, weight)["utility"], best)

    def test_origin_only_can_be_degenerate(self):
        episode = {"trains": 1, "capacity": 2, "durations": (1, 9),
                   "requests": [(0, 0, 1)] * 8 + [(0, 0, 2)] * 8}
        points, _ = frontier(episode)
        self.assertEqual(points, [(2, 20)])

    def test_mixed_snapshot_rejected(self):
        world = {"stations": 2, "trains": [[(0, 360, 360), (1, 400, 400)]] * 2}
        model = Model(world)
        def booking(oid, train, seat):
            return {"op": "B", "request": {"legs": [[train, 0, 0, 1]], "class": 0, "pax": 1},
                    "status": 0, "order": oid, "seats": [[[1, seat // 3 + 1, ord("ACF"[seat % 3])]]]}
        for i in range(23):
            self.assertTrue(model.apply(booking(i + 1, 0, i)))
        for i in range(24):
            self.assertTrue(model.apply(booking(i + 24, 1, i)))
        exchange = {"op": "E", "request": {"order": 24, "cells": 1,
                    "replacement": {"legs": [[0, 0, 0, 1]], "class": 0, "pax": 1}},
                    "status": 0, "order": 100, "seats": [[[1, 8, ord("F")]]], "start": 10, "end": 30}
        query = {"op": "Q", "request": [0, 1, 360, 2, 0, 1], "status": 0, "count": 2,
                 "result": [{"count": 1, "legs": [[t, 0, 0, 1, 360, 400]]} for t in (0, 1)], "start": 11, "end": 29}
        result, _ = linearize(model, [exchange, query])
        self.assertIsNone(result)
        query["count"] = 1; query["result"] = query["result"][:1]
        self.assertIsNotNone(linearize(model, [exchange, query])[0])

    def test_order_identity_and_failed_partial_are_atomic(self):
        model = Model({"stations": 2, "trains": [[(0,360,360),(1,400,400)]]})
        book = {"op": "B", "request": {"legs": [[0,0,0,1]], "class": 0, "pax": 2}, "status": 0,
                "order": 1, "seats": [[[1,1,65],[1,1,67]]]}
        self.assertTrue(model.apply(book))
        self.assertTrue(model.apply({"op":"R","request":{"order":1,"cells":1},"status":0}))
        self.assertTrue(model.apply({"op":"R","request":{"order":1,"cells":9},"status":3}))
        self.assertTrue(model.apply({"op":"R","request":{"order":1,"cells":8},"status":0}))
        self.assertFalse(model.apply(book))

    def test_actual_engines_and_mutant_logs(self):
        if not BINARIES:
            self.skipTest("engine paths not supplied")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); world = root / "test.world"; world.write_text(WORLD)
            parsed = load_world(world)
            for engine in BINARIES:
                for threads in (1, 4, 16):
                    for seed in (7, 19):
                        load = root / "test.load"; load.write_text(generate(parsed, seed, "spring", 96))
                        log = root / "test.jsonl"
                        subprocess.run([str(engine), str(world), str(load), str(threads), str(log)], check=True, capture_output=True)
                        lines = [json.loads(line) for line in log.read_text().splitlines()]
                        verdict = check(parsed, lines[0], lines[1:])
                        self.assertTrue(verdict["ok"], (engine, threads, seed, verdict["errors"]))
                        # Repeated successful release must be caught even in a
                        # component too large for exhaustive linearization.
                        candidate = next((e for e in lines[1:] if e["op"] == "R" and e["status"] == 0), None)
                        if candidate:
                            mutated = copy.deepcopy(lines)
                            extra = copy.deepcopy(candidate); extra["index"] = len(mutated) - 1
                            extra["start"] = 0; extra["end"] = max(e["end"] for e in mutated[1:]) + 1
                            mutated.append(extra); mutated[0]["calls"] += 1
                            bad = check(parsed, mutated[0], mutated[1:])
                            self.assertFalse(bad["ok"], "duplicate release mutant escaped")


if __name__ == "__main__":
    p = argparse.ArgumentParser(); p.add_argument("--engine", action="append", type=Path, default=[])
    a, rest = p.parse_known_args(); BINARIES = [p.resolve() for p in a.engine]
    unittest.main(argv=[sys.argv[0], *rest])
