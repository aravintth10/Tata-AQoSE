"""Did every generator of a run finish cleanly?"""

import json

from .metrics import summary_path


def check(out_dir, scenario):
    """Returns the reasons the run is invalid; empty means every generator
    ran to its stop time. The runner turns a non-empty list into
    `valid: false` and a retry."""
    problems = []
    for entry in scenario["traffic"]:
        for role in ("server", "client"):
            path = summary_path(out_dir, entry["id"], role)
            if not path.exists():
                problems.append(f"{entry['id']} {role}: no summary, the generator did not exit cleanly")
                continue
            summary = json.loads(path.read_text())
            if summary.get("status") != "ok":
                problems.append(f"{entry['id']} {role}: {summary.get('error', 'failed')}")
            elif role == "client" and summary.get("metric_samples", 0) == 0 and entry["profile"] != "backup_upload":
                problems.append(f"{entry['id']} {role}: wrote no metric samples")
    return problems
