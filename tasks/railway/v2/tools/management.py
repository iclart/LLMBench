#!/usr/bin/env python3
"""Reproducible management research, NOT an enabled scoring season.

The private online policy is a callback receiving only the current request,
current inventory, past requests and public lambda. No future requests.
"""
import argparse
import hashlib
from collections import Counter
from fractions import Fraction
import heapq
import importlib.util
import json
import math
from pathlib import Path
import random
import statistics

FAMILIES = ("connecting_shorts", "overlapping_bottlenecks", "changing_demand")
DURATIONS = (15, 20, 65, 10, 50, 30, 80, 20)


def ride_minutes(durations, a, b):
    # Inter-arrival intervals; intermediate stops dwell one minute. This is
    # exactly arrive[b]-depart[a] in export_episode(), not a surrogate score.
    return sum(durations[a:b]) - int(a > 0)


def generate(family, seed, capacity=24):
    rng = random.Random(seed)
    requests = []
    for train in range(3):
        if family == "connecting_shorts":
            types = [(0, 1, 4), (1, 2, 4), (5, 6, 3), (0, 5, .65), (0, 8, .45)]
        elif family == "overlapping_bottlenecks":
            types = [(0, 2, 3), (2, 3, 3), (4, 5, 3), (5, 6, 3), (0, 6, .4), (2, 8, .5), (0, 8, .2), (3, 7, .65)]
        else:
            types = [(0, 1, 4), (1, 2, 4), (5, 6, 3), (0, 5, .55), (0, 8, .6), (2, 7, .3)]
        for a, b, pressure in types:
            count = round(capacity * pressure * rng.uniform(.55, 1.45))
            requests.extend([(train, a, b)] * count)
    rng.shuffle(requests)
    if family == "changing_demand":
        # Overlapping arrival distributions, rather than a deterministic
        # all-short then all-long cliff that can be solved by one cutoff.
        requests.sort(key=lambda r: rng.random() + (.55 if r[2] - r[1] >= 5 else 0))
    return {"family": family, "capacity": capacity, "durations": DURATIONS, "trains": 3,
            "requests": requests}


def offline(episode, weight, occupied=None):
    """Exact maximum-weight interval scheduling on C seats via min-cost flow.

Duplicate intervals are aggregated. Integer tie-breaking prefers more people
at the same utility, without ever changing the primary objective.
"""
    weight = Fraction(weight)
    passengers = transport = 0
    for train in range(episode["trains"]):
        requests = Counter((a, b) for t, a, b in episode["requests"] if t == train)
        n = len(episode["durations"]) + 1
        graph = [[] for _ in range(n)]
        def edge(a, b, capacity, cost):
            forward = [b, capacity, cost, len(graph[b])]
            reverse = [a, 0, -cost, len(graph[a])]
            graph[a].append(forward); graph[b].append(reverse)
            return forward
        for i in range(n - 1):
            edge(i, i + 1, episode["capacity"], 0)
        links = []
        tie = sum(requests.values()) + 1
        for (a, b), count in requests.items():
            minutes = ride_minutes(episode["durations"],a,b)
            value = minutes * weight.denominator + weight.numerator
            link = edge(a, b, count, -(value * tie + 1))
            links.append((count, minutes, link))
        forced = []
        if occupied is not None:
            masks = occupied[train]
            intervals = Counter()
            for mask in masks:
                segment = 0
                while segment < n - 1:
                    if not mask >> segment & 1:
                        segment += 1; continue
                    start = segment
                    while segment < n - 1 and mask >> segment & 1:
                        segment += 1
                    intervals[start, segment] += 1
            reward = sum(count * (ride_minutes(episode["durations"],a,b) * weight.denominator + weight.numerator)
                         for (a,b),count in requests.items()) + 1
            for (a,b),count in intervals.items():
                forced.append((count, edge(a,b,count,-reward*tie)))
        for _ in range(episode["capacity"]):
            # Small DAG/residual graphs: Bellman-Ford is simple and exact,
            # including negative request costs and reverse edges.
            dist, prev = [math.inf] * n, [None] * n
            dist[0] = 0
            for _ in range(n - 1):
                changed = False
                for a in range(n):
                    for i, (b, cap, cost, _) in enumerate(graph[a]):
                        if cap and dist[a] + cost < dist[b]:
                            dist[b], prev[b], changed = dist[a] + cost, (a, i), True
                if not changed:
                    break
            at = n - 1
            while at:
                a, i = prev[at]
                forward = graph[a][i]; forward[1] -= 1
                graph[at][forward[3]][1] += 1
                at = a
        for original, minutes, link in links:
            served = original - link[1]
            passengers += served; transport += served * minutes
        if any(link[1] for _,link in forced):
            raise RuntimeError("infeasible occupied-seat reservation in offline bound")
    return {"people": passengers, "transport": transport, "utility": float(transport + weight * passengers)}


def frontier(episode):
    left = offline(episode, 0)
    right = offline(episode, sum(ride_minutes(episode["durations"],a,b) for _, a, b in episode["requests"]) + 1)
    points, slopes = {(left["people"], left["transport"]), (right["people"], right["transport"])}, set()
    def refine(a, b):
        if b["people"] == a["people"]:
            return
        slope = Fraction(a["transport"] - b["transport"], b["people"] - a["people"])
        if slope > 0:
            slopes.add(slope)
        middle = offline(episode, slope)
        key = (middle["people"], middle["transport"])
        if key not in points:
            points.add(key); refine(a, middle); refine(middle, b)
    refine(left, right)
    return sorted(points), sorted(slopes)


def simulate(episode, weight, policy, parameter=None):
    inventory = [[0] * episode["capacity"] for _ in range(episode["trains"])]
    past = {"count": 0, "segments": 0, "offered": [[0] * len(DURATIONS) for _ in inventory], "intervals": Counter()}
    passengers = transport = 0
    for request in episode["requests"]:
        train, a, b = request
        mask = ((1 << b) - 1) ^ ((1 << a) - 1)
        seats = inventory[train]
        free = [i for i, sold in enumerate(seats) if not sold & mask]
        minutes = ride_minutes(episode["durations"],a,b)
        accept = bool(free)
        if accept and policy == "length":
            accept = minutes >= parameter
        elif accept and policy == "reserve":
            remaining = min(sum(not sold & 1 << segment for sold in seats) for segment in range(a, b))
            accept = minutes >= sum(DURATIONS) * .6 or remaining > parameter * len(seats)
        elif accept and callable(policy):
            # The episode's future request list is deliberately NOT passed.
            accept = bool(policy(request, tuple(seats), past, weight, DURATIONS, parameter))
        if accept:
            # Same packing for every policy: only the admission decision differs.
            chosen = max(free, key=lambda i: (inventory[train][i].bit_count(), -i))
            seats[chosen] |= mask; passengers += 1; transport += minutes
        past["count"] += 1; past["segments"] += b - a
        past["intervals"][request] += 1
        for segment in range(a, b):
            past["offered"][train][segment] += 1
    return {"people": passengers, "transport": transport, "utility": transport + weight * passengers}


def bootstrap_lower(values, seed=9981):
    rng = random.Random(seed)
    samples = sorted(statistics.mean(rng.choices(values, k=len(values))) for _ in range(2000))
    return samples[49]


def study(policy, parameters, train_seeds=range(10), validation_seeds=range(10000, 10030), prepare_policy=None):
    result = {"schema_version": 2, "management_enabled": False, "families": {},
              "training_seeds": list(train_seeds), "validation_seeds": list(validation_seeds), "study_revision": 2}
    simple_grid = [("greedy", None)] + [("length", n) for n in (15, 30, 60, 90, 120, 180, 240)] + [("reserve", n) for n in (.1, .25, .4, .6, .8)]
    training_sets = {family:[generate(family,seed) for seed in train_seeds] for family in FAMILIES}
    mixed_training = [episode for family in FAMILIES for episode in training_sets[family]]
    if prepare_policy:
        prepare_policy(mixed_training)
    slopes = sorted(s for episode in mixed_training for s in frontier(episode)[1] if s > 0)
    weights = sorted(set((0, math.ceil(slopes[len(slopes)//2]), math.ceil(max(slopes))+1))) if slopes else [0]
    # One reference and one best simple policy per PUBLIC lambda, trained on
    # the entire mixture. No hidden scenario identity is given to a policy.
    fitted = {weight:(max(simple_grid,key=lambda p:sum(simulate(e,weight,*p)["utility"] for e in mixed_training)),
                      max(parameters,key=lambda p:sum(simulate(e,weight,policy,p)["utility"] for e in mixed_training)))
              for weight in weights}
    result["reference_information"] = "training_mixture_and_public_lambda_only"
    passed = 0
    for family in FAMILIES:
        entries = []
        for weight in weights:
            simple, param = fitted[weight]
            rows = []
            for seed in validation_seeds:
                episode = generate(family, seed)
                rows.append({"seed": seed, "greedy": simulate(episode, weight, "greedy"),
                             "simple": simulate(episode, weight, *simple), "online": simulate(episode, weight, policy, param),
                             "offline": offline(episode, weight)})
            against_greedy = [r["online"]["utility"] / r["greedy"]["utility"] - 1 for r in rows]
            against_simple = [r["online"]["utility"] / r["simple"]["utility"] - 1 for r in rows]
            entries.append({"lambda": weight, "simple": simple, "parameter": param, "rows": rows,
                            "gain_greedy": statistics.mean(against_greedy), "gain_simple": statistics.mean(against_simple),
                            "paired_ci_lower": bootstrap_lower(against_simple),
                            "win_fraction": statistics.mean(v > 0 for v in against_simple)})
        people = [statistics.mean(r["offline"]["people"] for r in entry["rows"]) for entry in entries]
        distinct = len(entries) == 3 and min(abs(a-b) for i,a in enumerate(people) for b in people[i+1:]) >= .05 * max(people)
        paired = [statistics.mean(e["rows"][i]["online"]["utility"] / e["rows"][i]["simple"]["utility"] - 1 for e in entries)
                  for i in range(len(list(validation_seeds)))]
        ok = (distinct and statistics.mean(e["gain_greedy"] for e in entries) >= .10 and
              statistics.mean(e["gain_simple"] for e in entries) >= .05 and
              all(e["gain_greedy"] >= 0 and e["gain_simple"] >= 0 for e in entries) and
              statistics.mean(v > 0 for v in paired) >= .8 and bootstrap_lower(paired) > 0)
        result["families"][family] = {"passed": ok, "distinct_tradeoffs": distinct, "entries": entries,
                                      "paired_ci_lower": bootstrap_lower(paired), "paired_win_fraction": statistics.mean(v > 0 for v in paired)}
        passed += ok
    result["gate_passed"] = passed >= 2
    # Research evidence alone does not publish/enable a scoring season.
    result["passing_families"] = passed
    return result


def export_episode(episode, weight, directory):
    if episode["capacity"]!=24:
        raise ValueError("C++ business-class export requires exactly 24 seats")
    directory.mkdir(parents=True,exist_ok=False)
    stations=len(episode["durations"])+1
    lines=["RAILWORLD 2",f"STATIONS {stations}",*[f"S{i}" for i in range(stations)],"LINES 1",
           "M "+str(stations)+" "+" ".join(map(str,range(stations))),f"TRAINS {episode['trains']}"]
    for train in range(episode["trains"]):
        stops=[];arrive=360
        for station in range(stations):
            stops.extend((station,arrive,arrive+int(0<station<stations-1)))
            if station<stations-1:arrive+=episode["durations"][station]
        lines.append(f"M{train} 1 0 {stations} "+" ".join(map(str,stops)))
    (directory/"world.world").write_text("\n".join([*lines,"END",""]),newline="\n")
    commands=[f"B 1 {t} 0 {a} {b} 0 1 0 0" for t,a,b in episode["requests"]]
    (directory/"management.load").write_text(f"RAILV2 2\nOPTIONS 1 {weight}\nPREFILL 0\nMAIN {len(commands)}\n"+
                                           "\n".join(commands)+"\nQUIET 0\nEND\n",newline="\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location("private_management_policy", args.policy)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    result = study(module.accept, module.PARAMETERS, prepare_policy=getattr(module,"prepare",None))
    result["policy_sha256"]=hashlib.sha256(args.policy.read_bytes()).hexdigest()
    result["research_source_sha256"]=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2))
    print(json.dumps({"gate_passed": result["gate_passed"], "passing_families": result["passing_families"],
                      "families": {name: {"passed": f["passed"], "distinct_tradeoffs": f["distinct_tradeoffs"],
                                           "gains": [(e["lambda"], e["gain_greedy"], e["gain_simple"]) for e in f["entries"]]}
                                   for name, f in result["families"].items()}}, indent=2))


if __name__ == "__main__":
    main()
