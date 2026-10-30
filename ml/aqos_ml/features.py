"""The one function that turns a feature record into the numbers a model
sees. Training and serving both call it, so they cannot differ.

28 features: the signed sizes of the first 8 packets, the 7 gaps between
them, IP version, protocol, the port range of each side, and the 9 window
statistics (missing when the record has no window). Raw port numbers,
addresses and host identity are never features.
"""

import numpy as np

WINDOW = ["pkts_up_per_s", "pkts_down_per_s", "bytes_up_per_s", "bytes_down_per_s", "mean_pkt_size_up",
          "mean_pkt_size_down", "pkt_size_spread", "rate_cv", "burstiness"]
NAMES = ([f"size_{i}" for i in range(1, 9)] + [f"gap_{i}_us" for i in range(1, 8)]
         + ["ip_version", "protocol", "port_class_initiator", "port_class_responder"] + WINDOW)
FEATURES_VERSION = 1


def port_class(port):
    """0 system, 1 registered, 2 ephemeral (IANA's three ranges)."""
    return 0 if port < 1024 else 1 if port < 49152 else 2


def vector(record):
    window = record.get("window")
    return [
        *record["pkt_sizes"], *record["pkt_gaps_us"], record["ip_version"], record["protocol"],
        port_class(record["src_port"]), port_class(record["dst_port"]),
        *([window[name] for name in WINDOW] if window else [np.nan] * len(WINDOW)),
    ]


def matrix(records):
    return np.asarray([vector(r) for r in records], dtype=np.float64).reshape(-1, len(NAMES))
