"""The narration for both parts of the AQoSE video.

PART1: the overview. Each line is anchored to the animation time (seconds in
aqose.html) at which its visual appears; scenes stretch to fit the speech.
PART2: the detailed walkthrough. Each scene's lines are cues; the scene's
animations start on the cue they illustrate (deep.html).

Numbers are written as words: the voice reads them more naturally.
"""

PART1_SCENES = [(0, 7), (7, 25.5), (25.5, 37.5), (37.5, 64.5), (64.5, 76.5), (76.5, 88.5), (88.5, 102.5), (102.5, 111)]

PART1 = [
    (0.8, "This is AQoSE: an add-on that tells Linux CAKE which connection is the call."),
    (7.6, "Here's a video call on a laptop, running smoothly."),
    (11.2, "Then the same laptop starts sharing files. CAKE splits the laptop's share equally between every connection."),
    (16.6, "So the call gets one twenty-first of what it needs."),
    (18.6, "Ninety-four percent of its frames arrive late. CAKE just doesn't know which connection is the call."),
    (26.0, "Why doesn't anything fix this?"),
    (26.8, "CAKE can prioritise marked packets, but apps rarely mark them, and internet providers often strip the marks."),
    (29.8, "Threshold tools call anything with many packets per second bulk. But video calls and cloud games send hundreds of packets a second."),
    (33.4, "qosify's default rules sent seventy-eight percent of cloud gaming flows to the slowest queue."),
    (38.0, "AQoSE reads only the first eight packets of each new connection: their sizes, the gaps between them, the ports and the protocol. Never the content."),
    (41.6, "That's twenty-eight numbers, in about an eighth of a second."),
    (44.5, "Two hundred and fifty-nine small decision trees turn them into odds, in six microseconds."),
    (50.5, "When it's unsure, it weighs those odds against the harm we measured in each CAKE lane."),
    (54.2, "Probably a backup, but maybe a game? Video is the safe choice. In the slow lane, a game would break."),
    (58.0, "Then it writes one label, once, into the kernel. CAKE applies it to every packet, and AQoSE never touches the packet path."),
    (65.0, "Back to our laptop. Now the call gets its own lane, and the file sharing keeps running behind it."),
    (70.4, "Late frames fall from ninety-four percent to zero point six. Delay drops from almost two seconds to eighteen milliseconds."),
    (77.0, "After that, a cheap check runs every second, with no model. Only three point three percent of checks lead to a new classification."),
    (82.6, "And it fails safe. Every change is checked, read back, and rolled back if delay rises. If AQoSE stops, plain CAKE is back in about two seconds."),
    (89.0, "All of this is measured in emulation. Where plain CAKE fails, AQoSE fixes it: for video calls, cloud games, a line that suddenly slows, and a replayed WhatsApp call."),
    (95.0, "In a hundred and forty paired runs, it was never worse than plain CAKE, and it beat cake-autorate twenty times out of twenty."),
    (103.0, "That's the overview. Now let's go through how it works, piece by piece."),
]

PART2 = [
    ("title", [
        "Part two. How AQoSE works, in detail.",
    ]),
    ("cake", [
        "First, what CAKE already does well. It shapes traffic just below the speed of the line, so the queue builds in the router, where it can be managed, and not in the modem's oversized buffer.",
        "It shares the line fairly: first between devices, then between each device's connections.",
        "It also has four priority lanes, called tins: Voice, Video, Best Effort and Bulk. Voice gets priority up to a quarter of the line, Video up to half, and Bulk always keeps at least a sixteenth, so it's never starved.",
        "But CAKE only uses those lanes when a packet carries a priority mark. Without one, everything lands in Best Effort, and fairness is all that's left.",
    ]),
    ("arch", [
        "AQoSE adds exactly one thing: a label on each connection. Here's where everything sits.",
        "In the kernel, every connection has an entry in the connection tracker. The low three bits of its mark choose the CAKE tin. A mark of zero means plain CAKE.",
        "On upload, one rule copies that mark onto every packet. On download, packets pass through a second device that restores the mark before a second CAKE instance.",
        "Above the kernel sits the controller, written in Go. It extracts features, asks the classifier, checks every change with a safety shield, and writes the mark.",
        "The classifier runs as its own Go service, and a separate watchdog stands by to restore plain CAKE.",
        "Remove two rules, and the whole system is off. None of AQoSE's code sits on the per-packet path.",
    ]),
    ("features", [
        "So how does it see a connection? One nftables rule sends the first eight packets of every new connection to the controller. Headers only, at most sixty-four bytes each. The kernel does the filtering.",
        "From those eight packets come fifteen numbers: eight packet sizes, signed by direction, and the seven gaps between them.",
        "Then the IP version, the protocol, and the port class of each end. Raw port numbers are never used.",
        "Once a second, the controller also reads the connection's counters for nine window statistics, like packet rate, byte rate and burstiness. That's twenty-eight numbers in all.",
        "Addresses never leave the controller. Connections and devices are known only by keyed hashes.",
    ]),
    ("model", [
        "Next, the model. We built a dataset of two hundred and forty-six thousand records from seven sources: public research datasets, our own testbed, and our own captures of WhatsApp calls and cloud gaming.",
        "Records are split by capture session, never by record. A sealed test set, twelve hundred flows from a hundred and eighty-three sessions, was opened only once, after the system was frozen.",
        "We compared a random forest with LightGBM. They tied on accuracy, but LightGBM answers ten times faster and is eight times smaller. On a router, that decides it.",
        "The model is two hundred and fifty-nine trees: thirty-seven rounds for each of seven classes, with calibrated probabilities.",
        "The trees are exported as data and run in Go. Go and Python agree on every answer, and one classification takes about six microseconds.",
        "On the sealed test set, accuracy was ninety-seven percent. Macro F one was zero point seven three, because some classes had only a handful of flows. And no video call or cloud game was sent to Bulk, where qosify's defaults sent seventy-eight percent of cloud gaming flows.",
    ]),
    ("decision", [
        "Now, the decision. A wrong lane doesn't always hurt the same. A call in Bulk is a disaster. A download in Voice is mostly harmless.",
        "So we measured it. We forced each kind of traffic into each lane, next to calls, streams and downloads, across two hundred and forty testbed runs, and scored the harm. That's the damage table.",
        "For each lane, AQoSE multiplies the odds of each class by its damage in that lane, and adds them up. The lane with the lowest expected damage wins.",
        "On top sit hard guards. Voice only if the connection is more than half likely to be a call or a game. Bulk only if the model is over eighty percent sure, never on a connection's first packets, and never for UDP, which is what calls and games use.",
        "To be honest, in our live tests this rule added little over simply taking the top label. The classifier does most of the work. But the rule holds the guards together, and it's safer when the model is unsure.",
    ]),
    ("reclass", [
        "Traffic changes. A camera turns on. A game moves from the lobby to a match. So AQoSE keeps watching, cheaply.",
        "Each class has a normal range for every window feature: from the fifth to the ninety-fifth percentile of its training flows.",
        "Every second, each connection is compared with its range. No model runs. Only if two or more features stay outside for two seconds, or about six for Best Effort and Bulk, is it classified again.",
        "Every connection is also rechecked at a random moment, thirty to sixty seconds after its last answer. Over eighty runs, a hundred and sixty-five thousand checks led to about five and a half thousand classifications. That's three point three percent.",
    ]),
    ("safety", [
        "Every change, from any part of the system, goes through the safety shield. It's a pure function: a proposal and the current state go in, and accept, clamp or reject comes out.",
        "It rejects invalid or stale proposals, a third lane change for one connection within a minute, and more than fifty re-marks a second. Rate changes are clamped to safe steps.",
        "Go's fuzzer threw ten point nine million generated proposals at it in five minutes. None broke a rule. Six bugs we planted on purpose were each caught in under a second.",
        "The enforcer treats every change as a transaction: snapshot, apply, read back, compare. A mismatch restores the snapshot.",
        "After a promotion, a verifier compares the delay in the Voice and Video lanes before and after. If delay rose by more than five milliseconds and by more than half, the change is undone.",
        "And the controller touches a heartbeat four times a second. If it goes quiet for two seconds, the watchdog removes the marking rules and restores plain CAKE. In twenty out of twenty kills, that took about two seconds.",
    ]),
    ("estimator", [
        "One more part: the line's speed. If the line slows down and the router doesn't know, the queue moves back into the modem, and everything is late again.",
        "AQoSE probes three reflectors ten times a second. When delay rises above a slowly moving baseline, it cuts the download rate to ninety percent of what actually got through. Only if the delay persists does it cut the upload.",
        "It raises the rate again slowly, ten percent at a time. And if the probes go silent, it holds still.",
        "When the line dropped to a fifth of its speed, AQoSE was under the new rate within two seconds, with a six percent error. cake-autorate took three seconds, with a twenty percent error.",
    ]),
    ("testing", [
        "How did we test all this? Everything runs in a rootless testbed on one laptop: a network namespace for each device, a router, and an internet bottleneck with a bloated, half-second buffer.",
        "Eleven traffic generators imitate calls, games, cloud games, streaming, browsing, downloads, backups and peer-to-peer. Every packet is timestamped. A frame counts as late if it arrives more than a hundred milliseconds late for a call, or fifty for a game.",
        "We ran nine home scenarios under five router setups, plus cake-autorate on two of them, with ten seeds of identical traffic each. Runs are paired by seed, and ninety repeated runs measured the noise.",
        "Then we froze the system: the code, the model, the damage table and every setting. And we ran it again: on fresh seeds, on a home it had never seen, on real WhatsApp calls replayed packet for packet, and fully over IPv6. All four hundred and ninety-two runs after the freeze were valid.",
    ]),
    ("results", [
        "Here are the results. Where a call or a game shares its device with heavy traffic, late frames fell from between eighty-three and ninety-four percent, to around one percent.",
        "Against plain CAKE, over a hundred and forty paired runs: seventy better, seventy tied, none worse. Against threshold rules, never worse. Against cake-autorate, better in all twenty.",
        "It doesn't starve anything. Bulk transfers kept a median of ninety-eight percent of their throughput, and fairness between devices didn't change.",
        "On replayed WhatsApp calls, late frames fell from fifty-seven percent to two point four, and from seventy-nine to fourteen. That second call's keyframes are bigger than the Video lane's share of a four megabit upload. The lane was right; the limit is CAKE's share.",
        "And where CAKE already works, with a call alone on its device, AQoSE simply ties it.",
    ]),
    ("limits", [
        "Now, what we haven't shown. Everything here is emulation, on one laptop. No real router, no Wi-Fi, and no real homes yet.",
        "Our generated calls don't lower their bitrate when squeezed, the way real apps do. The model is weaker on data sources it has never seen. And VPN tunnels are out of scope.",
        "We also found two problems we haven't fixed: a very fast cloud game can outgrow the Voice lane, and the line estimator overreacts when its probes come back after an outage.",
        "Next, we want to run it on a real OpenWrt router, test it with real apps live, and then run a field trial in volunteer homes.",
    ]),
    ("close", [
        "AQoSE. Recognise the call, give it its lane, and never make things worse. The report, the code and every result are on GitHub.",
    ]),
]

# How the voice should say a word. Applied to the text before synthesis only.
PRONOUNCE = {
    "AQoSE": "A-Q-O-S-E",
    "CAKE": "cake",
    "qosify": "Q-O-S-ify",
    "cake-autorate": "cake auto-rate",
    "nftables": "N-F-tables",
    "LightGBM": "Light G-B-M",
    "OpenWrt": "Open W-R-T",
    "UDP": "U-D-P",
    "IPv6": "I-P-v-six",
    "IP version": "I-P version",
    "F one": "F-one",
    "Wi-Fi": "Wi-Fi",
}
