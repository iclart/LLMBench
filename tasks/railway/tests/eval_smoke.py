#!/usr/bin/env python3
"""Smoke test for eval/railway_eval.py on a tiny season.

Prepares a season, then checks that the baseline submission evaluates
cleanly, that the do-nothing stub is rejected scenario by scenario, that an
engine refusing every sale (Withheld) is legal but scores 0 under the
satisfaction gate, and that a submission shadowing ticket_system.h does not
compile.
"""
import argparse
import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK / "eval"))
import scoring  # noqa: E402

FAILED_TOTAL = scoring.PERF_WEIGHT * scoring.PERF_FLOOR  # every measurement failed, nothing else scored


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--strong", action="store_true", help="the strong reference is available")
    a = ap.parse_args()
    work = Path(a.work)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    ev = [sys.executable, str(TASK / "eval/railway_eval.py"), "--build", a.build, "--sandbox", "none"]
    engine = "strong" if a.strong else "baseline"

    def run(*args, expect_ok=True):
        p = subprocess.run(ev + list(args), capture_output=True, text=True)
        if expect_ok and p.returncode != 0:
            sys.exit(f"railway_eval {' '.join(args[:1])} failed ({p.returncode}):\n{p.stderr[-4000:]}")
        return p

    season = work / "season"
    run("prepare", "--season", str(season), "--configs", str(TASK / "configs/public"), "--seed", "5",
        "--set", "intents=2000", "--set", "quiet_intents=200", "--anchor-repeats", "1",
        "--answers-engine", engine, "--strong-engine", engine)

    subs = work / "subs"
    (subs / "baseline").mkdir(parents=True)
    shutil.copy(TASK / "reference/baseline/baseline.cpp", subs / "baseline")
    (subs / "stub").mkdir()
    shutil.copy(TASK / "stub/solution.cpp", subs / "stub")
    (subs / "withhold").mkdir()
    shutil.copy(TASK / "reference/baseline/baseline.cpp", subs / "withhold/baseline.inc")
    (subs / "withhold/withhold.cpp").write_text(textwrap.dedent('''\
        // Sells nothing: books, then refunds and answers Withheld.
        #define create_ticket_system create_baseline_ticket_system
        #include "baseline.inc"
        #undef create_ticket_system
        namespace rail {
        class Withholding final : public TicketSystem {
         public:
          void init(const WorldData& w) override { inner_->init(w); }
          Status query(const QueryRequest& q, QueryResult* r) override { return inner_->query(q, r); }
          Status refund(uint64_t id) override { return inner_->refund(id); }
          Status book(const BookRequest& b, BookResult* r) override {
            const Status st = inner_->book(b, r);
            if (st != Status::Ok) return st;
            inner_->refund(r->order_id);
            return Status::Withheld;
          }
         private:
          TicketSystem* inner_ = create_baseline_ticket_system();
        };
        TicketSystem* create_ticket_system() { return new Withholding(); }
        }  // namespace rail
        '''))
    (subs / "shadow").mkdir()
    shutil.copy(TASK / "stub/solution.cpp", subs / "shadow")
    shutil.copy(TASK / "include/ticket_system.h", subs / "shadow")

    run("evaluate", "--season", str(season), "--src", str(subs / "baseline"), "--out", str(work / "baseline.json"),
        "--lite")
    r = json.loads((work / "baseline.json").read_text())
    if r["fatal"] or any(s.get("failure") for s in r["scenarios"].values()):
        sys.exit(f"baseline submission should pass: {r['fatal']} {r['scenarios']}")
    if not all(g and g["passed"] for g in r["scores"]["gates"].values()) or r["scores"]["transport"] <= 0:
        sys.exit(f"the baseline must pass the satisfaction gate and carry passengers: {r['scores']['gates']}")
    if r["scores"]["functional"] != 100 or r["scores"]["memory"] <= 0:
        sys.exit(f"the baseline must pass the functional suite and score on memory: {r['functional']}, "
                 f"memory {r['scores']['memory_mb']}")

    run("evaluate", "--season", str(season), "--src", str(subs / "stub"), "--out", str(work / "stub.json"), "--lite")
    r = json.loads((work / "stub.json").read_text())
    failed = [sc for sc, s in r["scenarios"].items() if s.get("failure")]
    # Failed measurements score the performance floor, so the total is negative.
    if len(failed) != 3 or abs(r["scores"]["total"] - FAILED_TOTAL) > 1e-9 or r["scores"]["functional"] != 0:
        sys.exit(f"stub should fail every scenario and every functional category: {r['scenarios']}, "
                 f"functional {r['scores']['functional']}")

    run("evaluate", "--season", str(season), "--src", str(subs / "withhold"), "--out", str(work / "withhold.json"),
        "--lite")
    r = json.loads((work / "withhold.json").read_text())
    if r["fatal"] or any(s.get("failure") for s in r["scenarios"].values()):
        sys.exit(f"withholding is legal and must pass the judge: {r['fatal']} {r['scenarios']}")
    gates = r["scores"]["gates"]
    # Refusing to sell is legal, so only the functional part (not tied to a scenario) remains,
    # on top of the performance floor of gated scenarios.
    s = r["scores"]
    if any(g is None or g["passed"] for g in gates.values()) or abs(s["total"] - (s["functional"] * 0.10 + FAILED_TOTAL)) > 1e-9:
        sys.exit(f"refusing every sale must fail the satisfaction gate everywhere: {gates}, total {s['total']}")

    run("evaluate", "--season", str(season), "--src", str(subs / "shadow"), "--out", str(work / "shadow.json"),
        "--lite", expect_ok=False)
    r = json.loads((work / "shadow.json").read_text())
    if r["fatal"] != "compile_error":
        sys.exit(f"a submission with its own ticket_system.h must be rejected, got {r['fatal']}")
    print("railway_eval smoke test passed")


if __name__ == "__main__":
    main()
