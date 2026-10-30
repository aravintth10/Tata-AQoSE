"""Which class a flow from a public dataset belongs to.

Public captures are labelled per file: "this file is a Skype video call".
Two things follow. The file name has to be mapped to one of our classes,
and only the flows that are the activity may take the label: the DNS
lookup and the software update in the same file are not a video call.
"""

import re

# (pattern on the lower-cased file name, activity, class). First match wins,
# so the more specific names come first.
_ISCX = [
    (r"(facebook|hangouts?|skype)_?audio|voipbuster", "voice_call", "telephony"),
    (r"(facebook|hangouts?|skype)_?video", "video_call", "multimedia_conferencing"),
    (r"netflix|vimeo|youtube", "video_streaming", "multimedia_streaming"),
    (r"spotify", "audio_streaming", "multimedia_streaming"),
    (r"skype_?file|ftps|sftp|scp", "file_transfer", "high_throughput_data"),
    (r"chat|aim|icq", "chat", "standard"),
    (r"email", "email", "standard"),
]
_LABAYEN = [
    (r"^bulk", "bulk_download", "high_throughput_data"),
    (r"^video", "video_streaming", "multimedia_streaming"),
    (r"^web", "web_browsing", "standard"),
    (r"^interactive", "interactive", "standard"),
    # "idle" files are background traffic only: nothing to learn a class from.
]
# UNB Tor / non-Tor 2016. Every flow in it reaches a gateway on the same
# LAN that answers in a quarter of a millisecond and carries nearly
# everything as TCP, voice calls included. So:
#   - its calls are left out: a call forced through TCP is not what a home
#     router sees;
#   - what is natively TCP is kept, with the caveat that the gap after the
#     first packet is the gateway's, not the internet's.
# Six of its files are byte-for-byte copies of others under a second name.
_TOR = [
    (r"2-[12]\.pcap$", None, None),  # the copies
    (r"voice|audio", None, None),
    (r"p2p", "p2p", "low_priority_data"),
    (r"vimeo|youtube", "video_streaming", "multimedia_streaming"),
    (r"spotify", "audio_streaming", "multimedia_streaming"),
    (r"email|thunderbird|pop_filetransfer", "email", "standard"),
    (r"ftp|skype_transfer", "file_transfer", "high_throughput_data"),
    (r"browsing|ssl", "web_browsing", "standard"),
    (r"chat", "chat", "standard"),
]
# UNSW 2025: every capture is a cloud gaming session.
_UNSW = [(r".", "cloud_gaming", "realtime_interactive")]
# Purdue 2026: every run is a video call on Meet, Teams, Zoom or Webex. Its
# flow table holds UDP flows only (scripts/vca_flow_tables.py).
_VCA = [(r".", "video_call", "multimedia_conferencing")]
_BY_SOURCE = {"iscx-vpn-nonvpn-2016": _ISCX, "labayen-2020": _LABAYEN, "iscx-tor-nontor-2016": _TOR,
              "unsw-cloud-gaming-2025": _UNSW, "purdue-vca-2026": _VCA}

# Activities carried by one or two large flows. In their captures a small
# flow is something else that happened to be running.
DOMINANT = {"voice_call", "video_call", "video_streaming", "audio_streaming", "file_transfer", "bulk_download",
            "cloud_gaming"}
MIN_SHARE = 0.05
# Ports that are background whatever the capture is of.
BACKGROUND_PORTS = {53, 67, 68, 88, 123, 137, 138, 161, 389, 445, 1900, 5353, 5355}


def label_for(source, file):
    """Returns (activity, class), or None when the file has no label we can
    use."""
    name = file.lower()
    for pattern, activity, cls in _BY_SOURCE.get(source, []):
        if re.search(pattern, name):
            return (activity, cls) if cls else None
    return None


def session_for(source, file, folder=""):
    """The unit that must not be split between training and testing.

    Several public sets record both ends of one call as files ending in a
    and b. Those are one session: put apart, the test set would contain
    the other side of calls the model trained on.
    """
    stem = re.sub(r"\.(pcap|pcapng|cap)$", "", file.lower())
    stem = re.sub(r"(?<=\d)[ab]$", "", stem)
    # folder is given for sets that reuse file names: "ex1.pcapng" in two
    # hundred folders is two hundred sessions, not one.
    return f"{source}/{folder.lower()}/{stem}" if folder else f"{source}/{stem}"


def is_activity_flow(activity, row, file_bytes):
    """Whether a flow is the activity its file is named for.

    row needs flow_bytes, src_port and dst_port; file_bytes is the total of
    the file's flows.
    """
    if row["src_port"] in BACKGROUND_PORTS or row["dst_port"] in BACKGROUND_PORTS:
        return False
    if activity in DOMINANT:
        return file_bytes > 0 and row["flow_bytes"] / file_bytes >= MIN_SHARE
    return True
