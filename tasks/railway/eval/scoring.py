"""The railway task's scoring formula (TASK.md 9, docs/DESIGN.md 4).

Shared by the official evaluation (railway_eval.py) and the workspace's
`make score` estimate, so both always compute the same thing. Inputs are
measurements as railway_eval.py records them: one dict per "<scenario>/t<N>"
with throughput, p99 latencies, the quiet phase's preference rates and the
main phase's booking quality from railjudge.

  total       = 65% performance + 10% functional + 12% transport
                + 8% preference + 5% memory
  performance = mean over scenarios x thread counts of
                70% throughput + 30% p99 latency, each scored on a power
                curve of r = value / the strong reference's:
                100 (r^0.6 - c^0.6) / (1 - c^0.6), c = baseline / strong,
                so 0 at the baseline, 100 at the strong reference, at most
                150; below the baseline -100 (1 - (value / baseline)^0.6),
                down to -100; a measurement that timed out, failed or was
                gated scores -100, so failing never beats being slow
  transport   = mean over scenarios of a score for r = passenger-minutes
                carried / the strong reference's: 70 at r = 1, 0 at r <= 0.93,
                100 at r >= 1.03, linear in between
  preference  = mean over scenarios of 100 x window and together rates /
                the strong reference's
  functional  = 100 x the mean pass rate over the categories of the
                functional suite (railrun func), whatever the scenarios did
  memory      = mean over scenarios of a score for m = the engine's peak
                memory (largest of the scenario's runs, minus the harness's
                own footprint) / the strong reference's: 100 at m <= 1,
                0 at m >= 4, 100 - 50 log2(m) in between
  gate        = a scenario whose satisfaction (purchases served) falls below
                85% of the strong reference's scores -100 in performance and
                0 in its other parts, like a scenario with wrong results
  fatal       = a data race or a slow init: every measurement -100, the rest 0
Transport, preference and the gate use the 1-thread measurement (its
replay order is fixed), or the lowest thread count measured.
"""
import json
import math
from pathlib import Path

SCENARIOS = ["daily", "spring", "refund_wave"]
PERF_WEIGHT, FUNCTIONAL_WEIGHT, TRANSPORT_WEIGHT, PREF_WEIGHT, MEMORY_WEIGHT = 0.65, 0.10, 0.12, 0.08, 0.05
THROUGHPUT_WEIGHT, LATENCY_WEIGHT = 0.7, 0.3
# Performance: 100 (r^e - c^e) / (1 - c^e) with r = value / strong and
# c = baseline / strong, capped; c is at most 1 / MIN_SPAN so that a strong
# reference barely ahead of the baseline does not turn noise into points.
CURVE_EXPONENT, CURVE_CAP, MIN_SPAN = 0.6, 150.0, 4.0
PERF_FLOOR = -100.0  # below the baseline the score falls towards this; failures get it
SATISFACTION_GATE = 0.85
# Transport: (ratio to the strong reference, score) points, linear between.
TRANSPORT_FLOOR, TRANSPORT_PAR, TRANSPORT_CEIL = (0.93, 0.0), (1.0, 70.0), (1.03, 100.0)
MEMORY_ZERO_RATIO = 4.0
MIN_ENGINE_MB = 1.0  # below this the engine's own memory is measurement noise


def curve_score(x, base, strong):
    """0 at the baseline, 100 at the strong reference, a power curve in x / strong between, at most CURVE_CAP;
    below the baseline a power curve in x / baseline down to PERF_FLOOR.

    A log scale would spread 0-100 evenly over every doubling from the
    baseline up, so with the strong reference hundreds of times faster
    than the baseline, half its throughput would still score close to 90.
    The power curve keeps most of the range near the strong reference
    (half scores about 65) and still gives credit far below it.
    """
    if base <= 0 or strong <= 0:
        return 0.0
    base = min(base, strong / MIN_SPAN)
    if x < base:
        return PERF_FLOOR * (1.0 - (max(x, 0.0) / base) ** CURVE_EXPONENT)
    c = (base / strong) ** CURVE_EXPONENT
    return min(CURVE_CAP, 100.0 * ((x / strong) ** CURVE_EXPONENT - c) / (1.0 - c))


def narrow_spans(anchors):
    """The measurements whose strong reference is less than MIN_SPAN times the
    baseline (throughput or latency gain); scored as if it were MIN_SPAN times."""
    out = []
    for key, base in anchors["baseline"].items():
        strong = anchors["strong"].get(key)
        if not strong or "throughput" not in base or "throughput" not in strong:
            continue
        for what, span in (("throughput", strong["throughput"] / max(base["throughput"], 1e-9)),
                           ("latency", latency_gain(strong, base))):
            if span < MIN_SPAN:
                out.append((key, what, span))
    return out


def latency_gain(m, base):
    return math.sqrt((base["query_p99_ns"] / max(m["query_p99_ns"], 1)) * (base["book_p99_ns"] / max(m["book_p99_ns"], 1)))


def measurement_score(m, base, strong):
    thr = curve_score(m["throughput"], base["throughput"], strong["throughput"])
    lat = curve_score(latency_gain(m, base), 1.0, latency_gain(strong, base))
    return THROUGHPUT_WEIGHT * thr + LATENCY_WEIGHT * lat, thr, lat


def preference_score(quiet, strong_quiet):
    parts = []
    for key in ("window_rate", "together_rate"):
        ref = strong_quiet[key]
        parts.append(quiet[key] / ref if ref > 0 else 1.0)
    return 100.0 * sum(parts) / len(parts)


def transport_ratio(quality, strong_quality):
    ref = strong_quality["passenger_minutes"]
    return quality["passenger_minutes"] / ref if ref > 0 else 1.0


def transport_score(quality, strong_quality):
    r = transport_ratio(quality, strong_quality)
    (r0, s0), (r1, s1), (r2, s2) = TRANSPORT_FLOOR, TRANSPORT_PAR, TRANSPORT_CEIL
    if r <= r0:
        return s0
    if r <= r1:
        return s0 + (s1 - s0) * (r - r0) / (r1 - r0)
    return min(s2, s1 + (s2 - s1) * (r - r1) / (r2 - r1))


def engine_mb(measurements, sc, threads, harness_mb):
    """Peak memory of the engine itself in one scenario, or None if unmeasured."""
    peaks = [m["maxrss_mb"] for t in threads
             if (m := measurements.get(f"{sc}/t{t}", {})).get("status", "ok") == "ok" and m.get("maxrss_mb")]
    return max(MIN_ENGINE_MB, max(peaks) - harness_mb) if peaks else None


def memory_score(mb, strong_mb):
    m = mb / strong_mb if strong_mb > 0 else 1.0
    if m <= 1.0:
        return 100.0
    return max(0.0, 100.0 * (1.0 - math.log2(m) / math.log2(MEMORY_ZERO_RATIO)))


def functional_score(functional):
    """functional: {"categories": {name: {"passed": n, "total": n}}} from railrun func."""
    cats = [c for c in (functional or {}).get("categories", {}).values() if c.get("total")]
    return 100.0 * sum(c["passed"] / c["total"] for c in cats) / len(cats) if cats else 0.0


def satisfaction_ratio(quality, strong_quality):
    ref = strong_quality["satisfaction"]
    return quality["satisfaction"] / ref if ref > 0 else 1.0


def score(measurements, anchors, threads, failed=(), fatal=False, functional=None, scenarios=SCENARIOS):
    """Scores a set of measurements against the anchors.

    measurements: {"<sc>/t<N>": {"status": "ok", ...metrics...}}; a missing
    key or another status scores PERF_FLOOR. failed: scenarios that failed outright
    (crash, wrong results, memory). functional: railrun func's result.
    Returns the scores dict railway_eval.py stores in its result.
    """
    quality_threads = 1 if 1 in threads else min(threads)
    per, perf_parts, transport_parts, pref_parts, gates, ratios = {}, [], [], [], {}, {}
    mem_parts, mem_mb = [], {}
    for sc in scenarios:
        qkey = f"{sc}/t{quality_threads}"
        qm = measurements.get(qkey, {})
        usable = not fatal and sc not in failed and qm.get("status") == "ok"
        gate = None
        if usable and "quality" in qm:
            ratio = satisfaction_ratio(qm["quality"], anchors["strong"][qkey]["quality"])
            gate = {"satisfaction": qm["quality"]["satisfaction"],
                    "strong": anchors["strong"][qkey]["quality"]["satisfaction"],
                    "ratio": ratio, "passed": ratio >= SATISFACTION_GATE}
            usable = gate["passed"]
        gates[sc] = gate
        for t in threads:
            key = f"{sc}/t{t}"
            m = measurements.get(key, {})
            if not usable or m.get("status") != "ok":
                per[key] = {"score": PERF_FLOOR, "throughput_score": PERF_FLOOR, "latency_score": PERF_FLOOR}
            else:
                s, thr, lat = measurement_score(m, anchors["baseline"][key], anchors["strong"][key])
                per[key] = {"score": s, "throughput_score": thr, "latency_score": lat}
            perf_parts.append(per[key]["score"])
        ratios[sc] = (transport_ratio(qm["quality"], anchors["strong"][qkey]["quality"])
                      if gate is not None else None)
        harness = anchors.get("harness_mb", {}).get(sc, 0.0)
        mine = engine_mb(measurements, sc, threads, harness)
        strong = engine_mb(anchors["strong"], sc, threads, harness)
        mem_mb[sc] = {"engine": mine, "strong": strong}
        if usable:
            transport_parts.append(transport_score(qm["quality"], anchors["strong"][qkey]["quality"])
                                   if "quality" in qm else 0.0)
            pref_parts.append(preference_score(qm["quiet"], anchors["strong"][qkey]["quiet"]))
            mem_parts.append(memory_score(mine, strong) if mine is not None and strong else 0.0)
        else:
            transport_parts.append(0.0)
            pref_parts.append(0.0)
            mem_parts.append(0.0)
    perf = sum(perf_parts) / len(perf_parts)
    transport = sum(transport_parts) / len(transport_parts)
    pref = sum(pref_parts) / len(pref_parts)
    memory = sum(mem_parts) / len(mem_parts)
    func = 0.0 if fatal else functional_score(functional)
    total = (PERF_WEIGHT * perf + FUNCTIONAL_WEIGHT * func + TRANSPORT_WEIGHT * transport
             + PREF_WEIGHT * pref + MEMORY_WEIGHT * memory)
    return {"total": total, "performance": perf, "functional": func, "transport": transport, "preference": pref,
            "memory": memory, "memory_mb": mem_mb, "memory_by_scenario": dict(zip(scenarios, mem_parts)),
            "measurements": per, "gates": gates, "quality_threads": quality_threads,
            "transport_by_scenario": dict(zip(scenarios, transport_parts)), "transport_ratio": ratios,
            "preference_by_scenario": dict(zip(scenarios, pref_parts))}


# ------------------------------------------------------------------ functional suite

def suite_categories(path):
    """The category of every case of a functional suite, in order."""
    return [line.split()[1] for line in Path(path).read_text().splitlines() if line.startswith("C ")]


def run_functional(suite, trace, launch, crashes_per_category=5):
    """Runs a functional suite to the end despite crashes.

    launch(start) runs `railrun func <world> <suite> --from start --trace
    <trace>` once and returns None if it exited normally, else a short
    reason ("crash (signal 11)", "timeout"). A case that crashes the engine
    fails and the run resumes after it with a fresh engine. After a timeout,
    or more crashes than crashes_per_category within one category, the rest
    of that category fails and the run resumes at the next category. Returns
    {"categories": {name: {"passed", "total", "failures"}}, "crashes": n}.
    """
    cats = suite_categories(suite)
    trace = Path(trace)
    trace.unlink(missing_ok=True)
    outcome, start, crashes, per_cat = {}, 0, 0, {}
    while start < len(cats):
        reason = launch(start)
        if trace.exists():
            for line in trace.read_text().splitlines():
                i, verdict, *msg = line.split(" ", 2)
                outcome[int(i)] = (verdict == "ok", json.loads(msg[0]) if msg and msg[0] else "")
        nxt = next((i for i in range(start, len(cats)) if i not in outcome), len(cats))
        if nxt >= len(cats):
            break
        reason = reason or "stopped early"
        cat = cats[nxt]
        per_cat[cat] = per_cat.get(cat, 0) + 1
        crashes += 1
        outcome[nxt] = (False, f"case {nxt}: the engine died ({reason})")
        start = nxt + 1
        if reason == "timeout" or per_cat[cat] > crashes_per_category:
            while start < len(cats) and cats[start] == cat:
                outcome[start] = (False, f"case {start}: not run, the engine kept dying ({reason})")
                start += 1
    result = {}
    for i, cat in enumerate(cats):
        r = result.setdefault(cat, {"passed": 0, "total": 0, "failures": []})
        ok, msg = outcome.get(i, (False, "not run"))
        r["total"] += 1
        r["passed"] += ok
        if not ok and len(r["failures"]) < 3:
            r["failures"].append(msg if msg.startswith("case") else f"case {i}, {msg}")
    return {"categories": result, "crashes": crashes}

