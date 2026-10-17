"""The railway task's scoring formula (TASK.md 9, docs/DESIGN.md 4).

Shared by the official evaluation (railway_eval.py) and the workspace's
`make score` estimate, so both always compute the same thing. Inputs are
measurements as railway_eval.py records them: one dict per "<scenario>/t<N>"
with throughput, p99 latencies, the quiet phase's preference rates and the
main phase's booking quality from railjudge.

  total       = 75% performance + 15% transport + 10% preference
  performance = mean over scenarios x thread counts of
                70% throughput + 30% p99 latency, each log-interpolated:
                0 at the baseline, 100 at the strong reference, not capped
  transport   = mean over scenarios of 100 x passenger-minutes carried /
                the strong reference's (not capped)
  preference  = mean over scenarios of 100 x window and together rates /
                the strong reference's
  gate        = a scenario whose satisfaction (purchases served) falls below
                85% of the strong reference's scores 0 in every part
Transport, preference and the gate use the 1-thread measurement (its
replay order is fixed), or the lowest thread count measured.
"""
import math

SCENARIOS = ["daily", "spring", "refund_wave"]
PERF_WEIGHT, TRANSPORT_WEIGHT, PREF_WEIGHT = 0.75, 0.15, 0.10
THROUGHPUT_WEIGHT, LATENCY_WEIGHT = 0.7, 0.3
SATISFACTION_GATE = 0.85


def log_score(x, base, strong):
    """100 at the strong reference, 0 at the baseline, log-interpolated, not capped above."""
    if x <= 0 or base <= 0 or strong <= 0:
        return 0.0
    span = math.log(strong / base)
    if span <= 1e-9:
        return 100.0 if x >= strong else 0.0
    return max(0.0, 100.0 * math.log(x / base) / span)


def latency_gain(m, base):
    return math.sqrt((base["query_p99_ns"] / max(m["query_p99_ns"], 1)) * (base["book_p99_ns"] / max(m["book_p99_ns"], 1)))


def measurement_score(m, base, strong):
    thr = log_score(m["throughput"], base["throughput"], strong["throughput"])
    lat = log_score(latency_gain(m, base), 1.0, latency_gain(strong, base))
    return THROUGHPUT_WEIGHT * thr + LATENCY_WEIGHT * lat, thr, lat


def preference_score(quiet, strong_quiet):
    parts = []
    for key in ("window_rate", "together_rate"):
        ref = strong_quiet[key]
        parts.append(quiet[key] / ref if ref > 0 else 1.0)
    return 100.0 * sum(parts) / len(parts)


def transport_score(quality, strong_quality):
    ref = strong_quality["passenger_minutes"]
    return 100.0 * quality["passenger_minutes"] / ref if ref > 0 else 100.0


def satisfaction_ratio(quality, strong_quality):
    ref = strong_quality["satisfaction"]
    return quality["satisfaction"] / ref if ref > 0 else 1.0


def score(measurements, anchors, threads, failed=(), fatal=False, scenarios=SCENARIOS):
    """Scores a set of measurements against the anchors.

    measurements: {"<sc>/t<N>": {"status": "ok", ...metrics...}}; a missing
    key or another status scores 0. failed: scenarios that failed outright
    (crash, wrong results, memory). Returns the scores dict railway_eval.py
    stores in its result.
    """
    quality_threads = 1 if 1 in threads else min(threads)
    per, perf_parts, transport_parts, pref_parts, gates = {}, [], [], [], {}
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
                per[key] = {"score": 0.0, "throughput_score": 0.0, "latency_score": 0.0}
            else:
                s, thr, lat = measurement_score(m, anchors["baseline"][key], anchors["strong"][key])
                per[key] = {"score": s, "throughput_score": thr, "latency_score": lat}
            perf_parts.append(per[key]["score"])
        if usable:
            transport_parts.append(transport_score(qm["quality"], anchors["strong"][qkey]["quality"])
                                   if "quality" in qm else 0.0)
            pref_parts.append(preference_score(qm["quiet"], anchors["strong"][qkey]["quiet"]))
        else:
            transport_parts.append(0.0)
            pref_parts.append(0.0)
    perf = sum(perf_parts) / len(perf_parts)
    transport = sum(transport_parts) / len(transport_parts)
    pref = sum(pref_parts) / len(pref_parts)
    total = 0.0 if fatal else PERF_WEIGHT * perf + TRANSPORT_WEIGHT * transport + PREF_WEIGHT * pref
    return {"total": total, "performance": perf, "transport": transport, "preference": pref,
            "measurements": per, "gates": gates, "quality_threads": quality_threads,
            "transport_by_scenario": dict(zip(scenarios, transport_parts)),
            "preference_by_scenario": dict(zip(scenarios, pref_parts))}
