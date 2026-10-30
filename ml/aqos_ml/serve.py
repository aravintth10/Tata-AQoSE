"""The ML service: the classifier and the decision engine behind a Unix
socket, for the controller.

  python -m aqos_ml.serve --socket /run/aqos/ml.sock --model lgbm-v1-0123abcd
  python -m aqos_ml.serve --socket /run/aqos/ml.sock --rules

One JSON object per line each way (contracts README, "The ML service"). The
three live configurations differ only in the flags here:

  O2  --model <version>                    (expected damage, the default)
  O1  --model <version> --mode top_label
  B4  --rules [--bulk-pps N --priority-bytes N]

Requests that arrive together are classified together: a tree model costs
almost the same for a hundred records as for one. A request that cannot be
answered gets `ok: false`, and the controller leaves that flow unlabelled.
"""

import argparse
import json
import os
import queue
import signal
import socket
import socketserver
import sys
import threading
from pathlib import Path

from jsonschema import Draft202012Validator

from . import decide, evaluate, model, rules

SCHEMAS = evaluate.REPO / "contracts" / "schema"
BATCH = 256  # most requests answered in one go


def _validator(name):
    return Draft202012Validator(json.loads((SCHEMAS / f"{name}.schema.json").read_text()))


class Service:
    """Answers request objects. No sockets here, so tests can call it."""

    def __init__(self, classifier, engine):
        self.classifier, self.engine = classifier, engine
        self._request = _validator("service_request")
        self._record = _validator("feature_record")

    def problem(self, request):
        """What is wrong with a request, or None."""
        if not isinstance(request, dict):
            return "request is not an object"
        for error in self._request.iter_errors(request):
            return f"request: {error.message}"
        if request["op"] == "classify":
            for error in self._record.iter_errors(request["record"]):
                return f"record: {error.message}"
        return None

    def health(self, request):
        return {"id": request["id"], "ok": True, "model_version": self.classifier.model_version,
                "decision_rule": self.engine.rule_name, "ready": True}

    # For the ablation that asks whether expected damage protects calls
    # better than top label when the classifier is worse: every number the
    # model sees is multiplied by a random factor, exp(N(0, noise)). The
    # flow's kind and protocol are left alone; so is what the guard on
    # Bulk reads. Zero, the default, changes nothing.
    noise = 0.0
    _rng = None

    def blur(self, record):
        if not self.noise:
            return record
        import numpy as np
        if self._rng is None:
            self._rng = np.random.default_rng(int(os.environ.get("AQOS_NOISE_SEED", "1")))
        def f(values):
            return np.exp(self._rng.normal(0.0, self.noise, len(values)))

        out = dict(record)
        sizes = np.asarray(record["pkt_sizes"], float)
        out["pkt_sizes"] = [int(np.sign(s) * min(max(abs(s) * k, 40), 1500)) for s, k in zip(sizes, f(sizes))]
        out["pkt_gaps_us"] = [int(g * k) for g, k in zip(record["pkt_gaps_us"], f(record["pkt_gaps_us"]))]
        if record.get("window"):
            w = dict(record["window"])
            keys = [k for k, v in w.items() if k != "samples" and isinstance(v, (int, float))]
            for k, factor in zip(keys, f(keys)):
                w[k] = w[k] * factor
            out["window"] = w
        return out

    def answer(self, requests):
        """One response per request, in the same order."""
        responses = [None] * len(requests)
        todo = []
        for i, request in enumerate(requests):
            problem = self.problem(request)
            if problem:
                rid = request.get("id") if isinstance(request, dict) else None
                rid = rid if isinstance(rid, int) and not isinstance(rid, bool) and rid >= 0 else None
                responses[i] = {"id": rid, "ok": False, "error": problem}
            elif request["op"] == "health":
                responses[i] = self.health(request)
            else:
                todo.append(i)
        if todo:
            records = [self.blur(requests[i]["record"]) for i in todo]
            try:
                many = getattr(self.classifier, "classify_many", None)
                classifications = many(records) if many else [self.classifier.classify(r) for r in records]
            except Exception as err:  # noqa: BLE001 - a failing model must not take the service down
                for i in todo:
                    responses[i] = {"id": requests[i]["id"], "ok": False,
                                    "error": f"classifier: {type(err).__name__}: {err}"}
                return responses
            for i, classification in zip(todo, classifications):
                request = requests[i]
                # A window record is a second look at a flow already seen.
                source = "reclassifier" if request["record"]["kind"] == "window" else "decision_engine"
                proposal = self.engine.propose(
                    classification, request["record"]["host_id"], previous_tin=request["previous_tin"],
                    leased_classes=request["leased_classes"], source=source, record=request["record"])
                responses[i] = {"id": request["id"], "ok": True, "classification": classification,
                                "proposal": proposal}
        return responses


class _Connection(socketserver.StreamRequestHandler):
    def handle(self):
        lock = threading.Lock()

        def reply(response):
            with lock:
                try:
                    self.wfile.write((json.dumps(response) + "\n").encode())
                    self.wfile.flush()
                except OSError:
                    pass  # the controller went away; nothing to tell it

        for line in self.rfile:
            if not line.strip():
                continue
            try:
                request = json.loads(line)
            except ValueError:
                reply({"id": None, "ok": False, "error": "request is not JSON"})
                continue
            self.server.pending.put((request, reply))


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, path, service):
        self.service, self.pending = service, queue.Queue()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        if os.path.exists(path):
            os.unlink(path)  # left by a previous run; a live one would hold a lock, not a file
        super().__init__(str(path), _Connection)
        os.chmod(path, 0o600)
        self._worker = threading.Thread(target=self._work, daemon=True)
        self._worker.start()

    def _work(self):
        """Takes whatever has arrived and answers it as one batch."""
        while True:
            batch = [self.pending.get()]
            while len(batch) < BATCH:
                try:
                    batch.append(self.pending.get_nowait())
                except queue.Empty:
                    break
            if batch[0] is None:
                return
            try:
                responses = self.service.answer([request for request, _ in batch])
            except Exception as err:  # noqa: BLE001
                responses = [{"id": None, "ok": False, "error": f"service: {type(err).__name__}: {err}"}] * len(batch)
            for (_, reply), response in zip(batch, responses):
                reply(response)


def build(args):
    damage = evaluate.load_damage_table(args.damage) if args.damage else evaluate.load_damage_table()
    if args.rules:
        classifier = rules.Rules(args.bulk_pps, args.priority_bytes)
        engine = decide.Engine(damage, mode="top_label", demotion_bar=args.demotion_bar, rule_name="rules",
                               bulk_guard=False)
    else:
        classifier = model.load(args.model, args.models)
        engine = decide.Engine(damage, mode=args.mode, demotion_bar=args.demotion_bar,
                               lease_multiplier=args.lease_multiplier, bulk_guard=not args.no_bulk_guard)
    service = Service(classifier, engine)
    service.noise = args.feature_noise
    return service


def main(argv=None):
    parser = argparse.ArgumentParser(prog="aqos_ml.serve", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--socket", required=True)
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--model", help="a version under models/")
    which.add_argument("--rules", action="store_true", help="the hand-written threshold rules (B4)")
    parser.add_argument("--models", default=model.MODELS)
    parser.add_argument("--mode", choices=decide.MODES, default="expected_damage")
    parser.add_argument("--damage", help="damage table (default configs/damage_table.yaml)")
    parser.add_argument("--demotion-bar", type=float, default=0.8)
    parser.add_argument("--feature-noise", type=float, default=0.0,
                        help="multiply every number the model sees by exp(N(0, this)): a weaker classifier, for the ablation")
    parser.add_argument("--no-bulk-guard", action="store_true",
                        help="let the model demote UDP flows and first-packet records to Bulk, as before the guard")
    parser.add_argument("--lease-multiplier", type=float, default=5.0)
    parser.add_argument("--bulk-pps", type=float, default=200.0)
    parser.add_argument("--priority-bytes", type=float, default=450.0)
    args = parser.parse_args(argv)

    service = build(args)
    server = Server(args.socket, service)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
    print(f"aqos_ml.serve: {service.classifier.model_version}, {service.engine.rule_name}, on {args.socket}",
          file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if os.path.exists(args.socket):
            os.unlink(args.socket)
    return 0


def ask(path, requests, timeout=5.0):
    """Sends requests over one connection and returns the responses by id.
    For tests and for trying the service by hand."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(path))
        sock.sendall("".join(json.dumps(r) + "\n" for r in requests).encode())
        reader, responses = sock.makefile("r"), {}
        for _ in requests:
            response = json.loads(reader.readline())
            responses[response["id"]] = response
        return responses


if __name__ == "__main__":
    sys.exit(main())
