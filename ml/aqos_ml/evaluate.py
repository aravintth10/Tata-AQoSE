"""One way to score any classifier, the trained models and the hand-written
rules alike. Whatever is compared must go through here, so that no method
is judged by a kinder measure than another.
"""

import json
from collections import Counter, defaultdict
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
TAXONOMY = json.loads((REPO / "contracts" / "taxonomy.json").read_text())
CLASSES = list(TAXONOMY["classes"])
TINS = list(TAXONOMY["tins"])
ECE_BINS = 10


def load_damage_table(path=REPO / "configs" / "damage_table.yaml"):
    """Returns {true class: {tin: damage}}, checked for completeness."""
    table = yaml.safe_load(Path(path).read_text())["damage"]
    for cls in CLASSES:
        missing = set(TINS) - set(table.get(cls, {}))
        if missing:
            raise ValueError(f"damage table has no entry for {cls} in {sorted(missing)}")
    return table


def top_label_tin(classification, record=None):
    """The tin of the most likely class: decision rule O1. Ties go to the
    class listed first in the taxonomy, so the answer never depends on
    dictionary order."""
    probs = classification["probs"]
    best = max(CLASSES, key=lambda c: (probs[c], -CLASSES.index(c)))
    return TAXONOMY["classes"][best]["tin"]


def score(classifier, rows, damage, decide=top_label_tin):
    """Scores a classifier on labelled rows.

    classifier.classify(record) returns a classification record; decide
    turns that into a tin. rows are feature records with a `label`.

    Every flow counts once, however many records it has: a long flow yields
    a window record every few seconds, and counting records would let a few
    long flows decide the score. A flow's records share its weight equally.

    Calibration is measured on the most likely class (ece, reliability).

    expected_damage is the mean over flows of the damage table's entry for
    the flow's true class and the tin chosen. expected_damage_balanced is
    the mean of that over classes, each class counting the same. The
    dataset's mix of classes is an accident of what could be collected, not
    what a home carries, so the balanced figure is the one to tune and
    choose by.
    """
    rows = list(rows)
    per_flow = Counter((row.get("session"), row["flow_id"]) for row in rows)
    confusion = defaultdict(lambda: defaultdict(float))
    tins = defaultdict(lambda: defaultdict(float))
    class_damage = defaultdict(float)
    class_flows = defaultdict(float)
    # One call for all rows where the classifier offers it: a tree model
    # asked row by row spends its time on overhead.
    many = getattr(classifier, "classify_many", None)
    classifications = many(rows) if many else [classifier.classify(row) for row in rows]
    reliability = [[0.0, 0.0, 0.0] for _ in range(ECE_BINS)]  # weight, confidence, correct
    for row, classification in zip(rows, classifications):
        weight = 1 / per_flow[row.get("session"), row["flow_id"]]
        probs = classification["probs"]
        predicted = max(CLASSES, key=lambda c: (probs[c], -CLASSES.index(c)))
        tin = decide(classification, row)
        cell = reliability[min(int(probs[predicted] * ECE_BINS), ECE_BINS - 1)]
        cell[0] += weight
        cell[1] += weight * probs[predicted]
        cell[2] += weight * (predicted == row["label"])
        confusion[row["label"]][predicted] += weight
        tins[row["label"]][tin] += weight
        class_damage[row["label"]] += weight * damage[row["label"]][tin]
        class_flows[row["label"]] += weight
    flows = sum(class_flows.values())
    recall, precision = {}, {}
    for cls in CLASSES:
        actual = class_flows[cls]
        # .get, so that reading does not add empty entries to the tables.
        right = confusion[cls].get(cls, 0.0) if cls in confusion else 0.0
        claimed = sum(confusion[other].get(cls, 0.0) for other in list(confusion))
        recall[cls] = right / actual if actual else None
        precision[cls] = right / claimed if claimed else None
    f1 = [2 * p * r / (p + r) if p and r else 0.0
          for p, r in ((precision[c], recall[c]) for c in CLASSES) if r is not None]
    by_class = {c: class_damage[c] / class_flows[c] for c in CLASSES if class_flows[c]}
    rounded = lambda table: {c: {k: round(v, 3) for k, v in table[c].items()} for c in CLASSES if table[c]}  # noqa: E731
    return {
        "model_version": getattr(classifier, "model_version", None),
        "rows": len(rows),
        "flows": round(flows),
        "expected_damage": sum(class_damage.values()) / flows if flows else None,
        "expected_damage_balanced": sum(by_class.values()) / len(by_class) if by_class else None,
        "damage_by_class": by_class,
        "accuracy": sum(confusion[c].get(c, 0.0) for c in list(confusion)) / flows if flows else None,
        "macro_f1": sum(f1) / len(f1) if f1 else None,
        # Expected calibration error of the top label: over ten bins of
        # confidence, the mean gap between how sure the classifier was and
        # how often it was right. Zero for "70% sure" being right 70% of
        # the time.
        "ece": sum(abs(c[1] - c[2]) for c in reliability) / flows if flows else None,
        "reliability": [{"confidence": round(c[1] / c[0], 4), "accuracy": round(c[2] / c[0], 4),
                         "flows": round(c[0], 2)} if c[0] else None for c in reliability],
        "recall": recall,
        "precision": precision,
        "confusion": rounded(confusion),
        "tins": rounded(tins),
    }
