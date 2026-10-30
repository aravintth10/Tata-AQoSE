"""Performs one run: testbed up, configuration, traffic, events, collect,
tear down, validate, manifest."""

import datetime
import fcntl
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import yaml

from . import validity
from ..gen.metrics import GRACE_S

REPO = Path(__file__).resolve().parents[3]
TESTBED = REPO / "bin" / "aqos-testbed"
CAKE_READER = REPO / "bin" / "aqos-cake-reader"
CTL = REPO / "bin" / "aqos-ctl"
TAXONOMY = json.loads((REPO / "contracts" / "taxonomy.json").read_text())
EXTRACT = REPO / "bin" / "aqos-extract"
LAN = "10.77.0.0/24,fd77:0:0:1::/64"
RESULTS = REPO / "results"

# What each configuration needs. Configurations with a controller arrive
# with chunk 6A.
CONFIGS = {
    "B0": {"script": "configs/baselines/B0.sh", "cake": False},
    "B2": {"script": "configs/baselines/B2.sh", "cake": True},
    # Measurement set-ups for the damage table (chunk 5B): B2, with every
    # host marked into the tin of its entry's class and the host of the
    # entry called `subject` forced into the tin named here.
    "D1": {"script": "configs/baselines/B2.sh", "cake": True, "force": "bulk"},
    "D2": {"script": "configs/baselines/B2.sh", "cake": True, "force": "best_effort"},
    "D3": {"script": "configs/baselines/B2.sh", "cake": True, "force": "video"},
    "D4": {"script": "configs/baselines/B2.sh", "cake": True, "force": "voice"},
}
# The live configurations are one controller with one setting changed: how
# the ML service decides. "@model" stands for the trained model's version.
CONFIGS.update({
    "B4": {"script": "configs/baselines/B2.sh", "cake": True, "service": ["--rules"]},
    "O1": {"script": "configs/baselines/B2.sh", "cake": True, "estimator": True,
           "service": ["--model", "@model", "--mode", "top_label"]},
    "O2": {"script": "configs/baselines/B2.sh", "cake": True, "estimator": True,
           "service": ["--model", "@model"]},
})
# The rules stand in for existing tools, which leave the shaper rate alone.
# Ours also follows the link's capacity; AQOS_ESTIMATOR=0 switches that off,
# to see what the labels alone are worth.
REFLECTOR = REPO / "bin" / "aqos-reflector"
REFLECTOR_PORT = 7077
AQOSD = REPO / "bin" / "aqosd"
MODELS = REPO / "models"
# B4 runs qosify's own thresholds, the ones people who install it get,
# unless configs/rules.yaml exists or AQOS_RULES names a file of thresholds
# tuned on the training data (python -m aqos_ml.rules writes one).
RULES = Path(os.environ.get("AQOS_RULES") or REPO / "configs" / "rules.yaml")
# The label value for each tin, as internal/enforce defines it.
TIN_MARK = {"bulk": 1, "best_effort": 2, "video": 3, "voice": 4}
SUBJECT = "subject"
NOT_YET = {"B1": "8D"}
# B3: plain CAKE with cake-autorate, the community's adaptive shaper,
# unmodified, from third_party/ (git clone of v3.2.2).
CONFIGS["B3"] = {"script": "configs/baselines/B2.sh", "cake": True, "autorate": True}
AUTORATE = REPO / "third_party" / "cake-autorate"
# cake-autorate reads the interfaces' byte counters from /sys and keeps
# its state under /run. The testbed enters a namespace's network only, so
# it would see the host's /sys; a private mount namespace with its own
# sysfs and /run gives it the router's view without changing the script.
_AUTORATE_WRAP = ("mount -t sysfs sysfs /sys && mount -t tmpfs tmpfs /run && mount -t tmpfs tmpfs /var/log && "
                  "exec bash \"$0\" \"$1\"")
# Events the testbed applies, and events the runner applies itself. The
# others need parts that do not exist yet.
TESTBED_EVENTS = {"link_rate", "link_impair"}
RUNNER_EVENTS = {"intent_lease", "kill_controller", "blackhole_reflector", "restore_reflector"}
# Changes to the traffic itself are part of each generator's plan, known to
# both ends before the run. The runner only notes the moment in its log.
PLANNED_EVENTS = {"traffic_param"}
WATCHDOG = REPO / "bin" / "aqos-watchdog"
INTENT_PORT = 8377
_POST = ("import sys, urllib.request, urllib.error\n"
         "request = urllib.request.Request(sys.argv[1], data=sys.argv[2].encode(), method='POST')\n"
         "try:\n    print(urllib.request.urlopen(request, timeout=3).read().decode().strip())\n"
         "except urllib.error.HTTPError as err:\n    sys.exit(err.read().decode().strip() or str(err))\n")


class RunError(Exception):
    """The run could not be performed at all, as opposed to performed and
    found invalid."""


def runtime_binary(binary):
    """Select all router runtime processes together; keep the Go testbed."""
    backend = os.environ.get("AQOS_RUNTIME", "rust")
    if backend not in ("go", "rust"):
        raise RunError(f"unknown AQOS_RUNTIME backend: {backend}")
    runtime = {str(p): p for p in (CAKE_READER, CTL, EXTRACT, REFLECTOR, AQOSD, WATCHDOG)}
    if backend == "rust" and str(binary) in runtime:
        directory = Path(os.environ.get("AQOS_RUNTIME_DIR", REPO / "bin"))
        return directory / (runtime[str(binary)].name + "-rs")
    if backend == "go" and str(binary) in runtime:
        return REPO / "bin" / "go" / runtime[str(binary)].name
    return binary


def run_id(scenario_id, config, seed, attempt=1):
    base = f"{scenario_id}-{config}-s{seed:04d}"
    return base if attempt == 1 else f"{base}-a{attempt}"


def find_scenario(name):
    """Accepts a path, or an id such as S1 for scenarios/S1.yaml."""
    for path in (Path(name), REPO / "scenarios" / f"{name}.yaml"):
        if path.is_file():
            return path.resolve()
    raise RunError(f"no scenario {name!r} (looked for the file and for scenarios/{name}.yaml)")


def _sh(cmd, **kwargs):
    return subprocess.run([str(runtime_binary(c)) for c in cmd], capture_output=True, text=True, **kwargs)


def _testbed(*args, check=True):
    result = _sh([TESTBED, *args])
    if check and result.returncode != 0:
        raise RunError(f"aqos-testbed {' '.join(map(str, args))}: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def versions():
    commit = _sh(["git", "-C", REPO, "rev-parse", "--short", "HEAD"])
    dirty = _sh(["git", "-C", REPO, "status", "--porcelain"]).stdout.strip()
    git_commit = commit.stdout.strip() if commit.returncode == 0 else "uncommitted"
    if commit.returncode == 0 and dirty:
        git_commit += "+dirty"
    tc = _sh(["tc", "-V"]).stdout.strip()
    return {
        "git_commit": git_commit,
        "kernel": platform.release(),
        "iproute2": tc.rsplit("iproute2-", 1)[-1] if "iproute2-" in tc else tc or "unknown",
        "model_version": None,
    }


def model_version():
    """The model the live configurations run: AQOS_MODEL if set, or the one
    training chose for dataset v2."""
    version = os.environ.get("AQOS_MODEL")
    chosen = MODELS / "chosen-v2.txt"
    if not version and chosen.exists():
        version = chosen.read_text().strip()
    if not version or not (MODELS / version / "meta.json").exists():
        raise RunError("no trained model: run `python -m aqos_ml.train`, or set AQOS_MODEL to a version under models/")
    return version


def service_args(config):
    """(arguments for aqos_ml.serve, the model version for the manifest)."""
    args, version = list(CONFIGS[config]["service"]), None
    # Extra service flags, for the ablations (for example --feature-noise 1.0).
    args += os.environ.get("AQOS_SERVE_ARGS", "").split()
    if "--rules" in args:
        from .. import rules
        version = rules.RULES_VERSION
        if RULES.exists():  # the thresholds tuned on the training data
            tuned = yaml.safe_load(RULES.read_text())
            args += ["--bulk-pps", tuned["bulk_pps"], "--priority-bytes", tuned["priority_bytes"]]
    else:
        version = model_version()
        args[args.index("@model")] = version
    return args, version


CLASSIFY = REPO / "bin" / "go" / "aqos-classify"
CLASSIFY_RUST = REPO / "bin" / "aqos-classify-rs"


def _cpu_seconds(pid):
    """CPU seconds a process and everything under it have used so far, from
    /proc. The runner starts each part through aqos-testbed exec, so the
    part itself is a descendant of the process it knows."""
    children, ticks = {}, {}
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = stat.read_text().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            continue
        p = int(stat.parent.name)
        children.setdefault(int(fields[1]), []).append(p)
        ticks[p] = sum(int(f) for f in fields[11:15])  # utime, stime, cutime, cstime
    total, todo = 0, [pid]
    while todo:
        p = todo.pop()
        total += ticks.get(p, 0)
        todo += children.get(p, [])
    return round(total / os.sysconf("SC_CLK_TCK"), 2)


def service_command(config, socket_path, args, version):
    """Rust is the default classifier. Python remains the B4 rules reference;
    Go/Python model services require explicit selection for comparisons.
    Missing Rust artifacts or unsupported flags fail without a fallback."""
    python = [sys.executable, "-m", "aqos_ml.serve", "--socket", socket_path, *args]
    trees = MODELS / str(version) / "trees.json"
    backend = os.environ.get("AQOS_SERVICE", "rust")
    if backend not in {"go", "python", "rust"}:
        raise RunError(f"unknown AQOS_SERVICE backend: {backend}")
    if "--rules" in args or backend == "python":
        return python
    if os.environ.get("AQOS_SERVE_ARGS", "").strip():
        raise RunError(f"AQOS_SERVE_ARGS is not supported by the {backend.title()} backend; select AQOS_SERVICE=python")
    binary = CLASSIFY_RUST if backend == "rust" else CLASSIFY
    if not binary.exists() or not trees.exists():
        target = "build-rust" if backend == "rust" else "build-go-reference"
        raise RunError(f"{backend.title()} classifier or exported model missing; run make {target}")
    mode = args[args.index("--mode") + 1] if "--mode" in args else "expected_damage"
    return [binary, "-socket", socket_path, "-model", trees, "-mode", mode,
            "-damage", REPO / "configs" / "damage_table.yaml"]


def entry_tins(scenario, forced):
    """{traffic id: tin} for a forced-tin run: the subject in the forced
    tin, every other entry in the tin of its own class."""
    if SUBJECT not in {entry["id"] for entry in scenario["traffic"]}:
        raise RunError(f"a forced-tin run needs a traffic entry called {SUBJECT!r}")
    tins = {}
    for entry in scenario["traffic"]:
        cls = TAXONOMY["profiles"][entry["profile"]]
        tins[entry["id"]] = forced if entry["id"] == SUBJECT else TAXONOMY["classes"][cls]["tin"]
    return tins


def force_rules(scenario, tins, hosts, seed):
    """nftables rules that label every flow of each traffic entry. An entry
    is told apart by its host's address and its server port, so that two
    entries on one host can sit in different tins. hosts is the testbed's
    host list, which has the addresses."""
    from ..gen import plan
    ports = plan.allocate_ports(seed, [entry["id"] for entry in scenario["traffic"]])
    address = {host["name"]: host for host in hosts}
    lines = []
    for entry in scenario["traffic"]:
        mark, host = TIN_MARK[tins[entry["id"]]], address[entry["host"]]
        port = ports[entry["id"]][0]
        for family, key in (("ip", "ip4"), ("ip6", "ip6")):
            lines.append(f"\t\t{family} saddr {host[key]} meta l4proto {{ tcp, udp }} th dport {port} "
                         f"ct mark set {mark}")
    return ("table inet aqos_force {\n\tchain prerouting {\n"
            "\t\ttype filter hook prerouting priority mangle;\n" + "\n".join(lines) + "\n\t}\n}\n")


def _bottleneck_kbit():
    """Reads the token buckets back from tc, so a run whose link was not
    what the scenario says cannot pass as valid."""
    rates = {}
    for direction, dev in (("up", "core0"), ("down", "rtr0")):
        out = _testbed("exec", "isp", "--", "tc", "-j", "qdisc", "show", "dev", dev, check=False)
        try:
            tbf = [q for q in json.loads(out) if q["kind"] == "tbf"][0]
            rates[direction] = tbf["options"]["rate"] * 8 / 1000  # tc reports bytes per second
        except (ValueError, IndexError, KeyError):
            pass
    return rates


class _Run:
    def __init__(self, scenario_path, config, seed, attempt, tag, results, extract=False):
        self.extract = extract
        self.scenario_path = scenario_path
        self.scenario = yaml.safe_load(scenario_path.read_text())
        self.config, self.seed, self.attempt, self.tag = config, seed, attempt, tag
        self.id = run_id(self.scenario["id"], config, seed, attempt)
        self.out = Path(results) / self.id
        self.procs = {}
        self.daemons = set()  # processes that run until they are told to stop
        self.expected_dead = set()  # processes a scenario's event killed on purpose
        self.killed_at = None
        self.reflectors = []
        self.model_version = None
        self.problems = []
        self.end_s = self.scenario["warmup_s"] + self.scenario["duration_s"]

    def log(self, text):
        with open(self.out / "runner.log", "a") as f:
            f.write(f"{datetime.datetime.now():%H:%M:%S} {text}\n")

    def sample(self, t_s, metric, value, **labels):
        line = json.dumps({"run_id": self.id, "t_s": t_s, "source": "system", "metric": metric,
                           "value": value, **labels})
        with open(self.out / "metrics.jsonl", "a") as f:
            f.write(line + "\n")

    def start(self, name, namespace, argv):
        """Starts a process inside a namespace, with its stderr kept."""
        logs = self.out / "logs"
        logs.mkdir(exist_ok=True)
        env = {**os.environ, "PYTHONPATH": str(REPO / "ml")}
        self.procs[name] = subprocess.Popen(
            [str(TESTBED), "exec", namespace, "--", *[str(runtime_binary(a)) for a in argv]], cwd=REPO, env=env,
            stdout=subprocess.DEVNULL, stderr=open(logs / f"{name}.err", "w"))

    def prepare(self):
        if self.out.exists():
            # Only a run without a manifest is ever rerun in place.
            shutil.rmtree(self.out)
        self.out.mkdir(parents=True)
        shutil.copy(self.scenario_path, self.out / "scenario.yaml")
        shutil.copy(REPO / CONFIGS[self.config]["script"], self.out / "config.sh")
        current = self.out.parent / "current"
        current.unlink(missing_ok=True)
        current.symlink_to(self.id)  # the exporter follows this

    def traffic(self, t0_ns, status):
        gen = [sys.executable, "-m", "aqos_ml.gen"]
        for role in ("server", "client"):
            for entry in self.scenario["traffic"]:
                argv = [*gen, role, "--scenario", self.out / "scenario.yaml", "--traffic-id", entry["id"],
                        "--seed", self.seed, "--run-id", self.id, "--t0-ns", t0_ns, "--out", self.out,
                        "--server4", status["server_ip4"], "--server6", status["server_ip6"]]
                self.start(f"{entry['id']}.{role}", "server" if role == "server" else "host-" + entry["host"], argv)
            if role == "server":
                time.sleep(0.4)  # servers must be listening before the first client packet

    def events(self, t0_ns, stop):
        """Applies the scenario's events at their times."""
        for event in sorted(self.scenario["events"], key=lambda e: e["at_s"]):
            wait = event["at_s"] - (time.monotonic_ns() - t0_ns) / 1e9
            if stop.wait(max(wait, 0)):
                return
            args = [f"{k}={v}" for k, v in (event.get("args") or {}).items()]
            if event["type"] in PLANNED_EVENTS:
                self.log(f"event at {event['at_s']} s: {event['type']} {' '.join(args)} (in the generators' plans)")
                continue
            if event["type"] == "intent_lease":
                self.lease(event)
                continue
            if event["type"] in RUNNER_EVENTS:
                self.fault(event)
                continue
            result = _sh([TESTBED, "event", event["type"], *args])
            self.log(f"event at {event['at_s']} s: {event['type']} {' '.join(args)} -> "
                     f"{(result.stdout or result.stderr).strip()}")
            if result.returncode != 0:
                self.problems.append(f"event {event['type']} at {event['at_s']} s failed: {result.stderr.strip()}")
            elif event["type"] == "link_rate":
                self.sample(event["at_s"], "capacity_true_kbit", int(event["args"]["rate_kbit"]),
                            direction=event["args"]["direction"])

    def lease(self, event):
        """The user asking for priority for a device: a request to the
        controller's lease API. Configurations without a controller have
        nobody to ask, which is the point of comparing them."""
        args = event.get("args") or {}
        if "aqosd" not in self.procs:
            self.log(f"event at {event['at_s']} s: intent_lease skipped, {self.config} has no controller")
            return
        body = json.dumps({"host": self.addresses[args["host"]], "minutes": args["minutes"]})
        result = _sh([TESTBED, "exec", "router", "--", sys.executable, "-c", _POST,
                      f"http://127.0.0.1:{INTENT_PORT}/leases", body])
        self.log(f"event at {event['at_s']} s: intent_lease {args} -> {(result.stdout or result.stderr).strip()}")
        if result.returncode != 0:
            self.problems.append(f"event intent_lease at {event['at_s']} s failed: {result.stderr.strip()}")

    def fault(self, event):
        """Faults injected on purpose: the controller killed outright, or
        the delay reflectors made unreachable. Configurations without the
        part that would fail have nothing to break and skip the event."""
        kind, args = event["type"], event.get("args") or {}
        if kind == "kill_controller":
            proc = self.procs.get("aqosd")
            if proc is None:
                self.log(f"event at {event['at_s']} s: kill_controller skipped, {self.config} has no controller")
                return
            proc.kill()  # no warning and no clean-up, as a crash would be
            self.killed_at = time.time()
            self.expected_dead.add("aqosd")
            self.log(f"event at {event['at_s']} s: controller killed")
            return
        if "reflector" not in self.procs:
            self.log(f"event at {event['at_s']} s: {kind} skipped, {self.config} probes no reflectors")
            return
        # Dropped in the core, beyond the bottleneck, as an outage on the
        # far side of the link would be.
        _sh([TESTBED, "exec", "core", "--", "nft", "delete", "table", "inet", "aqos_blackhole"])
        result = None
        if kind == "blackhole_reflector":
            which = [self.reflectors[i] for i in args.get("reflectors", range(len(self.reflectors)))]
            rules = ("table inet aqos_blackhole {\n\tchain forward {\n\t\ttype filter hook forward priority filter;\n"
                     f"\t\tip daddr {{ {', '.join(which)} }} drop\n\t}}\n}}\n")
            (self.out / "blackhole.nft").write_text(rules)
            result = _sh([TESTBED, "exec", "core", "--", "nft", "-f", self.out / "blackhole.nft"])
        self.log(f"event at {event['at_s']} s: {kind} {args}")
        if result is not None and result.returncode != 0:
            self.problems.append(f"event {kind} at {event['at_s']} s failed: {result.stderr.strip()}")

    def autorate(self, status):
        """Starts cake-autorate on the router, set up as a home user would:
        the configured rates as base and maximum (as our estimator never
        goes above them), a tenth as minimum (as ours), and the testbed's
        three reflectors pinged with ping."""
        if not (AUTORATE / "cake-autorate.sh").exists():
            raise RunError("cake-autorate is missing: git clone https://github.com/lynxthecat/cake-autorate "
                           "third_party/cake-autorate && git -C third_party/cake-autorate checkout v3.2.2")
        link, pct = self.scenario["link"], int(os.environ.get("SHAPER_PCT", "95"))
        base = {"dl": link["down_kbit"] * pct // 100, "ul": link["up_kbit"] * pct // 100}
        reflectors = " ".join(f'"{r}"' for r in status.get("reflectors") or [])
        floor = {d: max(base[d] // 10, 500) for d in base}
        config = "\n".join([
            "dl_if=ifb0", "ul_if=wan0", "pinger_binary=ping", f"reflectors=({reflectors})",
            f"no_pingers={len(status.get('reflectors') or [])}", "randomize_reflectors=0",
            "enable_sleep_function=0", "startup_wait_s=0.0", "log_to_file=1",
            f"log_file_path_override=\"{self.out}\"", "output_cake_changes=1",
            # It refuses a threshold for "busy" above the minimum rates.
            f"connection_active_thr_kbps={min(2000, *floor.values())}",
            *[f"{k}_{d}_shaper_rate_kbps={v}" for d in ("dl", "ul")
              for k, v in (("min", floor[d]), ("base", base[d]), ("max", base[d]))],
        ]) + "\n"
        # Its instance name comes from the file name: config.<name>.sh.
        (self.out / "config.b3.sh").write_text(config)
        self.start("autorate", "router", ["env", f"CAKE_AUTORATE_SCRIPT_PREFIX={AUTORATE}",
                                          "unshare", "-m", "--propagation", "private", "sh", "-c", _AUTORATE_WRAP,
                                          AUTORATE / "cake-autorate.sh", self.out / "config.b3.sh"])
        self.daemons.add("autorate")

    def perform(self):
        """Runs the traffic. Returns the bottleneck rates read at the end."""
        link = self.scenario["link"]
        self.log(_testbed("up", self.scenario_path).strip())
        status = json.loads(_testbed("status"))
        self.addresses = {host["name"]: host["ip4"] for host in status["hosts"]}
        (self.out / "environment.json").write_text(json.dumps({
            "testbed_mode": status["mode"], "cpus": os.cpu_count(), "python": platform.python_version(),
            "shaper_pct": os.environ.get("SHAPER_PCT", "95"),
            "estimator": bool(CONFIGS[self.config].get("estimator")) and os.environ.get("AQOS_ESTIMATOR", "1") != "0",
            "reclassifier": os.environ.get("AQOS_RECLASSIFIER", "1") != "0",
            "aqosd_args": os.environ.get("AQOSD_ARGS", ""),
            "serve_args": os.environ.get("AQOS_SERVE_ARGS", ""),
            "service": ("python" if self.config == "B4" else os.environ.get("AQOS_SERVICE", "rust"))
                       if CONFIGS[self.config].get("service") else "none",
            "runtime": os.environ.get("AQOS_RUNTIME", "rust"),
        }, indent=2) + "\n")
        self.log(_testbed("exec", "router", "--", REPO / CONFIGS[self.config]["script"],
                          link["down_kbit"], link["up_kbit"]).strip())

        forced = CONFIGS[self.config].get("force")
        if forced:
            tins = entry_tins(self.scenario, forced)
            self.log(_testbed("exec", "router", "--", CTL, "marking", "install").strip())
            rules = force_rules(self.scenario, tins, status["hosts"], self.seed)
            (self.out / "force.nft").write_text(rules)
            result = _sh([TESTBED, "exec", "router", "--", "nft", "-f", self.out / "force.nft"])
            if result.returncode != 0:
                raise RunError(f"nft: {result.stderr.strip()}")
            self.log(f"forced tins: {tins}")

        if CONFIGS[self.config].get("autorate"):
            self.autorate(status)

        service = CONFIGS[self.config].get("service")
        if service:
            # The ML service first: the controller will not start without it.
            args, self.model_version = service_args(self.config)
            # Not in the run directory: a Unix socket's path may be at most
            # 108 bytes, and a results directory can be anywhere.
            socket_path = _lock_path().with_name(f"ml-{os.getpid()}.sock")
            socket_path.unlink(missing_ok=True)
            self.start("ml-service", "router", service_command(self.config, socket_path, args, self.model_version))
            self.daemons.add("ml-service")
            for _ in range(300):
                if socket_path.exists() or self.procs["ml-service"].poll() is not None:
                    break
                time.sleep(0.1)
            if not socket_path.exists():
                raise RunError("the ML service did not start: see logs/ml-service.err")

        # Every process is told the same instant to call zero, far enough
        # ahead for all of them to have started.
        lead_s = 2.5 + 0.25 * len(self.scenario["traffic"])
        t0_ns = time.monotonic_ns() + int(lead_s * 1e9)
        if service:
            estimator = []
            if CONFIGS[self.config].get("estimator") and os.environ.get("AQOS_ESTIMATOR", "1") != "0":
                reflectors = self.reflectors = status.get("reflectors") or []
                self.start("reflector", "server", [REFLECTOR, *[f"{r}:{REFLECTOR_PORT}" for r in reflectors]])
                self.daemons.add("reflector")
                estimator = ["-reflectors", ",".join(reflectors)]
            # The model's class envelopes switch the reclassifier on. The
            # rules have no classes to have envelopes for, so B4 asks about
            # every flow every five seconds, as before.
            # AQOS_RECLASSIFIER=0 does the same for the model, and
            # AQOSD_ARGS adds controller flags, both for the ablations.
            extra = os.environ.get("AQOSD_ARGS", "").split()
            envelopes = MODELS / str(self.model_version) / "envelopes.json"
            if envelopes.exists() and os.environ.get("AQOS_RECLASSIFIER", "1") != "0":
                extra += ["-envelopes", envelopes]
            # The watchdog, started before the controller it watches. Its
            # safe state is plain CAKE at the rates B2 uses.
            pct = int(os.environ.get("SHAPER_PCT", "95"))
            heartbeat = self.out / "heartbeat"
            (self.out / "watchdog.json").write_text(json.dumps({
                "heartbeat": str(heartbeat), "timeout_ms": 2000,
                "safe_up_kbit": link["up_kbit"] * pct // 100, "safe_down_kbit": link["down_kbit"] * pct // 100,
            }, indent=2) + "\n")
            self.start("watchdog", "router", [WATCHDOG, "-config", self.out / "watchdog.json"])
            self.daemons.add("watchdog")
            self.start("aqosd", "router", [
                AQOSD, "-socket", socket_path, "-lan", LAN, "-shield", REPO / "configs" / "shield.yaml",
                "-run-id", self.id, "-t0-ns", t0_ns, "-until-s", self.end_s + GRACE_S,
                "-metrics", self.out / "metrics.jsonl", "-actions", self.out / "actions.jsonl",
                "-summary", self.out / "controller.json", "-intent", f"127.0.0.1:{INTENT_PORT}",
                "-verdicts", self.out / "verifier.jsonl", "-heartbeat", heartbeat, *estimator, *extra])
            # It reads the shaper rates, checks the service and installs the
            # marking before any traffic starts.
            time.sleep(1.0)
            if self.procs["aqosd"].poll() is not None:
                raise RunError("the controller did not start: see logs/aqosd.err")
        for direction in ("up", "down"):
            self.sample(0, "capacity_true_kbit", link[f"{direction}_kbit"], direction=direction)
        if CONFIGS[self.config]["cake"]:
            self.start("cake-reader", "router", [CAKE_READER, "-run-id", self.id, "-t0-ns", t0_ns,
                                                  "-until-s", self.end_s, "-out", self.out / "metrics.jsonl"])
        if self.extract:
            # The live extractor, as the controller will run it. Its records
            # are what a classifier would have seen during this run, so
            # they become training data. It starts before any traffic.
            self.start("extract", "router", [
                EXTRACT, "-live", "-lan", LAN, "-window-every", 5, "-until-s", lead_s + self.end_s + GRACE_S + 1,
                "-out", self.out / "features.jsonl", "-keys", self.out / "keys.jsonl"])
            time.sleep(0.5)
        self.traffic(t0_ns, status)

        stop = threading.Event()
        events = threading.Thread(target=self.events, args=(t0_ns, stop), daemon=True)
        events.start()
        deadline = time.monotonic() + lead_s + self.end_s + GRACE_S + 20
        for name, proc in self.procs.items():
            if name in self.daemons:
                continue
            try:
                proc.wait(max(deadline - time.monotonic(), 0.1))
            except subprocess.TimeoutExpired:
                proc.kill()
                self.problems.append(f"{name}: still running after the end of the run")
        (self.out / "cpu.json").write_text(json.dumps(
            {name: _cpu_seconds(self.procs[name].pid) for name in sorted(self.daemons)}, indent=2) + "\n")
        for name in self.daemons:
            if self.procs[name].poll() is not None:
                self.problems.append(f"{name}: stopped during the run (exit {self.procs[name].returncode})")
            self.procs[name].terminate()
        stop.set()
        events.join()
        for name, proc in self.procs.items():
            if proc.returncode not in (0, None) and not name.endswith((".client", ".server")) \
                    and name not in self.daemons and name not in self.expected_dead:
                self.problems.append(f"{name}: exit {proc.returncode}")
        if self.killed_at:
            # The watchdog leaves a file when it applies the safe state.
            safe = Path(str(self.out / "heartbeat") + ".safe")
            took = round(safe.stat().st_mtime - self.killed_at, 2) if safe.exists() else None
            (self.out / "failsafe.json").write_text(json.dumps({"seconds_to_safe_state": took}) + "\n")
            self.log(f"safe state {took} s after the controller was killed" if took is not None
                     else "the controller was killed and the watchdog never applied the safe state")
            if took is None:
                self.problems.append("the watchdog did not apply the safe state after the controller was killed")
        if forced:
            # Packets CAKE sent per tin, to show where the marks put them.
            (self.out / "tins.json").write_text(_testbed("exec", "router", "--", CTL, "tins", check=False))
        return _bottleneck_kbit()

    def stop(self):
        for proc in self.procs.values():
            if proc.poll() is None:
                proc.kill()
        _testbed("down", check=False)

    def manifest(self, started_at, problems):
        manifest = {
            "schema_version": 1, "run_id": self.id, "scenario_id": self.scenario["id"],
            "config": self.config, "seed": self.seed, "attempt": self.attempt, "tag": self.tag,
            "started_at": started_at,
            "duration_s": self.scenario["duration_s"], "warmup_s": self.scenario["warmup_s"],
            "valid": not problems, "invalid_reason": "; ".join(problems[:5]) or None,
            "versions": {**versions(), "model_version": self.model_version},
        }
        tmp = self.out / "manifest.json.tmp"
        tmp.write_text(json.dumps(manifest, indent=2) + "\n")
        tmp.rename(self.out / "manifest.json")
        return manifest


def _lock_path():
    return Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "aqos" / "run.lock"


def claim_testbed():
    """There is one testbed. Returns an open file holding a lock on it, or
    raises if another run has it: starting a second run would tear the
    first one's network down under it."""
    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = open(path, "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.seek(0)
        holder = lock.read().strip() or "another process"
        lock.close()
        raise RunError(f"the testbed is in use by {holder}; wait for it, or stop it first") from None
    lock.truncate(0)
    lock.write(f"run {os.getpid()} ({' '.join(sys.argv[:4])})")
    lock.flush()
    return lock


def run_one(scenario, config, seed, attempt=1, tag=None, results=RESULTS, extract=False):
    """Performs one run and returns its manifest. An interrupted run leaves
    no manifest, so it is simply run again."""
    if config in NOT_YET:
        raise RunError(f"configuration {config} is not built yet (chunk {NOT_YET[config]})")
    if config not in CONFIGS:
        raise RunError(f"unknown configuration {config!r}")
    for binary in (TESTBED, CAKE_READER, EXTRACT, CTL, AQOSD, REFLECTOR, WATCHDOG):
        binary = Path(runtime_binary(binary))
        if not binary.exists():
            target = "build" if binary == TESTBED or os.environ.get("AQOS_RUNTIME", "rust") == "rust" else "build-go-reference"
            raise RunError(f"{binary} is missing: run `make {target}`")
    if extract and CONFIGS[config].get("service"):
        raise RunError("the stand-alone extractor and the controller cannot run together: both own the nflog rule")
    run = _Run(find_scenario(scenario), config, seed, attempt, tag, results, extract)
    unsupported = {e["type"] for e in run.scenario["events"]} - TESTBED_EVENTS - RUNNER_EVENTS - PLANNED_EVENTS
    if unsupported:
        raise RunError(f"scenario {run.scenario['id']} uses events the runner cannot apply yet: {sorted(unsupported)}")

    # Validate the selected service before taking ownership of any namespace.
    if CONFIGS[config].get("service"):
        args, version = service_args(config)
        service_command(config, Path("preflight.sock"), args, version)

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    lock = claim_testbed()  # before anything is written: a refused run leaves no trace
    previous = signal.signal(signal.SIGTERM, interrupted)
    started_at = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        run.prepare()
        try:
            measured = run.perform()
        except RunError as exc:
            # The testbed or the configuration failed: a real attempt, and invalid.
            run.stop()
            run.log(f"failed: {exc}")
            return run.manifest(started_at, [str(exc)])
        except BaseException:
            run.stop()
            raise
        run.stop()
    finally:
        signal.signal(signal.SIGTERM, previous)
        lock.close()
    problems = run.problems + validity.check(run.out, run.scenario, measured)
    for problem in problems:
        run.log(f"invalid: {problem}")
    return run.manifest(started_at, problems)


def attempts(scenario_id, config, seed, results=RESULTS):
    """Manifests of the attempts already made at one run, oldest first."""
    found = []
    for attempt in range(1, 50):
        path = Path(results) / run_id(scenario_id, config, seed, attempt) / "manifest.json"
        if not path.exists():
            break
        found.append(json.loads(path.read_text()))
    return found


MAX_ATTEMPTS = 2  # the first try and one retry


def run_until_valid(scenario, config, seed, tag=None, results=RESULTS, report=print, extract=False):
    """Runs unless a valid result exists, retrying an invalid run once.
    Returns the valid manifest, or None if the attempts are used up."""
    path = find_scenario(scenario)
    scenario_id = yaml.safe_load(path.read_text())["id"]
    made = attempts(scenario_id, config, seed, results)
    requested_runtime = os.environ.get("AQOS_RUNTIME", "rust")
    if requested_runtime not in ("go", "rust"):
        raise RunError(f"unknown AQOS_RUNTIME backend: {requested_runtime}")
    for manifest in made:
        environment = Path(results) / manifest["run_id"] / "environment.json"
        prior_runtime = json.loads(environment.read_text()).get("runtime", "go") if environment.exists() else "go"
        if requested_runtime != prior_runtime:
            raise RunError("results contain a different runtime backend; use a separate --results directory")
    # A Rust trial must never silently reuse a Go result (or vice versa).
    # Older results have no environment file and are not evidence of Rust use.
    if CONFIGS[config].get("service") and config != "B4":
        requested = os.environ.get("AQOS_SERVICE", "rust")
        for manifest in made:
            environment = Path(results) / manifest["run_id"] / "environment.json"
            prior = json.loads(environment.read_text()).get("service", "go") if environment.exists() else "go"
            if "rust" in (requested, prior) and requested != prior:
                raise RunError("results contain a different classifier backend; use a separate --results directory")
    for manifest in made:
        if manifest["valid"]:
            report(f"skip   {manifest['run_id']}: already valid")
            return manifest
    while len(made) < MAX_ATTEMPTS:
        attempt = len(made) + 1
        report(f"run    {run_id(scenario_id, config, seed, attempt)}")
        manifest = run_one(path, config, seed, attempt, tag, results, **({"extract": True} if extract else {}))
        made.append(manifest)
        if manifest["valid"]:
            return manifest
        report(f"INVALID {manifest['run_id']}: {manifest['invalid_reason']}")
    report(f"gave up on {run_id(scenario_id, config, seed)} after {len(made)} attempts")
    return None


def expand(matrix):
    """Every (scenario, config, seed) of a matrix file's contents."""
    seeds = matrix["seeds"]
    if isinstance(seeds, dict):
        seeds = range(seeds["from"], seeds["to"] + 1)
    if matrix.get("by") == "seed":
        # Whole seeds first: stopping early leaves every scenario with the
        # same seeds, which is what a paired comparison needs.
        return [(scenario, config, seed)
                for seed in seeds for scenario in matrix["scenarios"] for config in matrix["configs"]]
    return [(scenario, config, seed)
            for scenario in matrix["scenarios"] for seed in seeds for config in matrix["configs"]]


def run_matrix(matrix_path, results=RESULTS, report=print):
    """Runs every combination that has no valid result yet. Safe to
    interrupt and run again."""
    matrix = yaml.safe_load(Path(matrix_path).read_text())
    runs = expand(matrix)
    valid = 0
    for i, (scenario, config, seed) in enumerate(runs, 1):
        report(f"[{i}/{len(runs)}]", end=" ")
        if run_until_valid(scenario, config, seed, matrix.get("tag"), results, report, matrix.get("extract", False)):
            valid += 1
    report(f"{valid} of {len(runs)} runs valid")
    return valid == len(runs)
