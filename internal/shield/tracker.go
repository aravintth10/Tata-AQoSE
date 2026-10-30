package shield

import (
	"sync"
	"time"

	"adaptiveqos/contracts"
)

// Tracker keeps the shield's state between proposals. The controller tells
// it what exists (flows, rates) and what the enforcer really applied; it
// answers proposals with Check.
type Tracker struct {
	mu    sync.Mutex
	cfg   Config
	state State
	// Counts of verdicts by code, for the metrics exporter.
	counts map[Code]uint64
}

func NewTracker(cfg Config) (*Tracker, error) {
	if err := cfg.Validate(); err != nil {
		return nil, err
	}
	return &Tracker{
		cfg:    cfg,
		state:  State{Flows: map[string]Flow{}, Rates: map[string]Rate{}},
		counts: map[Code]uint64{},
	}, nil
}

// Outcome is a verdict and what the controller must know besides it.
type Outcome struct {
	Verdict contracts.PolicyVerdict
	Code    Code
	// Unlabel is set when this proposal pinned its flow: the flow's label
	// must be cleared, which leaves it to plain CAKE for the rest of its
	// life.
	Unlabel bool
}

// FlowSeen adds a flow to the flow table, unlabelled. Seeing a known flow
// again changes nothing.
func (t *Tracker) FlowSeen(flowID, hostID string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if _, known := t.state.Flows[flowID]; !known {
		t.state.Flows[flowID] = Flow{HostID: hostID}
	}
}

// FlowGone forgets a flow that has ended.
func (t *Tracker) FlowGone(flowID string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	delete(t.state.Flows, flowID)
}

// RateRead records a shaper rate read back from CAKE that the shield did
// not approve itself: the rate at start-up, or one the watchdog set.
func (t *Tracker) RateRead(direction string, kbit int) {
	t.mu.Lock()
	defer t.mu.Unlock()
	rate := t.state.Rates[direction]
	rate.Kbit = kbit
	t.state.Rates[direction] = rate
}

// SetStarvation raises or clears the starvation alarm.
func (t *Tracker) SetStarvation(on bool) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.state.Starvation = on
}

// Decide gives the verdict for a proposal at time nowNs. A verdict other
// than reject must be followed by Applied once the enforcer has succeeded;
// until then the state is unchanged.
func (t *Tracker) Decide(p *contracts.PolicyProposal, nowNs int64) Outcome {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.state.NowNs = nowNs
	t.state.Remarks = keepRecent(t.state.Remarks, nowNs, int64(time.Second))
	out := Outcome{}
	out.Verdict, out.Code = Check(t.cfg, &t.state, p)
	t.counts[out.Code]++
	if out.Code == FlipCap {
		flow := t.state.Flows[p.FlowLabel.FlowID]
		flow.Pinned, flow.Tin, flow.Changes = true, "", nil
		t.state.Flows[p.FlowLabel.FlowID] = flow
		out.Unlabel = true
	}
	return out
}

// Applied records that the enforcer applied a verdict, at time nowNs.
func (t *Tracker) Applied(p *contracts.PolicyProposal, v contracts.PolicyVerdict, nowNs int64) {
	if p == nil || v.Applied == nil {
		return
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	switch {
	case p.Kind == "flow_label" && p.FlowLabel != nil:
		flow, known := t.state.Flows[p.FlowLabel.FlowID]
		if !known || flow.Pinned || flow.Tin == v.Applied.Tin {
			return
		}
		if flow.Tin != "" {
			window := t.cfg.FlipWindowMs * int64(time.Millisecond)
			flow.Changes = append(keepRecent(flow.Changes, nowNs, window), nowNs)
			t.state.Remarks = append(t.state.Remarks, nowNs)
		}
		flow.Tin = v.Applied.Tin
		t.state.Flows[p.FlowLabel.FlowID] = flow
	case p.Kind == "shaper_rate" && p.ShaperRate != nil:
		rate := t.state.Rates[p.ShaperRate.Direction]
		if v.Applied.RateKbit > rate.Kbit {
			rate.LastIncreaseNs, rate.Increased = nowNs, true
		}
		rate.Kbit = v.Applied.RateKbit
		t.state.Rates[p.ShaperRate.Direction] = rate
	}
}

// Flow returns what the shield knows about a flow.
func (t *Tracker) Flow(flowID string) (Flow, bool) {
	t.mu.Lock()
	defer t.mu.Unlock()
	flow, known := t.state.Flows[flowID]
	return flow, known
}

// Rate returns the shaper rate the shield holds for a direction.
func (t *Tracker) Rate(direction string) (int, bool) {
	t.mu.Lock()
	defer t.mu.Unlock()
	rate, known := t.state.Rates[direction]
	return rate.Kbit, known
}

// Counts returns how many verdicts each check has decided so far.
func (t *Tracker) Counts() map[Code]uint64 {
	t.mu.Lock()
	defer t.mu.Unlock()
	out := make(map[Code]uint64, len(t.counts))
	for code, n := range t.counts {
		out[code] = n
	}
	return out
}

// keepRecent returns the times within window before now, in a new slice.
func keepRecent(times []int64, now, window int64) []int64 {
	var kept []int64
	for _, ts := range times {
		if ts <= now && now-ts < window {
			kept = append(kept, ts)
		}
	}
	return kept
}

// Starvation watches for a host that has a queue and almost no throughput.
// The alarm is up while any host has been in that state for Hold.
type Starvation struct {
	Hold    time.Duration // how long before it counts (2 s)
	MinKbit float64       // "almost no throughput" is below this
	since   map[string]int64
}

func NewStarvation() *Starvation {
	return &Starvation{Hold: 2 * time.Second, MinKbit: 16, since: map[string]int64{}}
}

// Observe takes one reading for a host: whether packets of its are queued,
// and its throughput over the last interval.
func (s *Starvation) Observe(hostID string, nowNs int64, backlogged bool, kbit float64) {
	if !backlogged || kbit >= s.MinKbit {
		delete(s.since, hostID)
		return
	}
	if _, already := s.since[hostID]; !already {
		s.since[hostID] = nowNs
	}
}

// Forget drops a host that has left.
func (s *Starvation) Forget(hostID string) { delete(s.since, hostID) }

// Alarm reports whether any host is starved at nowNs, and which.
func (s *Starvation) Alarm(nowNs int64) (bool, []string) {
	var hosts []string
	for host, since := range s.since {
		if nowNs-since >= int64(s.Hold) {
			hosts = append(hosts, host)
		}
	}
	return len(hosts) > 0, hosts
}
