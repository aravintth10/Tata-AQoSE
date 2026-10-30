"""Command line for the traffic generators.

  python -m aqos_ml.gen plan   --scenario S1.yaml --seed 7
  python -m aqos_ml.gen t0     [--in 1.0]
  python -m aqos_ml.gen server --scenario S1.yaml --traffic-id call --seed 7 \\
        --run-id S1-B2-s0007 --t0-ns <t0> --out results/S1-B2-s0007
  python -m aqos_ml.gen client ... same, plus --server4 10.78.0.10 --server6 fd77:0:0:4::2
  python -m aqos_ml.gen check  --scenario S1.yaml --out results/S1-B2-s0007

The runner starts one server (in the server namespace) and one client (in
the host's namespace) per traffic entry, with the same seed and the same
--t0-ns, the CLOCK_MONOTONIC time at which the run's clock reads zero.
"""

import argparse
import json
import signal
import sys
import time
from pathlib import Path

import yaml

from . import plan as planning
from . import tcp, udp, validate
from .metrics import FlowLog, MetricWriter, RunClock, write_summary


def _load(path):
    return yaml.safe_load(Path(path).read_text())


def _run(args):
    scenario = _load(args.scenario)
    out = Path(args.out)
    summary = {"traffic_id": args.traffic_id, "role": args.role, "seed": args.seed, "status": "error"}
    writer = flows = None

    def terminated(signum, frame):
        raise SystemExit(f"terminated by signal {signum} before the stop time")

    signal.signal(signal.SIGTERM, terminated)
    try:
        plan = planning.build(scenario, args.traffic_id, args.seed)
        summary.update(profile=plan.profile, plan_digest=plan.digest(), params=plan.params)
        clock = RunClock(args.t0_ns)
        if clock.now_s() > plan.start_s:
            raise RuntimeError(f"started {clock.now_s() - plan.start_s:.3f} s after the entry's start time")
        writer = MetricWriter(out / "metrics.jsonl", args.run_id, plan)
        flows = FlowLog(out / "flows.jsonl", args.run_id, plan, clock)
        server_addr = args.server6 if plan.params["ip_version"] == 6 else args.server4
        if args.role == "client" and not server_addr:
            raise RuntimeError(f"--server{plan.params['ip_version']} is needed for this entry")
        module = udp if plan.profile in planning.UDP_PROFILES else tcp
        summary["stats"] = module.run(plan, args.role, clock, writer, flows, server_addr)
        summary["status"] = "ok"
    except (Exception, SystemExit, KeyboardInterrupt) as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        for log in (writer, flows):
            if log:
                log.close()
        if writer:
            summary["metric_samples"] = writer.count
    write_summary(out, args.traffic_id, args.role, summary)
    if summary["status"] != "ok":
        print(f"aqos_ml.gen {args.role} {args.traffic_id}: {summary['error']}", file=sys.stderr)
        return 1
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="aqos_ml.gen", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="role", required=True)

    p = sub.add_parser("plan", help="print each entry's plan digest, ports and parameters")
    p.add_argument("--scenario", required=True)
    p.add_argument("--seed", type=int, required=True)

    p = sub.add_parser("t0", help="print a --t0-ns value a little in the future")
    p.add_argument("--in", dest="delay", type=float, default=1.0, metavar="SECONDS")

    for role in ("server", "client"):
        p = sub.add_parser(role)
        p.add_argument("--scenario", required=True)
        p.add_argument("--traffic-id", required=True)
        p.add_argument("--seed", type=int, required=True)
        p.add_argument("--run-id", required=True)
        p.add_argument("--t0-ns", type=int, required=True)
        p.add_argument("--out", required=True, help="the run's result directory")
        p.add_argument("--server4")
        p.add_argument("--server6")

    p = sub.add_parser("check", help="exit non-zero unless every generator of the run finished cleanly")
    p.add_argument("--scenario", required=True)
    p.add_argument("--out", required=True)

    args = parser.parse_args(argv)
    if args.role == "t0":
        print(time.monotonic_ns() + int(args.delay * 1e9))
        return 0
    if args.role == "plan":
        scenario = _load(args.scenario)
        plans = {t["id"]: planning.build(scenario, t["id"], args.seed) for t in scenario["traffic"]}
        print(json.dumps({
            tid: {"profile": pl.profile, "digest": pl.digest(), "server_port": pl.server_port,
                  "client_port": pl.client_port, "params": pl.params}
            for tid, pl in plans.items()
        }, indent=2))
        return 0
    if args.role == "check":
        problems = validate.check(args.out, _load(args.scenario))
        for problem in problems:
            print(problem)
        return 1 if problems else 0
    return _run(args)


if __name__ == "__main__":
    sys.exit(main())
