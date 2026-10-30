"""A trained classifier as the rest of the system sees it: feature records
in, classification records out. The same interface as the rules in
rules.py, so both are scored by identical code."""

import json
import time
from pathlib import Path

import joblib
import numpy as np

from . import evaluate, features

MODELS = evaluate.REPO / "models"


class Model:
    """estimator is anything with predict_proba whose classes_ are the
    seven class names; normally a calibrated random forest or LightGBM.
    The random forest is tuned over key hyperparameters and selected by
    validation log loss before calibration."""

    def __init__(self, estimator, model_version):
        self.estimator, self.model_version = estimator, model_version
        # Column of each class in predict_proba's output. A class the
        # training data lacked has no column and gets probability zero.
        self._column = {cls: i for i, cls in enumerate(estimator.classes_)}

    def probabilities(self, records):
        """One row per record, columns in the taxonomy's class order,
        each row summing to 1."""
        raw = np.asarray(self.estimator.predict_proba(features.matrix(records)), dtype=np.float64)
        probs = np.zeros((len(raw), len(evaluate.CLASSES)))
        for j, cls in enumerate(evaluate.CLASSES):
            if cls in self._column:
                probs[:, j] = raw[:, self._column[cls]]
        probs = np.clip(probs, 0.0, 1.0)
        totals = probs.sum(axis=1, keepdims=True)
        # A row of zeros says nothing: every class equally likely.
        return np.where(totals > 0, probs / np.where(totals > 0, totals, 1), 1 / len(evaluate.CLASSES))

    def classify_many(self, records):
        records = list(records)
        if not records:
            return []
        started = time.perf_counter()
        probs = self.probabilities(records)
        each_us = (time.perf_counter() - started) * 1e6 / len(records)
        return [{
            "schema_version": 1, "flow_id": record["flow_id"], "model_version": self.model_version,
            "probs": dict(zip(evaluate.CLASSES, row.tolist())), "confidence": float(row.max()),
            "inference_us": each_us,
        } for record, row in zip(records, probs)]

    def classify(self, record):
        return self.classify_many([record])[0]


def save(model, directory, meta, envelopes):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    joblib.dump(model.estimator, directory / "model.joblib", compress=3)
    meta = {**meta, "model_bytes": (directory / "model.joblib").stat().st_size}
    (directory / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    (directory / "envelopes.json").write_text(json.dumps(envelopes, indent=2) + "\n")
    return meta


def load(version, models=MODELS):
    """Loads models/<version>/. Refuses a model trained on another feature
    list: its numbers would mean something else."""
    directory = Path(models) / version
    meta = json.loads((directory / "meta.json").read_text())
    if meta["features"] != features.NAMES:
        raise ValueError(f"model {version} was trained on a different feature list")
    return Model(joblib.load(directory / "model.joblib"), meta["model_version"])
