"""Independent literal ABI 2 model. No shared engine validation/search code.

Successful writes adopt the contestant's actual seats, rather than assuming
that two correct implementations choose the same seats.
"""
import copy

OK, SOLD_OUT, INVALID, NOT_FOUND, WITHHELD = range(5)
LETTERS = ("ACF", "ACDF", "ABCDF")
ROWS = (8, 13, 18)
CARS = (1, 2, 4)
COUNTS = (24, 104, 450)


def load_world(path):
    words = iter(path.read_text().split())
    if next(words) != "RAILWORLD":
        raise ValueError("invalid world magic")
    version = int(next(words))
    if version not in (1, 2) or next(words) != "STATIONS":
        raise ValueError("unsupported world")
    stations = int(next(words))
    for _ in range(stations):
        next(words)
    if next(words) != "LINES":
        raise ValueError("missing lines")
    for _ in range(int(next(words))):
        next(words)
        for _ in range(int(next(words))):
            next(words)
    if next(words) != "TRAINS":
        raise ValueError("missing trains")
    trains = []
    for _ in range(int(next(words))):
        next(words)
        lines = int(next(words)) if version == 2 else 1
        for _ in range(lines):
            next(words)
        trains.append([tuple(int(next(words)) for _ in range(3)) for _ in range(int(next(words)))])
    if next(words) != "END" or next(words, None) is not None:
        raise ValueError("invalid world trailer")
    return {"stations": stations, "trains": trains}


def cells(pax, legs):
    return sum(1 << (p * 3 + l) for p in range(pax) for l in range(legs))


class Model:
    def __init__(self, world, management=False):
        self.world = world
        self.management = management
        self.orders = {}
        self.holds = {}  # (train, day, class, class-relative seat) -> (a,b,id,p,l)
        self.at = [[] for _ in range(world["stations"])]
        self.incoming=[set() for _ in self.at]
        self.reach_cache={}
        for t, stops in enumerate(world["trains"]):
            previous=set()
            for i, (station, _, _) in enumerate(stops):
                self.at[station].append((t, i))
                self.incoming[station].update(previous);previous.add(station)

    def clone(self):
        result = copy.copy(self)
        # Payloads and hold lists are immutable between states; mutations below
        # replace the changed dictionary/list rather than modifying a parent.
        result.orders = self.orders.copy()
        result.holds = self.holds.copy()
        return result

    def empty_inventory(self):
        """Share immutable timetable indices without rebuilding them per call."""
        result=copy.copy(self)
        result.orders={};result.holds={}
        return result

    def resolve(self, leg):
        if len(leg) != 4:
            return None
        t, d, a, b = leg
        if not 0 <= t < len(self.world["trains"]) or not 0 <= d < 3:
            return None
        stops = self.world["trains"][t]
        indices = {stop[0]: i for i, stop in enumerate(stops)}
        if a not in indices or b not in indices or indices[a] >= indices[b]:
            return None
        i, j = indices[a], indices[b]
        return (t, d, i, j, [t, d, a, b, d * 1440 + stops[i][2], d * 1440 + stops[j][1]])

    def validate_book(self, b):
        if not 1 <= len(b["legs"]) <= 3 or not 1 <= b["pax"] <= 5 or not 0 <= b["class"] < 3:
            return None
        resolved = [self.resolve(leg) for leg in b["legs"]]
        if any(r is None for r in resolved):
            return None
        stops = [b["legs"][0][2]] + [leg[3] for leg in b["legs"]]
        if len(stops) != len(set(stops)) or len({(r[0], r[1]) for r in resolved}) != len(resolved):
            return None
        for prev, cur in zip(resolved, resolved[1:]):
            if prev[4][3] != cur[4][2] or prev[4][5] + 30 > cur[4][4]:
                return None
        return resolved

    def free(self, r, cls, seat, ignored=frozenset()):
        t, day, a, b = r[:4]
        return not any(x < b and a < y and (oid, p, l) not in ignored
                       for x, y, oid, p, l in self.holds.get((t, day, cls, seat), ()))

    def available(self, r, cls, pax, ignored=frozenset()):
        free=0
        for seat in range(COUNTS[cls]):
            if self.free(r,cls,seat,ignored):
                free+=1
                if free>=pax:return True
        return False

    def query(self, q):
        origin, destination, earliest, k, cls, pax = q
        if (not 0 <= origin < self.world["stations"] or not 0 <= destination < self.world["stations"] or
                origin == destination or not 0 <= earliest < 4320 or not 1 <= k <= 5 or not 0 <= cls < 3 or not 1 <= pax <= 5):
            return INVALID, []
        found = {}
        end = (earliest // 1440 + 1) * 1440
        if destination not in self.reach_cache:
            reach=[frozenset((destination,))]
            for _ in range(3):
                reach.append(frozenset(origin for target in reach[-1] for origin in self.incoming[target]))
            self.reach_cache[destination]=tuple(reach)
        reach=self.reach_cache[destination]
        ending = [[] for _ in self.at]
        for t, j in self.at[destination]:
            for i in range(j):
                ending[self.world["trains"][t][i][0]].append((t, i))
        rank = lambda path: (len(path), path[-1][5], -path[0][4], tuple(x for leg in path for x in leg[:2]))
        # The worst kept arrival once k itineraries are kept; recomputed only
        # when `found` changes (it is read at every step of the search).
        limit = [float("inf")]
        def bound():
            return limit[0]
        def update_bound():
            limit[0] = max(found.values(), key=rank)[-1][5] if len(found) >= k else float("inf")

        def visit(path, station, ready, transfers):
            remaining=length-len(path)
            if station not in reach[remaining] or ready >= bound():
                return
            last = len(path) + 1 == length
            for t, i in ending[station] if last else self.at[station]:
                stops = self.world["trains"][t]
                for day in range(3):
                    depart = day * 1440 + stops[i][2]
                    if depart < ready or depart >= bound() or (not path and depart >= end) or any(leg[:2] == [t, day] for leg in path):
                        continue
                    for j in range(i + 1, len(stops)):
                        at, arrive, _ = stops[j]
                        if (at == destination) != last or at == origin or at in transfers or at not in reach[remaining-1] or day * 1440 + arrive > bound():
                            continue
                        r = (t, day, i, j)
                        if not self.available(r, cls, pax):
                            continue
                        leg = [t, day, station, at, depart, day * 1440 + arrive]
                        next_path = path + [leg]
                        if at == destination:
                            key = tuple((leg[0], leg[1]) for leg in next_path)
                            arrival_key = lambda x: tuple(leg[5] for leg in x[:-1])
                            if key not in found or arrival_key(next_path) < arrival_key(found[key]):
                                found[key] = next_path
                            if len(found) > k:
                                worst = max(found, key=lambda key: rank(found[key]))
                                del found[worst]
                            update_bound()
                        elif len(next_path) < length:
                            visit(next_path, at, leg[5] + 30, transfers | {at})
        for length in range(1, 4):
            visit([], origin, earliest, set())
            if len(found) >= k:
                break
        values = sorted(found.values(), key=rank)
        return OK, values[:k]

    @staticmethod
    def seat_index(cls, seat):
        if len(seat) != 3:
            return None
        car, row, letter = seat
        if not CARS[cls] <= car < CARS[cls] + (1, 2, 5)[cls] or not 1 <= row <= ROWS[cls]:
            return None
        text = LETTERS[cls]
        if letter not in map(ord, text):
            return None
        return ((car - CARS[cls]) * ROWS[cls] + row - 1) * len(text) + text.index(chr(letter))

    def remove(self, oid, selected):
        order = self.orders[oid].copy()
        self.orders[oid] = order
        for p in range(order["request"]["pax"]):
            for l, r in enumerate(order["resolved"]):
                if selected & 1 << (p * 3 + l):
                    seat = order["seats"][l][p]
                    key = (r[0], r[1], order["request"]["class"], seat)
                    self.holds[key] = [h for h in self.holds[key] if h[2:] != (oid, p, l)]
        order["active"] &= ~selected

    def apply(self, event):
        """Return whether this event can occur next, committing only if valid."""
        op, request, status = event["op"], event["request"], event["status"]
        if op == "Q":
            expected, result = self.query(request)
            if status != expected:
                return False
            if status != OK:
                return True
            actual = event.get("result", [])
            if event.get("count") != len(actual) or any(it["count"] != len(it["legs"]) for it in actual):
                return False
            return [it["legs"] for it in actual] == result
        old, mask, ignored = None, 0, set()
        if op in ("R", "E"):
            oid, mask = request["order"], request["cells"]
            if not 0 < mask < 32768:
                return status == INVALID
            if op == "E":
                b = request["replacement"]
                resolved = self.validate_book(b)
                selected_pax = sum(bool(mask & (7 << (p * 3))) for p in range(5))
                if resolved is None or selected_pax != b["pax"]:
                    return status == INVALID
            old = self.orders.get(oid)
            if old is None:
                return status == NOT_FOUND
            if mask & ~cells(old["request"]["pax"], len(old["resolved"])):
                return status == INVALID
            if mask & old["active"] != mask:
                return status == NOT_FOUND
            if op == "R":
                if status != OK:
                    return False
                self.remove(oid, mask)
                return True
            ignored = {(oid, p, l) for p in range(5) for l in range(3) if mask & 1 << (p * 3 + l)}
        else:
            b = request
            resolved = self.validate_book(b)
            if resolved is None:
                return status == INVALID
        feasible = all(self.available(r, b["class"], b["pax"], ignored) for r in resolved)
        if not feasible:
            return status == SOLD_OUT
        if status == WITHHELD:
            return self.management
        if status != OK or not event.get("order") or event["order"] in self.orders:
            return False
        output = event.get("seats", [])
        if len(output) != len(resolved) or any(len(row) != b["pax"] for row in output):
            return False
        seat_indices = [[self.seat_index(b["class"], seat) for seat in row] for row in output]
        for r, indices in zip(resolved, seat_indices):
            if None in indices or len(set(indices)) != b["pax"] or not all(self.free(r, b["class"], s, ignored) for s in indices):
                return False
        if old is not None:
            self.remove(request["order"], mask)
        oid = event["order"]
        self.orders[oid] = {"request": copy.deepcopy(b), "resolved": resolved, "seats": seat_indices,
                            "active": cells(b["pax"], len(resolved))}
        for l, (r, indices) in enumerate(zip(resolved, seat_indices)):
            for p, seat in enumerate(indices):
                key = (r[0], r[1], b["class"], seat)
                self.holds[key] = [*self.holds.get(key, ()), (r[2], r[3], oid, p, l)]
        return True


def linearize(initial, events):
    """Exact bounded search, respecting real-time precedence. At most eight calls."""
    if len(events) > 8:
        raise ValueError("exact histories are limited to eight calls")
    predecessors = [sum(1 << j for j, other in enumerate(events)
                        if j != i and (other["end"] < event["start"] or
                           ("thread" in event and other.get("thread")==event["thread"] and other["index"]<event["index"])))
                    for i, event in enumerate(events)]
    visited = set()
    explored = 0

    def search(model, done):
        nonlocal explored
        explored += 1
        if done == (1 << len(events)) - 1:
            return model
        # Actual seat holds plus order cell states identify the sequential state.
        state = (done, tuple(sorted((oid, order["active"]) for oid, order in model.orders.items())),
                 tuple(sorted((key, tuple(sorted(value))) for key, value in model.holds.items() if value)))
        if state in visited:
            return None
        visited.add(state)
        for i, event in enumerate(events):
            if done >> i & 1 or predecessors[i] & ~done:
                continue
            candidate = model.clone()
            if candidate.apply(event):
                result = search(candidate, done | 1 << i)
                if result is not None:
                    return result
        return None
    return search(initial.clone(), 0), explored
