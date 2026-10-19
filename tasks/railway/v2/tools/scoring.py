"""Versioned ABI 2 scores. Never reinterpret ABI 1 results with this module."""
import statistics
import math
OPS = ("query", "book", "exchange", "refund")


def curve_score(value, baseline, strong):
    # Frozen ABI 2 copy of the established square-root scale, so a future
    # ABI 1 module edit cannot reinterpret an experimental ABI 2 season.
    if baseline <= 0 or strong <= 0:
        return 0.
    baseline = min(baseline, strong / 4)
    if value < baseline:
        return -100 * (1 - (max(value, 0) / baseline) ** .5)
    c = (baseline / strong) ** .5
    return min(150., 100 * ((value / strong) ** .5 - c) / (1 - c))


def measurement_score(measured, baseline, strong):
    if any(not value["latency"][op]["sample_sufficient"] for value in (measured,baseline,strong) for op in OPS):
        return {"status": "insufficient_samples", "score": None}
    throughput = curve_score(measured["throughput"], baseline["throughput"], strong["throughput"])
    latencies = {op: curve_score(1 / max(1, measured["latency"][op]["p99_ns"]),
                                   1 / max(1, baseline["latency"][op]["p99_ns"]),
                                   1 / max(1, strong["latency"][op]["p99_ns"])) for op in OPS}
    latency = statistics.mean(latencies.values())
    return {"status": "ok", "score": .7 * throughput + .3 * latency,
            "throughput_score": throughput, "latency_score": latency, "latency_by_op": latencies}


def management_score(utility, greedy, reference, evidence_passed):
    if not evidence_passed or reference <= greedy:
        return None
    return min(100., max(0., 100. * (utility - greedy) / (reference - greedy)))


def release_gate(checks):
    required = ("correctness", "asan_ubsan", "tsan", "cross_platform_generator", "management", "pilots")
    missing = [name for name in required if checks.get(name) is not True]
    return {"publishable": not missing, "missing": missing}


def pilot_gate(runs):
    differences = []
    for run in runs:
        for key, expected in (("model", "opencode-go/deepseek-v4.1-flash"), ("variant", "max"),
                              ("tool_version", "2.0.32"), ("minutes", 105)):
            if run.get(key) != expected:
                differences.append({"run": run.get("id"), "field": key, "actual": run.get(key), "expected": expected})
    complete = len(runs) == 3 and len({r.get("id") for r in runs}) == 3
    correct = sum(r.get("correctness_passed") is True for r in runs)
    scores = [r["performance"] for r in runs if isinstance(r.get("performance"), (int, float))]
    median = statistics.median(scores) if len(scores) == 3 else None
    passed = complete and correct >= 2 and median is not None and median <= 80 and not differences
    return {"passed": passed, "correct_runs": correct, "median_performance": median, "differences": differences}


def score(measured, baseline, strong, management=None, checks=None):
    """Same-season comparison; missing evidence stays missing, never becomes zero."""
    for anchor in (baseline, strong):
        for field in ("abi_version", "task_version", "inputs_sha256", "platform", "cpus", "compile_flags"):
            if measured.get(field) is None or measured.get(field) != anchor.get(field):
                raise ValueError("incompatible anchor: " + field)
    expected = {f"{s}/t{t}" for s in ("daily", "spring", "refund_wave") for t in (1,4,16)}
    gates={}
    for scenario in ("daily","spring","refund_wave"):
        mine=measured.get("measurements",{}).get(scenario+"/t1",{}).get("quality",{}).get("satisfaction")
        ref=strong.get("measurements",{}).get(scenario+"/t1",{}).get("quality",{}).get("satisfaction")
        gates[scenario]=(mine/ref>=.85 if ref and mine is not None else mine is not None if ref==0 else None)
    per = {}
    for key in sorted(expected):
        values = [v.get("measurements", {}).get(key, {}) for v in (measured, baseline, strong)]
        gate=gates[key.split("/")[0]]
        per[key] = measurement_score(*values) if gate is True and all(v.get("ok") for v in values) else {"status":"quality_gate" if gate is False else "incorrect_or_missing", "score":-100. if gate is False else None}
    performance = statistics.mean(v["score"] for v in per.values()) if all(v["score"] is not None for v in per.values()) else None
    categories = measured.get("functional", {}).get("categories", {})
    functional = 100*statistics.mean(c["passed"]/c["total"] for c in categories.values()) if categories and all(c["total"] for c in categories.values()) else None
    preferences, memories = [], []
    for scenario in ("daily", "spring", "refund_wave"):
        mine = measured.get("measurements", {}).get(scenario+"/t1", {})
        ref = strong.get("measurements", {}).get(scenario+"/t1", {})
        if gates[scenario] is False:
            preferences.append(0.);memories.append(0.);continue
        if mine.get("ok") and ref.get("ok") and "quiet" in mine and "quiet" in ref:
            preferences.append(100*statistics.mean(mine["quiet"][k]/ref["quiet"][k] if ref["quiet"][k]>0 else 1 for k in ("window_rate","together_rate")))
        own = [measured.get("measurements",{}).get(f"{scenario}/t{t}",{}).get("engine_mb") for t in (1,4,16)]
        reference = [strong.get("measurements",{}).get(f"{scenario}/t{t}",{}).get("engine_mb") for t in (1,4,16)]
        if all(v is not None and v>0 for v in own+reference):
            ratio=max(own)/max(reference)
            memories.append(100. if ratio<=1 else max(0.,100*(1-math.log2(ratio)/math.log2(4))))
    preference = statistics.mean(preferences) if len(preferences)==3 else None
    memory = statistics.mean(memories) if len(memories)==3 else None
    management_parts=[]
    if management and management.get("evidence_passed") is True:
        # Rows must already be aggregates over the same frozen validation
        # seeds. Equal weight by family then by lambda, never by demand count.
        families={}
        for row in management.get("rows",[]):
            value=management_score(row["utility"],row["greedy"],row["reference"],True)
            if value is None: break
            families.setdefault(row["family"],[]).append(value)
        else:
            if len(families)>=2 and all(len(v)==3 for v in families.values()):
                management_parts=[statistics.mean(v) for v in families.values()]
    business=statistics.mean(management_parts) if management_parts else None
    components={"performance":performance,"functional":functional,"management":business,"preference":preference,"memory":memory}
    validation=dict(checks or {})
    validation["correctness"]=measured.get("correctness_passed") is True
    validation["management"]=business is not None
    release=release_gate(validation)
    missing=[k for k,v in components.items() if v is None]
    total=sum(components[k]*w for k,w in zip(components,(.65,.10,.12,.08,.05))) if not missing and release["publishable"] else None
    return {"schema_version":2,"abi_version":2,"status":"validated" if total is not None else "experimental",
            "total":total,**components,"measurements":per,"quality_gates":gates,"missing_components":missing,"release":release}
