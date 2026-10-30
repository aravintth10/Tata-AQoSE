"""Export trained LightGBM trees and temperature calibration for native inference.

  python -m aqos_ml.export                 # the chosen v2 model
  python -m aqos_ml.export --fixture 2000  # shared Rust/Go reference fixture

The production Rust classifier and the Go comparison implementation consume the
same trees.json. Fixtures contain Python reference probabilities and decisions;
make test-rust checks native model and Unix-socket contract parity.
"""

import argparse
import gzip
import itertools
import json
from pathlib import Path

import numpy as np

from . import decide, evaluate, features, model
from .dataset import build as dataset

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "internal" / "classify" / "testdata" / "fixture.json.gz"


def tree(structure):
    """A tree as flat lists. A child index of n >= 0 is node n; -(i + 1)
    is leaf i."""
    nodes, leaves = [], []

    def walk(n):
        if "split_index" not in n:
            leaves.append(n["leaf_value"])
            return -len(leaves)
        if n["decision_type"] != "<=" or n["missing_type"] not in ("NaN", "None"):
            raise ValueError(f"split kind {n['decision_type']} {n['missing_type']} is not supported")
        i = len(nodes)
        nodes.append(None)
        left, right = walk(n["left_child"]), walk(n["right_child"])
        nodes[i] = [n["split_feature"], n["threshold"], bool(n["default_left"]), n["missing_type"] == "NaN", left, right]
        return i

    root = walk(structure)
    return {"root": root, "nodes": nodes, "leaves": leaves}


def export(version):
    m = model.load(version)
    calibrated = m.estimator.calibrated_classifiers_[0]
    if calibrated.method != "temperature":
        raise ValueError(f"only temperature calibration is exported, not {calibrated.method}")
    lgbm = calibrated.estimator.estimator
    dump = lgbm.booster_.dump_model()
    per = dump["num_tree_per_iteration"]
    out = {
        "model_version": version, "features": features.NAMES, "features_version": features.FEATURES_VERSION,
        "model_classes": [str(c) for c in lgbm.classes_], "classes": list(evaluate.CLASSES),
        "temperature": float(calibrated.calibrators[0].beta_),
        "trees": [{"class": i % per, **tree(t["tree_structure"])} for i, t in enumerate(dump["tree_info"])],
    }
    path = model.MODELS / version / "trees.json"
    path.write_text(json.dumps(out, separators=(",", ":")) + "\n")
    return path, m


def fixture(m, count, path=FIXTURE):
    """count records spread over the dataset, with what the Python side
    makes of them: probabilities, and the tin under each decision rule."""
    table = evaluate.load_damage_table()
    engines = {"expected_damage": decide.Engine(table), "top_label": decide.Engine(table, mode="top_label")}
    source = dataset.load(REPO / "data" / "dataset" / "v2.jsonl.gz")
    rows = list(itertools.islice(source, 0, None, 97))[:count]
    keep = ["flow_id", "host_id", "kind", "ts_ns", "ip_version", "protocol", "src_port", "dst_port",
            "pkt_sizes", "pkt_gaps_us", "window", "schema_version"]
    cases = []
    for row, c in zip(rows, m.classify_many(rows)):
        cases.append({"record": {k: row[k] for k in keep},
                      "probs": [c["probs"][k] for k in evaluate.CLASSES],
                      "tins": {mode: e.tin(c, row) for mode, e in engines.items()}})
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt") as f:
        json.dump({"model_version": m.model_version, "cases": cases}, f)
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(prog="aqos_ml.export", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", default=None)
    parser.add_argument("--fixture", type=int, default=0)
    args = parser.parse_args(argv)
    version = args.version or (model.MODELS / "chosen-v2.txt").read_text().strip()
    path, m = export(version)
    print(path.relative_to(REPO), f"{path.stat().st_size / 1e6:.2f} MB")
    if args.fixture:
        print(fixture(m, args.fixture).relative_to(REPO))


if __name__ == "__main__":
    main()
