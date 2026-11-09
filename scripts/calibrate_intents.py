"""
Calibrate and report the local intent classifier against tests/intent_validation_set.py.

Uses the REAL local embedding model (no network once cached; no Gemini). Prints, per category,
accuracy and the score distributions that justify the thresholds in intent_classifier.py.

    python scripts/calibrate_intents.py                 # report with the current thresholds
    python scripts/calibrate_intents.py --json out.json # also dump raw scores
    python scripts/calibrate_intents.py --search        # grid-search min_score/min_margin/mixed_floor
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from intent_validation_set import HOLDOUT, VALIDATION  # noqa: E402
from portfolio_agent.intent_classifier import (  # noqa: E402
    ANALYTICAL,
    DEFAULT_MIN_SCORE,
    Intent,
    IntentClassifierUnavailable,
    IntentThresholds,
    LocalIntentClassifier,
    build_default_classifier,
)


def is_correct(result_intent: Intent, expected: str) -> bool:
    exp = Intent(expected)
    if exp in ANALYTICAL:
        return result_intent in ANALYTICAL
    return result_intent == exp


def evaluate(clf: LocalIntentClassifier, cached: list) -> dict:
    stats: dict = defaultdict(lambda: [0, 0])
    errors = []
    for (text, expected, cat), scores in cached:
        res = clf.decide(scores)
        ok = is_correct(res.intent, expected)
        stats[cat][0] += ok
        stats[cat][1] += 1
        stats["ALL"][0] += ok
        stats["ALL"][1] += 1
        if not ok:
            errors.append((cat, text, expected, res.intent.value, res.reason, round(res.score, 3), round(res.margin, 3)))
    return {"stats": dict(stats), "errors": errors}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json")
    ap.add_argument("--holdout", action="store_true", help="evaluate the untouched HOLDOUT set instead of VALIDATION")
    ap.add_argument("--search", action="store_true", help="coarse grid over shared thresholds")
    ap.add_argument("--tune", action="store_true", help="per-intent threshold search (coordinate descent)")
    args = ap.parse_args()

    clf = build_default_classifier()
    if clf is None:
        print("Classifier disabled by PORTFOLIO_INTENT_CLASSIFIER=off")
        return 2
    try:
        clf.warm_up()
    except IntentClassifierUnavailable as exc:
        print(exc, file=sys.stderr)
        return 2

    cached = [((t, e, c), clf.similarities(t)) for t, e, c in (HOLDOUT if args.holdout else VALIDATION)]
    if args.json:
        Path(args.json).write_text(json.dumps([{"text": t, "expected": e, "category": c, "scores": s} for (t, e, c), s in cached], indent=2), encoding="utf-8")

    def show(label: str, thresholds: IntentThresholds) -> dict:
        clf.thresholds = thresholds
        r = evaluate(clf, cached)
        print(f"\n== {label}: min_margin={thresholds.min_margin} mixed_floor={thresholds.mixed_floor} pair={thresholds.analytic_pair_margin}")
        print("   min_score:", dict(thresholds.min_score))
        for cat in ["positive", "hard_negative", "mixed", "ambiguous", "regression", "ALL"]:
            if cat in r["stats"]:
                ok, n = r["stats"][cat]
                print(f"   {cat:<14}{ok}/{n}  ({ok / n:.0%})")
        return r

    base = IntentThresholds()
    r = show("current thresholds", base)
    for cat, text, exp, got, reason, score, margin in r["errors"]:
        print(f"   MISS [{cat}] {text!r}: expected {exp}, got {got} ({reason}, score {score}, margin {margin})")

    print("\n-- Winning-intent score distribution (correct positives), per expected intent --")
    per = defaultdict(list)
    for (t, e, c), scores in cached:
        if c == "positive":
            per[e].append(scores[e])
    for e, v in sorted(per.items()):
        print(f"   {e:<20} min {min(v):.3f}  median {sorted(v)[len(v)//2]:.3f}  max {max(v):.3f}")
    print("\n-- Best score of wrong intents on positives/hard negatives (what the threshold must exceed) --")
    wrong = defaultdict(list)
    for (t, e, c), scores in cached:
        if c in ("positive", "hard_negative") and Intent(e) not in ANALYTICAL:
            for k, v in scores.items():
                if k != e and Intent(k) not in ANALYTICAL:
                    wrong[k].append(v)
    for k, v in sorted(wrong.items()):
        print(f"   {k:<20} max wrong-intent score {max(v):.3f}")

    if args.search:
        best = None
        grid = itertools.product([0.45, 0.5, 0.55, 0.6, 0.65], [0.0, 0.02, 0.04, 0.06, 0.08], [0.45, 0.5, 0.55, 0.6, 0.65, 0.7])
        for ms, mm, mf in grid:
            th = IntentThresholds(min_score={k: ms for k in DEFAULT_MIN_SCORE}, min_margin=mm, mixed_floor=mf)
            clf.thresholds = th
            r = evaluate(clf, cached)
            ok, n = r["stats"]["ALL"]
            # prefer fewer wrong-class decisions (an UNKNOWN is a safe miss; a wrong class is not)
            wrong_class = sum(1 for e in r["errors"] if e[3] != "UNKNOWN")
            key = (wrong_class, -ok)
            if best is None or key < best[0]:
                best = (key, ms, mm, mf, ok, n)
        print(f"\nGrid search best: min_score={best[1]} min_margin={best[2]} mixed_floor={best[3]} -> {best[4]}/{best[5]} correct, {best[0][0]} wrong-class")
    if args.tune:
        def objective(th: IntentThresholds):
            clf.thresholds = th
            r = evaluate(clf, cached)
            wrong_class = sum(1 for e in r["errors"] if e[3] != "UNKNOWN" and e[2] != "UNKNOWN") + sum(1 for e in r["errors"] if e[2] == "UNKNOWN")
            return (wrong_class, len(r["errors"]))

        grid = [round(0.30 + 0.025 * i, 3) for i in range(0, 21)]
        best_overall = None
        for mm, mf in itertools.product([0.02, 0.04, 0.06, 0.08], [0.50, 0.55, 0.60, 0.65, 0.70]):
            ms = {k: 0.5 for k in DEFAULT_MIN_SCORE}
            for _ in range(2):
                for intent in DEFAULT_MIN_SCORE:
                    scored = {}
                    for v in grid:
                        scored[v] = objective(IntentThresholds(min_score={**ms, intent: v}, min_margin=mm, mixed_floor=mf))
                    top = min(scored.values())
                    plateau = sorted(v for v, o in scored.items() if o == top)
                    ms[intent] = plateau[len(plateau) // 2]  # middle of the best plateau = most robust choice
            key = objective(IntentThresholds(min_score=ms, min_margin=mm, mixed_floor=mf))
            if best_overall is None or key < best_overall[0]:
                best_overall = (key, ms, mm, mf)
        key, ms, mm, mf = best_overall
        print(f"\nTuned: min_margin={mm} mixed_floor={mf} wrong-class={key[0]} total-errors={key[1]}")
        print("min_score =", json.dumps(ms))
    return 0


if __name__ == "__main__":
    sys.exit(main())
