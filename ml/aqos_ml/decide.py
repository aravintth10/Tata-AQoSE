"""The decision engine: turns a classification into one tin for the flow,
and into the policy proposal that goes to the safety shield.

Two rules, selected by `mode`:

    expected_damage (O2)  For each tin, sum probability times damage over
                          the classes; take the tin with the lowest sum.
    top_label (O1)        The tin of the most likely class.

Both share everything else: the bar for demotion to Bulk, the choice of
DSCP, the reason string and the fallback. Any error gives Best Effort, which
is what plain CAKE does with an unmarked flow.
"""

import os
import time

from . import evaluate

CLASSES, TINS, TAXONOMY = evaluate.CLASSES, evaluate.TINS, evaluate.TAXONOMY
MODES = ("expected_damage", "top_label")
# Classes a lease protects: the ones a person notices within a second.
INTERACTIVE = ("telephony", "realtime_interactive", "multimedia_conferencing")
SAFE_TIN = "best_effort"
# When the damage table cannot separate tins, the flow goes to the tin its
# most likely class has in the taxonomy (RFC 4594's own advice), unless
# that is Bulk: demotion needs evidence, not a tie. Failing that, in this
# order.
_TIE_ORDER = {"best_effort": 0, "video": 1, "voice": 2, "bulk": 3}

PLAIN = {
    "telephony": "a voice call",
    "realtime_interactive": "a game or other real-time traffic",
    "multimedia_conferencing": "a video call",
    "multimedia_streaming": "streaming video",
    "standard": "ordinary web traffic",
    "high_throughput_data": "a large download",
    "low_priority_data": "background transfer",
}
TIN_NAME = {"voice": "Voice", "video": "Video", "best_effort": "Best Effort", "bulk": "Bulk"}


def new_id():
    return os.urandom(8).hex()


class Engine:
    """damage is {true class: {tin: damage}} as evaluate.load_damage_table
    returns it. demotion_bar is how sure the engine must be that a flow
    belongs in Bulk before it puts it there. lease_multiplier scales the
    damage of leaving a leased interactive class in Best Effort or Bulk.
    tie_margin is how close two tins' expected damages must be to count as
    equal. voice_bar is how sure the engine must be that a flow is a call
    or a game before it puts it in Voice."""

    def __init__(self, damage, mode="expected_damage", demotion_bar=0.8, lease_multiplier=5.0, rule_name=None,
                 tie_margin=0.05, voice_bar=0.5, bulk_guard=True):
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, not {mode!r}")
        self.damage, self.mode = damage, mode
        # What proposals call the rule. The hand-written rules (B4) decide
        # by top label and say "rules".
        self.rule_name = rule_name or mode
        self.demotion_bar, self.lease_multiplier = demotion_bar, lease_multiplier
        # Expected damages closer than this are a tie. The measured table
        # is rounded to half a point, so smaller differences are arithmetic,
        # not measurement.
        self.tie_margin = tie_margin
        # Voice has priority over everything up to a quarter of the link.
        # The damage table measured each kind of flow there alone, found
        # that nearly all of them do no harm, and so cannot say what
        # happens when all of them are sent there together: in the smoke
        # runs a stream and a cloud game shared Voice, outgrew its quarter
        # and the game lost half its frames. Like Bulk, Voice needs
        # evidence: the flow is probably one of the classes Voice is for.
        self.voice_bar = voice_bar
        # Bulk also needs evidence besides the model's confidence. Trained
        # without a data source and tested on it, the model sent 16% of
        # unseen video-call records to Bulk, confidently wrong. Two facts
        # the model cannot be wrong about are checked: a flow is not
        # demoted on its first eight packets alone, only once it has run
        # for a few seconds; and a UDP flow is never demoted, because the
        # traffic that suffers in Bulk (calls, games, cloud gaming) is
        # UDP, and the traffic that belongs there is mostly TCP. The cost:
        # peer-to-peer traffic over UDP stays in Best Effort, where plain
        # CAKE would have it. With the guard, the held-out video calls,
        # cloud games and downloads went to Bulk 0% of the time.
        self.bulk_guard = bulk_guard
        self._voice_classes = [c for c in CLASSES if TAXONOMY["classes"][c]["tin"] == "voice"]
        # The classes whose least damaging tin is Bulk according to the
        # table. In top-label mode it is the taxonomy that says so.
        self._bulk_classes = {
            "expected_damage": [c for c in CLASSES if damage[c]["bulk"] <= min(damage[c].values())],
            "top_label": [c for c in CLASSES if TAXONOMY["classes"][c]["tin"] == "bulk"],
        }[mode]

    def _table(self, leased_classes):
        """The damage table with leases applied. Leases change the table,
        never the probabilities."""
        if not leased_classes:
            return self.damage
        table = {cls: dict(row) for cls, row in self.damage.items()}
        for cls in leased_classes:
            if cls in INTERACTIVE:
                for tin in ("best_effort", "bulk"):
                    table[cls][tin] *= self.lease_multiplier
        return table

    def expected(self, probs, leased_classes=()):
        """{tin: expected damage of putting the flow there}."""
        table = self._table(leased_classes)
        return {tin: sum(probs[cls] * table[cls][tin] for cls in CLASSES) for tin in TINS}

    def choose(self, classification, leased_classes=(), record=None):
        """Returns (tin, top class, expected damage per tin, held back).
        held back is True when the rule chose Bulk and the demotion bar,
        or the guard on Bulk, turned that into Best Effort. record is the
        feature record the classification is about; without it the guard
        cannot look and does not hold anything back."""
        probs = _checked(classification["probs"])
        top = max(CLASSES, key=lambda c: (probs[c], -CLASSES.index(c)))
        expected = self.expected(probs, leased_classes)
        if self.mode == "top_label":
            tin = TAXONOMY["classes"][top]["tin"]
        else:
            allowed = list(TINS)
            if sum(probs[c] for c in self._voice_classes) <= self.voice_bar:
                allowed.remove("voice")
            lowest = min(expected[t] for t in allowed)
            tied = [t for t in allowed if expected[t] - lowest <= self.tie_margin]
            own = TAXONOMY["classes"][top]["tin"]
            tin = own if own in tied and own != "bulk" else min(tied, key=_TIE_ORDER.get)
        held = tin == "bulk" and (sum(probs[c] for c in self._bulk_classes) <= self.demotion_bar
                                  or self.guarded(record))
        return (SAFE_TIN if held else tin), top, expected, held

    def guarded(self, record):
        """Whether the guard keeps this flow out of Bulk whatever the model says."""
        return bool(self.bulk_guard and record is not None
                    and (record.get("kind") == "initial" or record.get("protocol") == 17))

    def tin(self, classification, record=None):
        """The tin alone, in the form evaluate.score takes as `decide`."""
        return self.choose(classification, record=record)[0]

    def propose(self, classification, host_id, previous_tin=None, leased_classes=(),
                source="decision_engine", ts_ns=None, proposal_id=None, record=None):
        """A policy proposal for the flow the classification is about. Never
        raises: anything wrong gives the fallback proposal for Best Effort."""
        ts_ns = time.monotonic_ns() if ts_ns is None else ts_ns
        proposal_id = proposal_id or new_id()
        flow_id = classification.get("flow_id") if isinstance(classification, dict) else None
        try:
            tin, top, expected, held = self.choose(classification, leased_classes, record)
            dscp = TAXONOMY["classes"][top]["dscp"] if TAXONOMY["classes"][top]["tin"] == tin \
                else TAXONOMY["tins"][tin]["dscp"]
            label = {
                "flow_id": flow_id, "host_id": host_id, "tin": tin, "dscp": dscp,
                "previous_tin": previous_tin, "top_class": top,
                "expected_damage": round(expected[tin], 6), "decision_rule": self.rule_name,
            }
            reason = self._reason(classification["probs"], tin, top, expected, held,
                                  bool(set(leased_classes) & set(INTERACTIVE)), held and self.guarded(record))
        except Exception as err:  # noqa: BLE001 - the whole point is to catch everything
            label = {
                "flow_id": flow_id, "host_id": host_id, "tin": SAFE_TIN,
                "dscp": TAXONOMY["tins"][SAFE_TIN]["dscp"], "previous_tin": previous_tin,
                "top_class": None, "expected_damage": None, "decision_rule": "fallback",
            }
            reason = f"Could not decide ({type(err).__name__}: {err}); left in Best Effort."
        return {
            "schema_version": 1, "proposal_id": proposal_id, "ts_ns": ts_ns, "source": source,
            "kind": "flow_label", "reason": reason, "flow_label": label,
        }

    def _reason(self, probs, tin, top, expected, held, leased, guarded=False):
        text = f"Looks like {PLAIN[top]} ({probs[top]:.0%} confident)"
        top_tin = TAXONOMY["classes"][top]["tin"]
        if guarded:
            text += ("; not demoted to Bulk on its first packets, or while it is UDP, as calls and games are,"
                     " so it stays in Best Effort.")
        elif held:
            text += (f"; not sure enough to demote it to Bulk (needs over {self.demotion_bar:.0%}),"
                     " so it stays in Best Effort.")
        elif self.mode == "top_label":
            text += f"; {TIN_NAME[tin]} is the tin for that class."
        elif tin != "voice" and expected["voice"] < expected[tin] - self.tie_margin:
            text += (f"; not sure enough that it is a call or a game to put it in Voice (needs over"
                     f" {self.voice_bar:.0%}), so {TIN_NAME[tin]}, the next lowest expected damage.")
        elif tin == top_tin:
            text += f"; the {TIN_NAME[tin]} tin has the lowest expected damage."
        else:
            text += (f"; {TIN_NAME[tin]} has the lowest expected damage ({expected[tin]:.2f}, against"
                     f" {expected[top_tin]:.2f} for {TIN_NAME[top_tin]}).")
        if leased:
            text += " A priority lease is active for this device."
        return text


def _checked(probs):
    """The seven probabilities as floats, or an error. A classification
    that is not a distribution must not be decided on."""
    values = {cls: float(probs[cls]) for cls in CLASSES}
    if any(not 0.0 <= v <= 1.0 for v in values.values()):  # also false for NaN
        raise ValueError("a probability is outside 0 to 1")
    if abs(sum(values.values()) - 1.0) > 1e-6:
        raise ValueError(f"probabilities sum to {sum(values.values()):.6f}, not 1")
    return values
