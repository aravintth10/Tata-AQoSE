"""The hand-written threshold rules that existing tools use (qosify-style):
configuration B4, and the method the classifier has to beat.

    A flow whose packet rate stays above a threshold is bulk.
    A flow whose average packet length stays below a threshold is priority.
    Everything else is default.

The rules sit behind the same interface as the trained model and return the
same classification record, with all the probability on one class, so both
are scored by identical code.
"""

import itertools
import time

from . import evaluate

RULES_VERSION = "qosify-rules-v1"
# What each verdict means in our classes. Their tins are Bulk, Voice and
# Best Effort, as in qosify.
BULK, PRIORITY, DEFAULT = "low_priority_data", "realtime_interactive", "standard"


class Rules:
    """bulk_pps: packets per second, both directions, above which a flow is
    bulk. priority_bytes: average packet size below which a flow is
    priority. The starting values are qosify's own defaults."""

    def __init__(self, bulk_pps=200.0, priority_bytes=450.0):
        self.bulk_pps, self.priority_bytes = bulk_pps, priority_bytes
        self.model_version = RULES_VERSION

    def verdict(self, record):
        window = record.get("window")
        if not window:
            # Rules need a rate. Until a full second has been counted there
            # is none, and the flow is default.
            return DEFAULT
        pps = window["pkts_up_per_s"] + window["pkts_down_per_s"]
        if pps <= 0:
            return DEFAULT
        if pps > self.bulk_pps:
            return BULK
        mean_bytes = (window["bytes_up_per_s"] + window["bytes_down_per_s"]) / pps
        if mean_bytes < self.priority_bytes:
            return PRIORITY
        return DEFAULT

    def classify(self, record):
        started = time.perf_counter()
        chosen = self.verdict(record)
        return {
            "schema_version": 1,
            "flow_id": record["flow_id"],
            "model_version": self.model_version,
            "probs": {cls: 1.0 if cls == chosen else 0.0 for cls in evaluate.CLASSES},
            "confidence": 1.0,
            "inference_us": (time.perf_counter() - started) * 1e6,
        }


BULK_GRID = [25, 50, 100, 150, 200, 300, 400, 600, 800, 1200, 2000, 4000]
PRIORITY_GRID = [0, 100, 150, 200, 300, 450, 600, 800, 1000, 1200]


def tune(rows, damage):
    """Finds the two thresholds with the lowest class-balanced expected
    damage on rows, the same data and the same measure the model is tuned on. Without this the
    comparison would be against rules nobody tried to make good.

    Returns (rules, score, all tried as [(bulk_pps, priority_bytes, damage)]).
    """
    rows = list(rows)
    tried = []
    for bulk_pps, priority_bytes in itertools.product(BULK_GRID, PRIORITY_GRID):
        result = evaluate.score(Rules(bulk_pps, priority_bytes), rows, damage)
        tried.append((bulk_pps, priority_bytes, result["expected_damage_balanced"]))
    # Ties go to the thresholds nearest qosify's defaults in the grid order.
    bulk_pps, priority_bytes, _ = min(tried, key=lambda t: (t[2], abs(t[0] - 200), abs(t[1] - 450)))
    best = Rules(bulk_pps, priority_bytes)
    return best, evaluate.score(best, rows, damage), tried


def main(argv=None):
    """python -m aqos_ml.rules --dataset data/dataset/v1.jsonl.gz

    Tunes the two thresholds on the training split and writes them to
    configs/rules.yaml, which the live B4 configuration reads. Without that
    file B4 runs qosify's defaults.
    """
    import argparse

    import yaml

    from .dataset import build as dataset

    parser = argparse.ArgumentParser(prog="aqos_ml.rules", description=main.__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", default=evaluate.REPO / "data" / "dataset" / "v1.jsonl.gz")
    parser.add_argument("--damage", help="damage table (default configs/damage_table.yaml)")
    parser.add_argument("--out", default=evaluate.REPO / "configs" / "rules.yaml")
    args = parser.parse_args(argv)
    damage = evaluate.load_damage_table(args.damage) if args.damage else evaluate.load_damage_table()
    rows = list(dataset.load(args.dataset, splits={"train"}))
    best, tuned_score, _ = tune(rows, damage)
    default_score = evaluate.score(Rules(), rows, damage)
    with open(args.out, "w") as f:
        f.write("# The threshold rules' two thresholds, tuned on the training split by\n"
                "# `python -m aqos_ml.rules`. Read by the live B4 configuration.\n")
        yaml.safe_dump({
            "bulk_pps": float(best.bulk_pps), "priority_bytes": float(best.priority_bytes),
            "dataset": str(args.dataset), "train_flows": tuned_score["flows"],
            "balanced_damage_tuned": round(tuned_score["expected_damage_balanced"], 4),
            "balanced_damage_qosify_defaults": round(default_score["expected_damage_balanced"], 4),
        }, f, sort_keys=False)
    print(f"bulk above {best.bulk_pps:g} packets/s, priority below {best.priority_bytes:g} bytes: balanced damage "
          f"{tuned_score['expected_damage_balanced']:.3f} (qosify's defaults: {default_score['expected_damage_balanced']:.3f})")
    print(f"written to {args.out}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
