#!/usr/bin/env python3
"""Layered ABI 2 judge: exact short histories; conservative large histories.

Unresolved concurrent top-K correctness is explicitly reported, never
misrepresented as a complete linearizability proof.
"""
import argparse
import json
import math
from pathlib import Path
from model import Model, linearize, load_world, OK, INVALID, SOLD_OUT, WITHHELD
from model import LETTERS
from audit import audit


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def preferences(events):
    window_wanted = window_got = together_wanted = together_got = 0
    for event in events:
        if event["phase"] != "QUIET" or event["op"] not in ("B", "E") or event["status"] != OK:
            continue
        request = event["request"] if event["op"] == "B" else event["request"]["replacement"]
        cls, pax = request["class"], request["pax"]
        if not 0 <= cls < 3:
            continue
        for seats in event.get("seats", []):
            for p, seat in enumerate(seats):
                if request.get("window", 0) >> p & 1:
                    window_wanted += 1; window_got += seat[2] in (65, 70)
            if not request.get("together") or pax < 2:
                continue
            together_wanted += 1
            same_car = len({s[0] for s in seats}) == 1
            width = len(LETTERS[cls]); block = (width + 1) // 2
            if not same_car or not seats or any(s[2] not in map(ord, LETTERS[cls]) for s in seats):
                continue
            if pax > width:
                satisfied = max(s[1] for s in seats) - min(s[1] for s in seats) <= 1
            elif len({s[1] for s in seats}) != 1:
                satisfied = False
            elif pax > block:
                satisfied = True
            else:
                columns = [LETTERS[cls].index(chr(s[2])) for s in seats]
                aisle = (1, 2, 3)[cls]
                satisfied = len({c < aisle for c in columns}) == 1 and max(columns) - min(columns) == pax - 1
            together_got += satisfied
    return {"window_wanted": window_wanted, "window_got": window_got,
            "window_rate": window_got/window_wanted if window_wanted else 1.,
            "together_wanted": together_wanted, "together_got": together_got,
            "together_rate": together_got/together_wanted if together_wanted else 1.}


def check(world, header, events):
    if header.get("schema_version") != 2 or header.get("abi_version") != 2 or header.get("calls") != len(events):
        raise ValueError("invalid ABI 2 log header/count")
    if not 1 <= header["threads"] <= 256 or header["mode"] not in (0,1):
        raise ValueError("invalid execution mode/thread count")
    if sorted(e["index"] for e in events) != list(range(len(events))):
        raise ValueError("missing/duplicate event index")
    if any(e["start"] > e["end"] or e["start"] < 0 or e["status"] not in range(5) for e in events):
        raise ValueError("invalid call window/status")
    if any(e["phase"] not in ("PREFILL","MAIN","QUIET") or e["op"] not in ("Q","B","E","R") for e in events):
        raise ValueError("unknown event phase/operation")
    if header["init_ns"] > 30_000_000_000:
        return {"ok": False, "errors": ["init exceeds 30 seconds"]}
    model = Model(world, management=header["mode"] == 1)
    errors, uncertain, exact = [], 0, 0
    chronological = sorted(events, key=lambda e: (e["start"], e["end"], e["index"]))
    # Overlap components are separated by a genuine quiescent point.
    groups, group, end = [], [], -1
    for event in chronological:
        if header["threads"] == 1 or event["phase"] != "MAIN":
            if group:
                groups.append(group);group=[]
            groups.append([event]);end=-1
            continue
        if group and event["start"] > end:
            groups.append(group); group = []
        group.append(event); end = max(end, event["end"])
    if group:
        groups.append(group)
    state_known = True
    inventory_audits = []
    for group in groups:
        if len(group) == 1 and state_known:
            if not model.apply(group[0]):
                errors.append({"kind":"sequential_model","events":[group[0]["index"]]})
                break
            exact += 1
        elif len(group) <= 8 and state_known:
            next_model, _ = linearize(model, group)
            if next_model is None:
                errors.append({"kind": "linearizability", "events": [e["index"] for e in group]})
                break
            model = next_model; exact += len(group)
        else:
            # Large components: process observed writes in return order when
            # possible; uncertainty is retained if no complete order is proven.
            # A later quiet phase does not magically restore a known state.
            uncertain += len(group)
            model, found_errors, coverage = audit(model, group)
            errors.extend(found_errors); inventory_audits.append(coverage)
            if found_errors:
                break
    latency = {}
    for op, name in (("Q", "query"), ("B", "book"), ("E", "exchange"), ("R", "refund")):
        calls=[e for e in events if e["phase"]=="MAIN" and e["op"]==op]
        usable=[e for e in calls if e["status"]!=INVALID and (op not in ("E","R") or (e["request"]["order"]>0 and e["request"]["cells"]>0))]
        values = [e["end"] - e["start"] for e in usable]
        latency[name] = {"count": len(values), "p50_ns": percentile(values, .5), "p99_ns": percentile(values, .99),
                         "sample_sufficient": len(values) >= 10000,"total_calls":len(calls),"successful_calls":sum(e["status"]==OK for e in usable)}
    main_count = sum(e["phase"] == "MAIN" for e in events)
    trades=[e for e in events if e["phase"]=="MAIN" and e["op"] in ("B","E") and e["status"] in (OK,SOLD_OUT)]
    quality={"eligible_trades":len(trades),"accepted_trades":sum(e["status"]==OK for e in trades),
             "satisfaction":sum(e["status"]==OK for e in trades)/len(trades) if trades else 1.}
    return {"schema_version": 2, "ok": not errors, "errors": errors, "exact_calls": exact,
            "unresolved_calls": uncertain, "complete_linearizability_proof": uncertain == 0 and not errors,
            "large_inventory_audit_complete": not errors, "inventory_audits": inventory_audits,
            "throughput": main_count * 1e9 / max(header["main_ns"], 1), "latency": latency, "quiet": preferences(events),"quality":quality}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("world", type=Path)
    parser.add_argument("log", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    with args.log.open() as source:
        header = json.loads(next(source)); events = [json.loads(line) for line in source]
    result = check(load_world(args.world), header, events)
    args.out.write_text(json.dumps(result, indent=2))
    print(json.dumps(result))
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
