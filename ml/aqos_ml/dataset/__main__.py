"""Dataset v1.

  python -m aqos_ml.dataset scenarios --count 48 --out scenarios/train
  python -m aqos_ml.run matrix scenarios/train/matrix.yaml --results results-train
  python -m aqos_ml.dataset build

`scenarios` writes randomised training scenarios and a matrix file that runs
them with the live extractor on. `build` joins the public flow tables under
data/public/ with those runs into data/dataset/v1.jsonl.gz, splits it by
session, and prints what each class has.
"""

import argparse
import sys
from pathlib import Path

import yaml

from . import build as builder
from . import scenarios

REPO = builder.REPO


def main(argv=None):
    parser = argparse.ArgumentParser(prog="aqos_ml.dataset", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("scenarios")
    p.add_argument("--count", type=int, default=48)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--out", type=Path, default=REPO / "scenarios" / "train")
    p = sub.add_parser("build")
    p.add_argument("--public", type=Path, default=REPO / "data" / "public", help="searched for flows*.jsonl.gz")
    p.add_argument("--runs", type=Path, default=REPO / "results-train")
    p.add_argument("--own", type=Path, default=REPO / "data" / "raw", help="our own capture sessions")
    p.add_argument("--out", type=Path, default=REPO / "data" / "dataset")
    p.add_argument("--name", default="v1")
    args = parser.parse_args(argv)

    if args.command == "scenarios":
        args.out.mkdir(parents=True, exist_ok=True)
        paths = []
        for i in range(args.count):
            scenario = scenarios.make(i, args.seed)
            path = args.out / f"{scenario['id']}.yaml"
            path.write_text(yaml.safe_dump(scenario, sort_keys=False))
            paths.append(str(path.relative_to(REPO)) if path.is_relative_to(REPO) else str(path))
        # With CAKE and without, so first packets are seen in both regimes.
        matrix = {"scenarios": paths, "configs": ["B2", "B0"], "seeds": [args.seed], "tag": "train", "extract": True}
        (args.out / "matrix.yaml").write_text(yaml.safe_dump(matrix, sort_keys=False))
        print(f"wrote {len(paths)} scenarios and matrix.yaml to {args.out} ({2 * len(paths)} runs)")
        return 0

    tables = sorted(args.public.rglob("flows*.jsonl.gz"))
    # `current` is the runner's link to its latest run, which is also listed
    # under its own name: following it would count that run twice.
    runs = sorted(p.parent for p in args.runs.glob("*/features.jsonl")
                  if not p.parent.is_symlink()) if args.runs.exists() else []
    own = sorted(args.own.glob("*.json")) if args.own.exists() else []
    meta = builder.build(tables, runs, args.out, args.name, own_sessions=own)
    print(f"\n{'class':26}" + "".join(f"{s:>22}" for s in builder.SPLITS))
    print(f"{'':26}" + "".join(f"{'sess  flows  windows':>22}" for _ in builder.SPLITS))
    from ..evaluate import CLASSES
    for cls in CLASSES:
        cells = meta["summary"].get(cls, {})
        print(f"{cls:26}" + "".join(
            f"{'{:4d} {:6d} {:8d}'.format(*cells[s]) if s in cells else '-':>22}" for s in builder.SPLITS))
    if meta["left_out"]:
        print("\nleft out:", ", ".join(f"{n} {why}" for why, n in meta["left_out"].items()))
    for problem in meta["problems"]:
        print("NOT READY:", problem)
    print(f"\nwrote {args.out / (args.name + '.jsonl.gz')}, {args.name}.test.jsonl.gz (do not open before the report) and {args.name}.json")
    return 1 if meta["problems"] else 0


if __name__ == "__main__":
    sys.exit(main())
