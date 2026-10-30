"""Header at the start of every generator datagram.

This is the generators' own payload, used to measure latency, loss and
frame arrival exactly. The feature extractor never reads it.
"""

import struct

MAGIC = b"AQ"
# magic, stream, flags, sequence number, send time (CLOCK_MONOTONIC ns),
# frame number, index in frame, packets in frame
_HEADER = struct.Struct("!2sBBIQIHH")
HEADER_SIZE = _HEADER.size

# A flow_storm request: magic, then the size of the reply wanted.
STORM_MAGIC = b"AQS1"
STORM = struct.Struct("!4sI")


def pack_into(buf, stream, seq, ts_ns, frame, index, count):
    _HEADER.pack_into(buf, 0, MAGIC, stream, 0, seq & 0xFFFFFFFF, ts_ns, frame & 0xFFFFFFFF, index, count)


def unpack(data):
    """Returns (stream, seq, ts_ns, frame, index, count), or None if the
    datagram is not ours."""
    if len(data) < HEADER_SIZE or data[:2] != MAGIC:
        return None
    _, stream, _, seq, ts_ns, frame, index, count = _HEADER.unpack_from(data)
    return stream, seq, ts_ns, frame, index, count
