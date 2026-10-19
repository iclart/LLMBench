#!/usr/bin/env python3
"""ABI 2 deterministic intent generator; actual order IDs stay runtime-local."""
import argparse
from pathlib import Path
import random
from model import load_world

LIVE = 18446744073709551615


def generate(world, seed, scenario, count):
    rng = random.Random(seed)
    trains = world["trains"]
    hot = list(range(min(12, len(trains))))
    at = {}
    for t, stops in enumerate(trains):
        for i, stop in enumerate(stops[:-1]):
            at.setdefault(stop[0], []).append((t,i))
    def leg(hot_request=False):
        t = rng.choice(hot) if hot_request else rng.randrange(len(trains))
        stops = trains[t]
        a = rng.randrange(len(stops) - 1)
        b = rng.randrange(a + 1, len(stops))
        return t, rng.randrange(3) if not hot_request else 0, stops[a][0], stops[b][0]
    def book(hot_request=False):
        route = [leg(hot_request)]
        nodes={route[0][2],route[0][3]}
        # Include real multi-leg writes and cross-day rollback pressure;
        # building this intent uses only the static timetable.
        for _ in range(rng.choice((0,0,0,1,2))):
            t,day,origin,destination=route[-1]
            ready=day*1440+next(s[1] for s in trains[t] if s[0]==destination)+30
            options=at.get(destination,())
            for attempt in range(min(24,len(options))):
                nt,a=rng.choice(options)
                nd=max(0,(ready-trains[nt][a][2]+1439)//1440)
                if nd>=3 or any((nt,nd)==r[:2] for r in route): continue
                b=rng.randrange(a+1,len(trains[nt]))
                end=trains[nt][b][0]
                if end in nodes: continue
                route.append((nt,nd,destination,end));nodes.add(end);break
            else: break
        cls = 0 if hot_request else rng.choice((0, 1, 2, 2, 2))
        return str(len(route))+" " + " ".join(str(v) for r in route for v in r) + f" {cls} {rng.randrange(1,6)} {rng.randrange(32)} {rng.randrange(2)}"
    prefill = []
    if scenario != "daily":
        for t in hot:
            stops = trains[t]
            if scenario=="spring":
                prefill.extend([f"B 1 {t} 0 {stops[0][0]} {stops[-1][0]} 0 1 0 0"] * 20)
            else:
                for _ in range(24):
                    a=rng.randrange(len(stops)-1);b=rng.randrange(a+1,len(stops))
                    prefill.append(f"B 1 {t} 0 {stops[a][0]} {stops[b][0]} 0 1 0 0")
    main = []
    for i in range(count):
        hot_request = scenario != "daily" and rng.randrange(1000) < 800
        roll = rng.randrange(100)
        q_end,b_end,e_end={"daily":(30,65,85),"spring":(30,60,85),"refund_wave":(25,50,70)}[scenario]
        if roll < q_end:
            if hot_request:
                _, _, origin, destination = leg(True)
            else:
                origin, destination = rng.sample(range(world["stations"]), 2)
            main.append(f"Q {origin} {destination} {rng.randrange(300,1200) if hot_request else rng.randrange(4320)} {rng.randrange(1,6)} {rng.randrange(3)} {rng.randrange(1,6)}")
        elif roll < b_end:
            main.append("B " + book(hot_request))
        elif roll < e_end:
            main.append(f"E {LIVE} {rng.choice((1,9,73,32767))} " + book(hot_request))
        else:
            main.append(f"R {LIVE} {rng.choice((1,9,73,32767))}")
    # Explicit, deterministic quiet probes check both exact top-K and capacity.
    quiet = []
    for _ in range(64):
        t, day, origin, destination = leg()
        quiet.append(f"Q {origin} {destination} {day * 1440 + 300} 5 0 1")
        quiet.append("B " + book())
    return ("RAILV2 2\nOPTIONS 0 0\nPREFILL " + str(len(prefill)) + "\n" + "\n".join(prefill) +
            "\nMAIN " + str(len(main)) + "\n" + "\n".join(main) + "\nQUIET " + str(len(quiet)) +
            "\n" + "\n".join(quiet) + "\nEND\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("world", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--scenario", choices=("daily", "spring", "refund_wave"), required=True)
    parser.add_argument("--seed", type=int, default=20261018)
    parser.add_argument("--count", type=int, default=100000)
    a = parser.parse_args()
    if a.count < 1:
        parser.error("count must be positive")
    a.out.write_text(generate(load_world(a.world), a.seed, a.scenario, a.count), newline="\n")


if __name__ == "__main__":
    main()
