#!/usr/bin/env python3
"""Generates the functional test suite for a world (tools/common/suite.h).

  make_suite.py --world W --reference RAILRUN --seed N --out SUITE [--per-category N]

Query cases are sampled from random queries and sorted into categories by
the reference engine's answers (RAILRUN is a railrun binary linked against
it, e.g. railrun_strong); their expected answers are the reference's. Book and
refund cases are built from the world's timetable so that each tests one
rule, on train-days no other case touches; the runner checks them against
the rules, not against a reference. The reference must pass the whole suite.
"""
import argparse
import random
import subprocess
import sys
import tempfile
from pathlib import Path

DAYS, DAY_MIN, MIN_TRANSFER, MAX_LEGS = 3, 1440, 30, 3
SEATS = {0: 24, 1: 104, 2: 450}  # per class and train-day (TASK.md 2.3)
QUERY_CATEGORIES = ["q_direct", "q_transfer1", "q_transfer2", "q_crossline", "q_overnight", "q_none", "q_invalid"]


class World:
    def __init__(self, path):
        lines = Path(path).read_text().split("\n")
        if lines[0] != "RAILWORLD 2":
            sys.exit(f"{path}: need a RAILWORLD 2 file")
        i = 1
        n = int(lines[i].split()[1])
        self.stations = n
        i += 1 + n
        m = int(lines[i].split()[1])
        i += 1 + m
        t = int(lines[i].split()[1])
        self.trains = []
        for row in lines[i + 1:i + 1 + t]:
            p = row.split()
            c = int(p[1])
            k = int(p[2 + c])
            vals = list(map(int, p[3 + c:3 + c + 3 * k]))
            stops = [tuple(vals[3 * j:3 * j + 3]) for j in range(k)]  # (station, arrive, depart)
            self.trains.append({"id": len(self.trains), "cross": c == 2, "stops": stops})
        self.at = {}
        for tr in self.trains:
            for j, (s, _, _) in enumerate(tr["stops"]):
                self.at.setdefault(s, []).append((tr["id"], j))


class Builder:
    def __init__(self, world, rng):
        self.w, self.rng, self.used = world, rng, set()

    def train_day(self, pred=lambda tr: True, min_stops=2):
        for _ in range(10000):
            tr = self.rng.choice(self.w.trains)
            day = self.rng.randrange(DAYS)
            if len(tr["stops"]) >= min_stops and (tr["id"], day) not in self.used and pred(tr):
                self.used.add((tr["id"], day))
                return tr, day
        raise SystemExit("make_suite: ran out of train-days")

    @staticmethod
    def leg(tr, day, a, b):
        return (tr["id"], day, tr["stops"][a][0], tr["stops"][b][0])

    @staticmethod
    def book(legs, cls=0, pax=1, window=0, together=0, label=None, declared=None):
        declared = len(legs) if declared is None else declared
        s = f"B {declared} {len(legs)} " + " ".join(" ".join(map(str, l)) for l in legs) + f" {cls} {pax} {window} {together}"
        return s + (f" @{label}" if label else "")

    def fill(self, legs, cls, seats=None, label_prefix=None):
        """Books that together take exactly the class's seats on these legs."""
        left, ops, k = seats or SEATS[cls], [], 0
        while left > 0:
            pax = min(left, self.rng.randint(1, 5))
            left -= pax
            k += 1
            ops.append(self.book(legs, cls, pax, label=f"{label_prefix}{k}" if label_prefix else None))
        return ops

    def transfer_pair(self):
        """Two train-days connecting at a station with a valid transfer."""
        for _ in range(20000):
            t1 = self.rng.choice(self.w.trains)
            if len(t1["stops"]) < 2:
                continue
            j = self.rng.randrange(1, len(t1["stops"]))
            s, arr, _ = t1["stops"][j]
            for t2id, j2 in self.rng.sample(self.w.at[s], len(self.w.at[s])):
                t2 = self.w.trains[t2id]
                if t2id == t1["id"] or j2 + 1 >= len(t2["stops"]):
                    continue
                for d1 in range(DAYS):
                    for d2 in range(DAYS):
                        if d2 * DAY_MIN + t2["stops"][j2][2] < d1 * DAY_MIN + arr + MIN_TRANSFER:
                            continue
                        if (t1["id"], d1) in self.used or (t2id, d2) in self.used:
                            continue
                        # the two legs must not share any other station either
                        a = self.rng.randrange(0, j)
                        b = self.rng.randrange(j2 + 1, len(t2["stops"]))
                        st1 = {x[0] for x in t1["stops"][a:j + 1]}
                        if t2["stops"][b][0] in st1:
                            continue
                        self.used.update({(t1["id"], d1), (t2id, d2)})
                        return (t1, d1, a, j), (t2, d2, j2, b)
        raise SystemExit("make_suite: no transfer found")


def book_cases(w, rng, n):
    b = Builder(w, rng)
    cases = []

    def case(cat, ops):
        cases.append((cat, ops))

    # Malformed books: Invalid and nothing changes.
    for i in range(n):
        tr, day = b.train_day(min_stops=3)
        k = len(tr["stops"])
        good = b.leg(tr, day, 0, k - 1)
        kind = i % 10
        if kind == 0:
            op = b.book([], declared=0)
        elif kind == 1:
            op = b.book([good, good, good], declared=4)
        elif kind == 2:
            op = b.book([(len(w.trains), day, good[2], good[3])])
        elif kind == 3:
            op = b.book([(tr["id"], DAYS, good[2], good[3])])
        elif kind == 4:
            op = b.book([b.leg(tr, day, k - 1, 0)])
        elif kind == 5:
            op = b.book([good], cls=3)
        elif kind == 6:
            op = b.book([good], pax=0)
        elif kind == 7:
            op = b.book([good], pax=6)
        elif kind == 8:  # the same train-day twice
            op = b.book([b.leg(tr, day, 0, 1), b.leg(tr, day, 1, 2)])
        else:  # a station the train does not stop at
            stops = {s for s, _, _ in tr["stops"]}
            other = next(s for s in range(w.stations) if s not in stops)
            op = b.book([(tr["id"], day, good[2], other)])
        # A valid book on the same train-day afterwards still finds every seat.
        case("b_invalid", [op] + b.fill([good], 0))

    # Capacity: exactly the class's seats can be sold, no more.
    for _ in range(n):
        tr, day = b.train_day()
        k = len(tr["stops"])
        a = rng.randrange(0, k - 1)
        z = rng.randrange(a + 1, k)
        cls = rng.choice([0, 0, 1])
        leg = b.leg(tr, day, a, z)
        case("b_capacity", b.fill([leg], cls) + [b.book([leg], cls, rng.randint(1, 3))])

    # Interval tickets: a seat sold A->B can be sold again B->C, never across.
    for _ in range(n):
        tr, day = b.train_day(min_stops=3)
        k = len(tr["stops"])
        m = rng.randrange(1, k - 1)
        ops = b.fill([b.leg(tr, day, 0, m)], 0) + b.fill([b.leg(tr, day, m, k - 1)], 0)
        ops.append(b.book([b.leg(tr, day, rng.randrange(0, m), rng.randrange(m + 1, k))], 0))
        case("b_interval", ops)

    # Multi-leg atomicity: a book whose second leg is full takes nothing.
    for _ in range(n):
        (t1, d1, a, j), (t2, d2, j2, z) = b.transfer_pair()
        l1, l2 = b.leg(t1, d1, a, j), b.leg(t2, d2, j2, z)
        ops = b.fill([l2], 0)
        ops += [b.book([l1, l2], 0, rng.randint(1, 3)) for _ in range(3)]
        ops += b.fill([l1], 0)  # every seat of the first leg is still free
        case("b_multileg", ops)

    # Groups and seat validity: distinct seats of the right class.
    for _ in range(n):
        tr, day = b.train_day()
        k = len(tr["stops"])
        ops = []
        for _ in range(8):
            a = rng.randrange(0, k - 1)
            z = rng.randrange(a + 1, k)
            pax = rng.randint(2, 5)
            ops.append(b.book([b.leg(tr, day, a, z)], rng.choice([0, 1, 2]), pax,
                              rng.randrange(1 << pax), rng.randint(0, 1)))
        case("b_seats", ops)

    # Cross-line trains: interval rules across the junction.
    cross = [t for t in w.trains if t["cross"]]
    for _ in range(n if cross else 0):
        tr, day = b.train_day(lambda t: t["cross"], min_stops=3)
        k = len(tr["stops"])
        leg = b.leg(tr, day, 0, k - 1)
        ops = b.fill([leg], 0) + [b.book([b.leg(tr, day, rng.randrange(0, k - 1), k - 1)], 0)]
        case("b_crossline", ops)

    # Refunds: once Ok, then NotFound; seats come back.
    for i in range(n):
        tr, day = b.train_day()
        k = len(tr["stops"])
        leg = b.leg(tr, day, 0, k - 1)
        ops = b.fill([leg], 0, label_prefix="o")
        ops += ["R @o1", "R @o1", "R #0"]
        ops += [b.book([leg], 0, 1) for _ in range(5)]  # the freed seats can be sold again
        case("refund", ops)

    # Refund of a multi-leg order frees every leg.
    for _ in range(n):
        (t1, d1, a, j), (t2, d2, j2, z) = b.transfer_pair()
        l1, l2 = b.leg(t1, d1, a, j), b.leg(t2, d2, j2, z)
        ops = b.fill([l1], 0, seats=SEATS[0] - 2) + b.fill([l2], 0, seats=SEATS[0] - 2)
        ops.append(b.book([l1, l2], 0, 2, label="m"))
        ops += ["R @m"] + [b.book([l1], 0, 2), b.book([l2], 0, 2)]
        case("refund_multileg", ops)
    return cases


def query_candidates(w, rng, count):
    qs = []
    while len(qs) < count:
        a = rng.randrange(w.stations)
        if rng.random() < 0.3:
            z = rng.randrange(w.stations)
        else:  # a 1-3 leg ride, so that transfers are common
            z = a
            for _ in range(rng.randint(1, MAX_LEGS)):
                if z not in w.at:
                    break
                t, j = rng.choice(w.at[z])
                stops = w.trains[t]["stops"]
                if j + 1 >= len(stops):
                    break
                z = stops[rng.randrange(j + 1, len(stops))][0]
        if z == a:
            continue
        earliest = rng.randrange(DAYS * DAY_MIN) if rng.random() < 0.7 else rng.randrange(DAYS) * DAY_MIN + rng.randrange(1200, DAY_MIN)
        qs.append((a, z, earliest, rng.randint(1, 5)))
    return qs


def categorize(w, q, answer):
    p = answer.split()
    if p[0] != "0":
        return "q_invalid"
    if p[1] == "0":
        return "q_none"
    items = [list(map(int, x.split())) for x in " ".join(p[2:]).split("|")[1:]]
    legs = [[it[i:i + 6] for i in range(0, len(it), 6)] for it in items]
    if any(w.trains[l[0]]["cross"] for its in legs for l in its):
        return "q_crossline"
    most = max(len(its) for its in legs)
    if most == 3:
        return "q_transfer2"
    day = q[2] // DAY_MIN
    if any(l[5] >= (day + 1) * DAY_MIN for its in legs for l in its):
        return "q_overnight"
    return "q_transfer1" if most == 2 else "q_direct"


def invalid_queries(w, rng, n):
    out = []
    for i in range(n):
        a, z = rng.randrange(w.stations), rng.randrange(w.stations)
        z = z if z != a else (a + 1) % w.stations
        kind = i % 6
        out.append([(a, a, 600, 3), (w.stations, z, 600, 3), (a, z, -1, 3), (a, z, DAYS * DAY_MIN, 3),
                    (a, z, 600, 0), (a, z, 600, 6)][kind])
    return out


def write(path, cases):
    with open(path, "w", newline="\n") as f:
        f.write(f"RAILSUITE 1\nCASES {len(cases)}\n")
        for cat, ops in cases:
            f.write(f"C {cat}\n")
            for op in ops:
                f.write((f"Q {op[0]} {op[1]} {op[2]} {op[3]} | ?" if isinstance(op, tuple) else op) + "\n")
            f.write("E\n")
        f.write("END\n")


def run(argv):
    p = subprocess.run([str(a) for a in argv], capture_output=True, text=True)
    if p.returncode != 0:
        sys.exit(f"make_suite: {' '.join(map(str, argv))} failed:\n{p.stdout[-2000:]}\n{p.stderr[-2000:]}")
    return p


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--world", required=True)
    ap.add_argument("--reference", required=True, help="railrun binary of the reference engine")
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--per-category", type=int, default=20)
    a = ap.parse_args()
    w = World(a.world)
    rng = random.Random(a.seed)
    n = a.per_category

    with tempfile.TemporaryDirectory() as tmp:
        cand = query_candidates(w, rng, 60 * n)
        write(Path(tmp) / "cand.suite", [("q", [q]) for q in cand])
        run([a.reference, "func", a.world, Path(tmp) / "cand.suite", "--record", Path(tmp) / "cand.rec"])
        answers = [line.split(" | ", 1)[1] for line in (Path(tmp) / "cand.rec").read_text().splitlines()
                   if line.startswith("Q ")]
        bins = {c: [] for c in QUERY_CATEGORIES}
        for q, ans in zip(cand, answers):
            bins[categorize(w, q, ans)].append(q)
        bins["q_invalid"] = invalid_queries(w, rng, n)
        cases = []
        others = [q for c in ("q_direct", "q_transfer1") for q in bins[c][n:]] or bins["q_direct"]
        for cat in QUERY_CATEGORIES:
            for i, q in enumerate(bins[cat][:n]):
                # "No itinerary" alone would reward an engine that never finds one.
                cases.append((cat, [q, others[i % len(others)]] if cat == "q_none" else [q]))
            if len(bins[cat]) < n // 2:
                print(f"make_suite: only {len(bins[cat])} {cat} queries", file=sys.stderr)
        cases += book_cases(w, rng, n)
        write(Path(tmp) / "suite.in", cases)
        run([a.reference, "func", a.world, Path(tmp) / "suite.in", "--record", a.out])
    check = subprocess.run([a.reference, "func", a.world, a.out], capture_output=True, text=True)
    if check.returncode != 0:
        sys.exit(f"make_suite: the reference fails its own suite:\n{check.stdout[-3000:]}")
    counts = {}
    for cat, _ in cases:
        counts[cat] = counts.get(cat, 0) + 1
    print(f"suite: {len(cases)} cases, " + ", ".join(f"{c} {k}" for c, k in counts.items()), file=sys.stderr)


if __name__ == "__main__":
    main()
