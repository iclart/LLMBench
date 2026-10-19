"""Conservative large-history checks and exact reconstruction at quiescence.

Lifetimes use guaranteed/possible occupancy envelopes. They can prove errors
without guessing a linearization order; they cannot prove every history.
"""
from model import Model, OK, SOLD_OUT, INVALID, NOT_FOUND, WITHHELD, cells


def audit(initial, events):
    errors = []
    all_orders = initial.orders.copy()
    # oid -> (create event or None for a pre-existing order)
    created = {oid: None for oid in all_orders}
    removed = {}
    def fail(kind, event):
        errors.append({"kind": kind, "event": event["index"]})
    # Establish immutable order identities and returned seats independently
    # of write ordering. No available-seat assumption is made here.
    for e in events:
        if e["op"] not in ("B", "E"):
            continue
        b = e["request"] if e["op"] == "B" else e["request"]["replacement"]
        resolved = initial.validate_book(b)
        if resolved is None:
            if e["status"] != INVALID:
                fail("invalid_booking", e)
            continue
        if e["status"] == WITHHELD and not initial.management:
            fail("withheld_in_performance", e)
        if e["op"]=="B" and e["status"] in (INVALID,NOT_FOUND):
            fail("booking_status",e)
        if e["status"] != OK:
            continue
        oid = e.get("order", 0)
        output = e.get("seats", [])
        if not oid or oid in all_orders:
            fail("order_identity", e); continue
        if len(output) != len(resolved) or any(len(row) != b["pax"] for row in output):
            fail("seat_shape", e); continue
        indices = [[initial.seat_index(b["class"], seat) for seat in row] for row in output]
        if any(None in row or len(set(row)) != b["pax"] for row in indices):
            fail("invalid_seat", e); continue
        all_orders[oid] = {"request": b, "resolved": resolved, "seats": indices,
                           "active": cells(b["pax"], len(resolved))}
        created[oid] = e
    for e in events:
        if e["op"] not in ("R", "E"):
            continue
        request, status = e["request"], e["status"]
        mask, oid = request["cells"], request["order"]
        malformed = not 0 < mask < 32768
        if e["op"] == "E":
            b = request["replacement"]
            malformed |= initial.validate_book(b) is None or sum(bool(mask & (7 << (p * 3))) for p in range(5)) != b["pax"]
        if malformed:
            if status != INVALID:
                fail("invalid_selection", e)
            continue
        order = all_orders.get(oid)
        creator = created.get(oid)
        if order is None or (creator is not None and e["end"] < creator["start"]):
            if status != NOT_FOUND:
                fail("unknown_order", e)
            continue
        valid_mask = cells(order["request"]["pax"], len(order["resolved"]))
        if mask & ~valid_mask:
            if status != INVALID:
                fail("selection_shape", e)
            continue
        if status==INVALID or (e["op"]=="R" and status not in (OK,NOT_FOUND)):
            fail("selection_status",e)
        if status != OK:
            continue
        for p in range(order["request"]["pax"]):
            for l in range(len(order["resolved"])):
                bit = 1 << (p * 3 + l)
                if not mask & bit:
                    continue
                key = (oid, p, l)
                if not order["active"] & bit or key in removed:
                    fail("double_release", e)
                else:
                    removed[key] = e
    for e in events:
        if e["op"] not in ("R", "E") or e["status"] != NOT_FOUND:
            continue
        request = e["request"]; order = all_orders.get(request["order"])
        if order is None or not 0 < request["cells"] < 32768:
            continue
        creator = created[request["order"]]
        definitely_created = creator is None or creator["end"] < e["start"]
        mask = request["cells"]
        if mask & ~cells(order["request"]["pax"], len(order["resolved"])):
            continue
        can_be_missing = any(not order["active"] & (1 << (p * 3 + l)) or
                             ((request["order"], p, l) in removed and removed[(request["order"], p, l)]["start"] <= e["end"])
                             for p in range(5) for l in range(3) if mask & 1 << (p * 3 + l))
        if definitely_created and not can_be_missing:
            fail("false_not_found", e)
    lifetimes = []
    by_seat = {}
    by_resource = {}
    for oid, order in all_orders.items():
        creator = created[oid]
        for p in range(order["request"]["pax"]):
            for l, r in enumerate(order["resolved"]):
                bit = 1 << (p * 3 + l)
                if not order["active"] & bit:
                    continue
                death = removed.get((oid, p, l))
                entry = {"key": (r[0], r[1], order["request"]["class"], order["seats"][l][p]),
                         "hold": (r[2], r[3], oid, p, l), "creator": creator, "death": death,
                         "possible_start": creator["start"] if creator else -1,
                         "certain_start": creator["end"] if creator else -1,
                         "possible_end": death["end"] if death else float("inf"),
                         "certain_end": death["start"] if death else float("inf")}
                lifetimes.append(entry); by_seat.setdefault(entry["key"], []).append(entry)
                by_resource.setdefault(entry["key"][:3], []).append(entry)
    for holds in by_seat.values():
        # Only overlapping stop ranges and guaranteed live-time intervals
        # prove an oversell. Atomic exchanges may touch at a single point.
        active=[]
        for a in sorted(holds,key=lambda h:h["certain_start"]):
            if a["certain_start"]>=a["certain_end"]:continue
            active=[b for b in active if b["certain_end"]>a["certain_start"]]
            for b in active:
                if a["hold"][0] >= b["hold"][1] or b["hold"][0] >= a["hold"][1]:
                    continue
                if max(a["certain_start"], b["certain_start"]) < min(a["certain_end"], b["certain_end"]):
                    errors.append({"kind": "oversell", "orders": [a["hold"][2], b["hold"][2]]})
            active.append(a)
    def envelope(event, worst, ignore=frozenset(), resources=None):
        result = initial.empty_inventory()
        candidates = lifetimes if resources is None else (h for key in resources for h in by_resource.get(key, ()))
        for h in candidates:
            if h["hold"][2:] in ignore:
                continue
            included = (h["possible_start"] <= event["end"] and h["possible_end"] >= event["start"]) if worst else (
                h["certain_start"] < event["start"] and h["certain_end"] > event["end"])
            if included:
                result.holds.setdefault(h["key"], []).append(h["hold"])
        return result
    # Full SoldOut checks are inexpensive relative to full route enumeration.
    for e in events:
        if e["op"] not in ("B", "E") or e["status"] != SOLD_OUT:
            continue
        b = e["request"] if e["op"] == "B" else e["request"]["replacement"]
        resolved = initial.validate_book(b)
        if resolved is None:
            continue
        ignored = set()
        if e["op"] == "E":
            oid, mask = e["request"]["order"], e["request"]["cells"]
            ignored = {(oid, p, l) for p in range(5) for l in range(3) if mask & 1 << (p * 3 + l)}
        worst = envelope(e, True, ignored, {(r[0], r[1], b["class"]) for r in resolved})
        if all(worst.available(r, b["class"], b["pax"]) for r in resolved):
            fail("false_sold_out", e)
    query_events = [e for e in events if e["op"] == "Q"]
    # Spread expensive exact-envelope queries over the entire component.
    selected = {i * len(query_events) // min(64, len(query_events)) for i in range(min(64, len(query_events)))}
    for index, e in enumerate(query_events):
        q = e["request"]
        valid = (len(q) == 6 and 0 <= q[0] < initial.world["stations"] and 0 <= q[1] < initial.world["stations"] and
                 q[0] != q[1] and 0 <= q[2] < 4320 and 1 <= q[3] <= 5 and 0 <= q[4] < 3 and 1 <= q[5] <= 5)
        if not valid:
            if e["status"] != INVALID:
                fail("invalid_query", e)
            continue
        if e["status"] != OK:
            fail("query_status", e); continue
        items = e.get("result", [])
        if e.get("count") != len(items) or len(items) > q[3]:
            fail("query_shape", e); continue
        resources = {(leg[0], leg[1], q[4]) for item in items for leg in item["legs"]}
        optimistic = envelope(e, False, resources=resources)
        keys, sequences = [], []
        for item in items:
            path = item["legs"]
            b = {"legs": [leg[:4] for leg in path], "class": q[4], "pax": q[5]}
            r = initial.validate_book(b)
            if (item["count"] != len(path) or r is None or [x[4] for x in r] != path or path[0][2] != q[0] or
                    path[-1][3] != q[1] or not q[2] <= path[0][4] < (q[2] // 1440 + 1) * 1440):
                fail("query_route", e); continue
            if not all(optimistic.available(x, q[4], q[5]) for x in r):
                fail("query_impossible_inventory", e)
            sequence = tuple(tuple(leg[:2]) for leg in path)
            sequences.append(sequence); keys.append((len(path), path[-1][5], -path[0][4], sequence))
        if keys != sorted(keys) or len(sequences) != len(set(sequences)):
            fail("query_order", e)
        if index in selected:
            _, guaranteed = envelope(e, True).query(q)
            for path in guaranteed:
                sequence = tuple(tuple(leg[:2]) for leg in path)
                key = (len(path), path[-1][5], -path[0][4], sequence)
                if sequence not in sequences and (len(items) < q[3] or (keys and key < keys[-1])):
                    fail("query_definite_omission", e)
    final = initial.empty_inventory()
    final.orders = all_orders
    changed=set()
    for key in removed:
        oid, p, l = key
        if oid not in changed:
            final.orders[oid]=final.orders[oid].copy();changed.add(oid)
        final.orders[oid]["active"] &= ~(1 << (p * 3 + l))
    for h in lifetimes:
        if h["death"] is None:
            final.holds.setdefault(h["key"], []).append(h["hold"])
    return final, errors, {"query_envelope_samples": len(selected), "lifetimes": len(lifetimes)}
