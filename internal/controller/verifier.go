package controller

import (
	"fmt"
	"time"

	"adaptiveqos/contracts"
	"adaptiveqos/internal/cake"
	"adaptiveqos/internal/runclock"
	"adaptiveqos/internal/verify"
)

// The changes the verifier watches: the ones that can make things worse
// for traffic that was doing well. A demotion or a rate cut is the safe
// direction and is not watched.
const (
	watchPromotion = "promotion"
	watchRaise     = "rate_increase"
	watchLease     = "lease"
)

// MaxWatches bounds how many changes are waiting for a verdict. Beyond it
// a change simply goes unjudged.
const MaxWatches = 256

// HoldBack is how long a flow whose promotion was undone is kept from
// being promoted again. Without it the next window record would promote
// it, the verifier would undo that, and the shield would pin the flow.
const HoldBack = 60 * time.Second

// watch is one change waiting for its verdict.
type watch struct {
	at   float64 // seconds on the run's clock
	kind string
	// What was changed, and what it was before.
	flowID, hostID string
	fromTin, toTin contracts.Tin
	rule           string
	direction      string
	fromKbit       int
	toKbit         int
	leaseID        string
}

// VerdictLine is one line of the verifier's log.
type VerdictLine struct {
	TS float64 `json:"t_s"` // when the verdict was reached
	At float64 `json:"changed_at_s"`
	// Kind is promotion, rate_increase or lease; Target names the flow,
	// the direction or the lease.
	Kind   string `json:"kind"`
	Target string `json:"target"`
	From   string `json:"from"`
	To     string `json:"to"`
	verify.Result
	// RolledBack: undone; "" when the verdict asked for nothing, or what
	// kept the change from being undone.
	RolledBack bool   `json:"rolled_back"`
	Detail     string `json:"detail,omitempty"`
}

// tinDSCP is the code point taxonomy.json gives each tin, which a flow
// label proposal must carry.
var tinDSCP = map[contracts.Tin]string{
	contracts.TinVoice: "EF", contracts.TinVideo: "AF31", contracts.TinBestEffort: "CS0", contracts.TinBulk: "CS1",
}

func (c *Controller) addWatch(w watch) {
	if c.cfg.Verify == nil || len(c.watches) >= MaxWatches {
		return
	}
	w.at = c.cfg.Clock.NowS()
	c.watches = append(c.watches, w)
}

// watchLabel notes a flow that was just put into Voice or Video.
func (c *Controller) watchLabel(p *contracts.PolicyProposal, from, to contracts.Tin) {
	promoted := (to == contracts.TinVoice || to == contracts.TinVideo) && from != to
	if !promoted || p.Source == "verifier" || p.FlowLabel == nil {
		return
	}
	c.addWatch(watch{kind: watchPromotion, flowID: p.FlowLabel.FlowID, hostID: p.FlowLabel.HostID,
		fromTin: from, toTin: to, rule: p.FlowLabel.DecisionRule})
}

// watchRate notes a shaper rate that was just raised.
func (c *Controller) watchRate(p *contracts.PolicyProposal, direction string, from, to int) {
	if to <= from || from < 1 || p.Source == "verifier" {
		return
	}
	c.addWatch(watch{kind: watchRaise, direction: direction, fromKbit: from, toKbit: to})
}

// watchLeases notes leases that have started since the last look.
func (c *Controller) watchLeases() {
	if c.cfg.Leases == nil {
		return
	}
	for _, lease := range c.cfg.Leases.List() {
		if !c.leases[lease.ID] {
			c.leases[lease.ID] = true
			c.addWatch(watch{kind: watchLease, leaseID: lease.ID, hostID: lease.Host})
		}
	}
}

// observe adds this second to what the verifier has to go on: the delay
// in the Voice and Video tins, and the probes' delay.
func (c *Controller) observe() {
	sample := verify.Sample{TS: c.cfg.Clock.NowS()}
	for _, dev := range c.cfg.Devices {
		stats, err := c.cfg.ReadCake(dev)
		if err != nil || stats == nil {
			continue
		}
		for i, tin := range cake.Tins {
			if (tin != contracts.TinVoice && tin != contracts.TinVideo) || i >= len(stats.Tins) {
				continue
			}
			key := dev + "/" + string(tin)
			before, had := c.tinPkts[key]
			c.tinPkts[key] = stats.Tins[i].SentPackets
			// CAKE's delay figure only moves when the tin sends. From an
			// idle tin it is whatever the last packet saw, however long ago.
			if had && stats.Tins[i].SentPackets > before {
				sample.TinMs, sample.HasTin = max(sample.TinMs, stats.Tins[i].AvgDelayUs/1000), true
			}
		}
	}
	if c.cfg.Estimator != nil {
		sample.ProbeMs, sample.HasProbe = c.cfg.Estimator.Delay()
	}
	c.history = append(c.history, sample)
	keep := c.cfg.Verify.BeforeS + 2*c.cfg.Verify.WindowS + 5
	for len(c.history) > 0 && c.history[0].TS < sample.TS-keep {
		c.history = c.history[1:]
	}
}

// judge gives a verdict on every change that is now two windows old, and
// undoes the ones that made things worse.
func (c *Controller) judge() {
	now := c.cfg.Clock.NowS()
	waiting := c.watches[:0]
	for _, w := range c.watches {
		if now < w.at+2*c.cfg.Verify.WindowS {
			waiting = append(waiting, w)
			continue
		}
		line := VerdictLine{TS: now, At: w.at, Kind: w.kind, Result: verify.Judge(*c.cfg.Verify, c.history, w.at)}
		switch w.kind {
		case watchPromotion:
			line.Target, line.From, line.To = w.flowID, string(w.fromTin), string(w.toTin)
		case watchRaise:
			line.Target, line.From, line.To = w.direction, fmt.Sprint(w.fromKbit), fmt.Sprint(w.toKbit)
		case watchLease:
			line.Target, line.To = w.leaseID, w.hostID
		}
		c.count(func(n *Counts) {
			n.Watched++
			switch line.Verdict {
			case verify.Regressed:
				n.Regressed++
			case verify.Improved:
				n.Improved++
			}
		})
		if line.Verdict == verify.Regressed {
			if c.cfg.Rollback {
				line.RolledBack, line.Detail = c.rollback(w, line.Reason)
			} else {
				line.Detail = "rollback is switched off"
			}
			if line.RolledBack {
				c.count(func(n *Counts) { n.RolledBack++ })
			}
		}
		c.write(c.cfg.Verdicts, line)
	}
	c.watches = waiting
	for id, until := range c.heldBack {
		if runclock.NowNs() >= until {
			delete(c.heldBack, id)
		}
	}
}

// rollback restores what a change replaced. Like every other change it is
// a proposal to the shield, which may refuse it.
func (c *Controller) rollback(w watch, why string) (done bool, detail string) {
	reason := "Undone by the verifier: " + why + "."
	base := contracts.PolicyProposal{SchemaVersion: contracts.SchemaVersion, ProposalID: newID(),
		TsNs: runclock.NowNs(), Source: "verifier", Reason: reason}
	switch w.kind {
	case watchPromotion:
		flow, known := c.shield.Flow(w.flowID)
		if !known || flow.Tin != w.toTin {
			return false, "the flow has ended or been moved since"
		}
		// A flow that had no label before goes to Best Effort, where
		// plain CAKE would have had it.
		back := w.fromTin
		if back == "" {
			back = contracts.TinBestEffort
		}
		current := flow.Tin
		base.Kind = "flow_label"
		base.FlowLabel = &contracts.FlowLabel{FlowID: w.flowID, HostID: w.hostID, Tin: back, DSCP: tinDSCP[back],
			PreviousTin: &current, DecisionRule: w.rule}
		c.heldBack[w.flowID] = runclock.NowNs() + int64(HoldBack)
		if action := c.Propose(&base); action.Outcome != "applied" {
			delete(c.heldBack, w.flowID)
			return false, "not applied: " + action.Outcome + " " + action.Verdict.Reason
		}
		return true, ""
	case watchRaise:
		current, _ := c.shield.Rate(w.direction)
		if current != w.toKbit {
			return false, "the rate has been changed since"
		}
		base.Kind = "shaper_rate"
		base.ShaperRate = &contracts.ShaperRate{Direction: w.direction, RateKbit: w.fromKbit, PreviousRateKbit: &current}
		if action := c.ProposeRate(&base); action.Outcome != "applied" {
			return false, "not applied: " + action.Outcome + " " + action.Verdict.Reason
		}
		return true, ""
	case watchLease:
		if err := c.cfg.Leases.Delete(w.leaseID); err != nil {
			return false, err.Error()
		}
		return true, ""
	}
	return false, ""
}
