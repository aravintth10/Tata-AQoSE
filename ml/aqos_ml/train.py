"""Trains the classifier (chunk 5A).

  python -m aqos_ml.train --dataset data/dataset/v1.jsonl.gz

Two models are trained on the train split: a random forest tuned over
key hyperparameters, and LightGBM tuned over a few settings. Each is
calibrated on the calibration split, three ways, and the calibration
that gives the lowest expected damage on validation is kept. The model
with the lower class-balanced expected damage on validation wins, the
forest on a tie.

The test split is never read here. It is opened once, for the report.

Writes models/<version>/ with the model, meta.json (how it was made and how
it scored) and envelopes.json (each class's normal range of the window
statistics, for the reclassifier).
"""

import argparse
import hashlib
import json
import platform
import sys
import time
import warnings
from collections import Counter
from pathlib import Path

import lightgbm
import numpy as np
import sklearn
from lightgbm import LGBMClassifier
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import RandomForestClassifier
from sklearn.frozen import FrozenEstimator

from . import decide, evaluate, features, model, rules
from .dataset import build as dataset

CALIBRATIONS = ("sigmoid", "isotonic", "temperature")
# What LightGBM is tuned over.
SEARCH = {
    "num_leaves": [15, 31, 63, 127],
    "learning_rate": [0.03, 0.05, 0.1],
    "min_child_samples": [5, 10, 20, 40],
    "colsample_bytree": [0.6, 0.8, 1.0],
    "reg_lambda": [0.0, 1.0, 5.0],
}


def weights(rows):
    """Each flow counts once however many records it has, and each class
    counts the same however many flows it has: the dataset's mix is an
    accident of what could be collected."""
    per_flow = Counter((r["session"], r["flow_id"]) for r in rows)
    w = np.array([1 / per_flow[r["session"], r["flow_id"]] for r in rows])
    labels = np.array([r["label"] for r in rows])
    for cls in set(labels):
        w[labels == cls] /= w[labels == cls].sum()
    return w * len(rows) / w.sum()


class Split:
    def __init__(self, rows):
        self.rows = rows
        self.X = features.matrix(rows)
        self.y = np.array([r["label"] for r in rows])
        self.w = weights(rows) if rows else np.array([])


# Random forest hyperparameter search space.
RF_SEARCH = {
    "n_estimators": [200, 300, 500],
    "max_depth": [16, 24, 32, None],
    "min_samples_leaf": [1, 2, 4],
    "max_features": ["sqrt", "log2", 0.5],
    "class_weight": ["balanced", "balanced_subsample", None],
}


def forest(train, validation, seed, jobs, trials=12, report=print):
    """Random forest with hyperparameters searched over RF_SEARCH.
    Selects the setting with the lowest validation log loss."""
    from sklearn.metrics import log_loss
    rng = np.random.default_rng(seed)
    best_est, best_loss = None, float("inf")
    for _ in range(trials):
        params = {k: v[int(rng.integers(len(v)))] for k, v in RF_SEARCH.items()}
        est = RandomForestClassifier(n_jobs=jobs, random_state=seed, **params)
        est.fit(train.X, train.y, sample_weight=train.w)
        loss = log_loss(validation.y, est.predict_proba(validation.X))
        report(f"  rf {params} -> log_loss {loss:.4f}")
        if loss < best_loss:
            best_est, best_loss, best_params = est, loss, params
    report(f"  rf best: {best_params}")
    # One thread from here on: the service classifies one record at a time.
    return best_est.set_params(n_jobs=1), best_params


def boosted(train, validation, params, seed, jobs):
    """LightGBM with the number of trees set by early stopping on the
    validation split's log loss."""
    est = LGBMClassifier(objective="multiclass", n_estimators=600, subsample=0.8, subsample_freq=1,
                         n_jobs=jobs, random_state=seed, verbose=-1, **params)
    with warnings.catch_warnings():
        # eval_set is deprecated in favour of eval_X and eval_y, which in
        # LightGBM 4.7 do not accept class names as labels.
        warnings.filterwarnings("ignore", message="The argument 'eval_set' is deprecated")
        est.fit(train.X, train.y, sample_weight=train.w,
                eval_set=[(validation.X, validation.y)], eval_sample_weight=[validation.w],
                callbacks=[lightgbm.early_stopping(30, verbose=False)])
    return est.set_params(n_jobs=1)


def calibrated(est, calibration, method):
    wrapped = CalibratedClassifierCV(FrozenEstimator(est), method=method)
    with warnings.catch_warnings():
        # scikit-learn warns that the frozen model is not given the weights.
        # It is already trained; the weights are for the calibration only.
        warnings.filterwarnings("ignore", message="Since FrozenEstimator does not appear to accept sample_weight")
        return wrapped.fit(calibration.X, calibration.y, sample_weight=calibration.w)


def envelopes(train):
    """For each class and window statistic, the 5th and 95th percentile
    over that class's training records that have a window."""
    out = {}
    first = features.NAMES.index(features.WINDOW[0])
    for cls in evaluate.CLASSES:
        block = train.X[train.y == cls][:, first:first + len(features.WINDOW)]
        block = block[~np.isnan(block).any(axis=1)]
        if len(block):
            low, high = np.percentile(block, [5, 95], axis=0)
            out[cls] = {name: [float(a), float(b)] for name, a, b in zip(features.WINDOW, low, high)}
    return out


def single_record_us(candidate, rows, repeats=200):
    """Median time to classify one record on its own, as the service does."""
    rows = rows[:repeats]
    times = []
    for row in rows:
        started = time.perf_counter()
        candidate.classify(row)
        times.append((time.perf_counter() - started) * 1e6)
    return float(np.median(times))


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def brief(result):
    return {k: result[k] for k in ("expected_damage_balanced", "expected_damage", "accuracy", "macro_f1", "ece",
                                   "damage_by_class", "recall", "precision")}


def train(dataset_path, models=model.MODELS, damage_path=None, seed=1, jobs=4, trials=12, report=print):
    dataset_path = Path(dataset_path)
    name = dataset_path.name.removesuffix(".jsonl.gz")
    digest = sha256(dataset_path)
    damage_path = Path(damage_path or evaluate.REPO / "configs" / "damage_table.yaml")
    damage = evaluate.load_damage_table(damage_path)
    engine = decide.Engine(damage)

    rows = {s: [] for s in ("train", "calibration", "validation")}
    for row in dataset.load(dataset_path, splits=set(rows)):
        rows[row["split"]].append(row)
    splits = {s: Split(r) for s, r in rows.items()}
    train_, cal, val = splits["train"], splits["calibration"], splits["validation"]
    for s, split in splits.items():
        missing = set(evaluate.CLASSES) - set(split.y)
        if missing:
            raise SystemExit(f"the {s} split has no {sorted(missing)}; the dataset cannot train a model")
        report(f"{s:12} {len(split.rows):7} records")

    def judge(est, version):
        return evaluate.score(model.Model(est, version), val.rows, damage, decide=engine.tin)

    def best_calibration(est, kind):
        """The calibration that decides best on validation: lowest
        class-balanced expected damage, calibration error breaking a tie.

        The first version chose by calibration error alone. That error is
        an average over flows, most of which are ordinary web traffic, and
        on a draft dataset it picked the calibration that decided worst
        (0.29 against 0.21). What the probabilities are for is the
        decision, so the decision is what they are judged by."""
        tried = {}
        for method in CALIBRATIONS:
            try:
                wrapped = calibrated(est, cal, method)
            except (ValueError, TypeError) as err:  # a method this scikit-learn lacks
                report(f"  {kind} {method}: skipped ({err})")
                continue
            tried[method] = (wrapped, judge(wrapped, kind))
            report(f"  {kind} {method:11} ece {tried[method][1]['ece']:.4f}  "
                   f"balanced damage {tried[method][1]['expected_damage_balanced']:.4f}")
        method = min(tried, key=lambda m: (round(tried[m][1]["expected_damage_balanced"], 4), tried[m][1]["ece"]))
        return method, tried[method][0], {m: brief(r) for m, (_, r) in tried.items()}

    candidates = {}
    report("random forest")
    started = time.perf_counter()
    rf, rf_params = forest(train_, val, seed, jobs, trials, report)
    method, wrapped, tried = best_calibration(rf, "rf")
    candidates["rf"] = {"estimator": wrapped, "calibration": method, "calibrations": tried,
                        "uncalibrated": brief(judge(rf, "rf")), "fit_s": round(time.perf_counter() - started, 1),
                        "params": rf_params}

    report("LightGBM")
    started = time.perf_counter()
    rng, searched, best = np.random.default_rng(seed), [], None
    for _ in range(trials):
        params = {k: v[int(rng.integers(len(v)))] for k, v in SEARCH.items()}
        est = boosted(train_, val, params, seed, jobs)
        result = judge(est, "lgbm")
        searched.append({**params, "trees": int(est.best_iteration_ or est.n_estimators),
                         "expected_damage_balanced": result["expected_damage_balanced"]})
        report(f"  {params} -> {result['expected_damage_balanced']:.4f}")
        if best is None or result["expected_damage_balanced"] < best[2]["expected_damage_balanced"]:
            best = (est, params, result)
    method, wrapped, tried = best_calibration(best[0], "lgbm")
    candidates["lgbm"] = {"estimator": wrapped, "calibration": method, "calibrations": tried,
                          "uncalibrated": brief(best[2]), "fit_s": round(time.perf_counter() - started, 1),
                          "params": {**best[1], "trees": int(best[0].best_iteration_ or 0)}, "search": searched}

    for kind, c in candidates.items():
        c["version"] = f"{kind}-{name}-{digest[:8]}"
        c["model"] = model.Model(c["estimator"], c["version"])
        c["validation"] = evaluate.score(c["model"], val.rows, damage, decide=engine.tin)
        c["validation_top_label"] = brief(evaluate.score(
            c["model"], val.rows, damage, decide=decide.Engine(damage, mode="top_label").tin))
        c["single_record_us"] = single_record_us(c["model"], val.rows)
    # The forest on a tie: the simpler, more interpretable model is preferred.
    chosen = min(candidates, key=lambda k: (round(candidates[k]["validation"]["expected_damage_balanced"], 4), k != "rf"))

    # The rules, tuned on the same training data and judged on the same
    # validation data, for a first look. The real comparison is chunk 6B.
    tuned, _, _ = rules.tune(train_.rows, damage)
    rules_result = evaluate.score(tuned, val.rows, damage,
                                  decide=decide.Engine(damage, mode="top_label", bulk_guard=False).tin)

    importances = None
    inner = candidates[chosen]["estimator"].calibrated_classifiers_[0].estimator
    inner = getattr(inner, "estimator", inner)
    if hasattr(inner, "feature_importances_"):
        total = float(np.sum(inner.feature_importances_)) or 1.0
        importances = {n: round(float(v) / total, 4) for n, v in sorted(
            zip(features.NAMES, inner.feature_importances_), key=lambda t: -t[1])}

    written = {}
    for kind, c in candidates.items():
        meta = {
            "model_version": c["version"], "kind": kind, "chosen": kind == chosen,
            "schema_version": 1, "features_version": features.FEATURES_VERSION, "features": features.NAMES,
            "classes": evaluate.CLASSES,
            "dataset": {"name": name, "sha256": digest, "records": {s: len(r) for s, r in rows.items()}},
            "damage_table": {"file": damage_path.name, "sha256": sha256(damage_path)},
            "seed": seed, "params": c["params"], "calibration": c["calibration"],
            "calibrations_tried": c["calibrations"], "uncalibrated": c["uncalibrated"],
            "search": c.get("search"), "fit_s": c["fit_s"], "single_record_us": round(c["single_record_us"], 1),
            "validation": {k: v for k, v in c["validation"].items() if k != "model_version"},
            "validation_top_label": c["validation_top_label"],
            "rules_on_validation": {"bulk_pps": tuned.bulk_pps, "priority_bytes": tuned.priority_bytes,
                                    **brief(rules_result)},
            "feature_importance": importances if kind == chosen else None,
            "libraries": {"python": platform.python_version(), "scikit-learn": sklearn.__version__,
                          "lightgbm": lightgbm.__version__, "numpy": np.__version__},
        }
        written[kind] = model.save(c["model"], Path(models) / c["version"], meta, envelopes(train_))

    report("")
    report(f"{'':22}{'balanced damage':>16}{'accuracy':>10}{'macro-F1':>10}{'ECE':>8}{'µs/record':>11}{'MB':>7}")
    for kind, c in candidates.items():
        v = c["validation"]
        report(f"{c['version']:22}{v['expected_damage_balanced']:16.4f}{v['accuracy']:10.3f}{v['macro_f1']:10.3f}"
               f"{v['ece']:8.4f}{c['single_record_us']:11.0f}{written[kind]['model_bytes'] / 1e6:7.1f}"
               + ("   <- chosen" if kind == chosen else ""))
    report(f"{'rules, tuned':22}{rules_result['expected_damage_balanced']:16.4f}{rules_result['accuracy']:10.3f}"
           f"{rules_result['macro_f1']:10.3f}")
    (Path(models) / f"chosen-{name}.txt").write_text(candidates[chosen]["version"] + "\n")
    return candidates[chosen]["version"], written


def main(argv=None):
    parser = argparse.ArgumentParser(prog="aqos_ml.train", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", default=evaluate.REPO / "data" / "dataset" / "v1.jsonl.gz")
    parser.add_argument("--models", default=model.MODELS)
    parser.add_argument("--damage", help="damage table to select by (default configs/damage_table.yaml)")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--trials", type=int, default=12, help="LightGBM settings to try")
    args = parser.parse_args(argv)
    version, _ = train(args.dataset, args.models, args.damage, args.seed, args.jobs, args.trials)
    print(f"chosen: {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
