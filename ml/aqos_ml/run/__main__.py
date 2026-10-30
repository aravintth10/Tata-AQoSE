"""Experiment runner.

  python -m aqos_ml.run one --scenario S1 --config B2 --seed 7
  python -m aqos_ml.run matrix matrix.yaml
  python -m aqos_ml.run show results/S1-B2-s0007
  python -m aqos_ml.run table --results results-live --out reports/live.md

`table` puts the runs in a directory side by side, one column per
configuration. `one` performs a single run and prints a short summary. `matrix` performs
every combination in a file and skips those that already have a valid
result, so it can be interrupted and started again. A matrix file:

  scenarios: [S1]
  configs: [B0, B2]
  seeds: {from: 0, to: 9}
  tag: null
  extract: false   # true also runs the live extractor, for training data
"""

import argparse
import sys

from . import runner, summary


def main(argv=None):
    parser = argparse.ArgumentParser(prog="aqos_ml.run", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("one")
    p.add_argument("--scenario", required=True, help="an id such as S1, or a path")
    p.add_argument("--config", required=True)
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--tag")
    p.add_argument("--extract", action="store_true", help="also run the live extractor and keep its records")
    p.add_argument("--results", default=runner.RESULTS)
    p = sub.add_parser("matrix")
    p.add_argument("file")
    p.add_argument("--results", default=runner.RESULTS)
    p = sub.add_parser("show")
    p.add_argument("run_dir")
    p = sub.add_parser("table")
    p.add_argument("--results", default=runner.RESULTS)
    p.add_argument("--out", help="write the table here instead of printing it")
    args = parser.parse_args(argv)

    try:
        if args.command == "show":
            print("\n".join(summary.summarise(args.run_dir)))
            return 0
        if args.command == "table":
            from . import table
            text = table.markdown(args.results)
            if args.out:
                runner.Path(args.out).parent.mkdir(parents=True, exist_ok=True)
                runner.Path(args.out).write_text(text)
                print(f"wrote {args.out}")
            else:
                print(text)
            return 0
        if args.command == "matrix":
            return 0 if runner.run_matrix(args.file, args.results) else 1
        manifest = runner.run_until_valid(args.scenario, args.config, args.seed, args.tag, args.results,
                                          extract=args.extract)
        if manifest is None:
            return 1
        print("\n".join(summary.summarise(runner.Path(args.results) / manifest["run_id"])))
        return 0
    except runner.RunError as exc:
        print(f"aqos_ml.run: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted: the run in progress left no manifest and will be run again", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
