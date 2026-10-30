package controller

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"sync"
	"sync/atomic"
	"time"

	"adaptiveqos/contracts"
	"adaptiveqos/internal/cake"
	"adaptiveqos/internal/capacity"
	"adaptiveqos/internal/enforce"
	"adaptiveqos/internal/extract"
	"adaptiveqos/internal/heartbeat"
	"adaptiveqos/internal/intent"
	"adaptiveqos/internal/reclass"
	"adaptiveqos/internal/runclock"
	"adaptiveqos/internal/shield"
	"adaptiveqos/internal/verify"
)

// Labeller is the part of the enforcer the controller uses for flows.
type Labeller interface {
	SetLabel(f enforce.Flow, tin contracts.Tin) error
}

// Shaper is the part of the enforcer the controller uses for rates.
type Shaper interface {
	SetRate(direction string, kbit int) error
}

// Service is the part of the ML service client the controller uses.
type Service interface {
	Classify(rec *contracts.FeatureRecord, previous *contracts.Tin, leased []contracts.Class) error
	Results() <-chan Result
}

// Config holds the controller's own settings. Starting values.
type Config struct {
	// MaxClassifyPerS caps classifications per second. Flows over the cap
	// stay unlabelled and are counted: a flood of new flows must not turn
	// into a flood of work.
	MaxClassifyPerS int
	// Confirm is how many window records in a row must ask for the same
	// new tin before a labelled flow is moved (starting value 2). One odd
	// window must not move a flow. A flow's first label needs no
	// confirmation.
	Confirm int
	// Heartbeat is the file the watchdog watches. Empty for none.
	Heartbeat string
	RunID     string
	Clock     runclock.Clock
	Metrics   io.Writer // metric samples, one JSON object per line; nil for none
	Actions   io.Writer // the action log, one JSON object per line; nil for none

	// Leases, if the user can give a device priority. Nil for none.
	Leases *intent.Store

	// The capacity estimator, if the configuration has one. Rounds brings
	// its probes; Shaper sets the rates it asks for; ReadCake reads what
	// went through CAKE on a device (nil means tc).
	Estimator *capacity.Estimator
	Rounds    <-chan capacity.Round
	Shaper    Shaper
	Devices   map[string]string // direction to the device shaping it
	ReadCake  func(dev string) (*cake.Stats, error)

	// Reclass, if set, watches labelled flows against the normal range of
	// their class and decides when one goes back to the classifier (chunk
	// 7A). The extractor must then send a window record every second.
	// Without it every flow is asked about again at every window record.
	Reclass *reclass.Watcher
	// AskEvery is how many window records pass between questions about a
	// flow that has no label yet, when Reclass is set (starting value 5).
	AskEvery int

	// Verify, if set, judges each risky change ten seconds on and undoes
	// one that made things worse (chunk 7B). Verdicts is its log, one JSON
	// object per line. With Rollback false it judges and logs only.
	Verify   *verify.Config
	Verdicts io.Writer
	Rollback bool
}

// Action is one line of the action log: a proposal, the shield's verdict,
// and what became of it.
type Action struct {
	TS       float64                   `json:"t_s"`
	Proposal *contracts.PolicyProposal `json:"proposal"`
	Verdict  contracts.PolicyVerdict   `json:"verdict"`
	Code     shield.Code               `json:"code"`
	// Outcome: applied, rejected, flow_gone (it ended first), mismatch
	// (read-back differed and the change was undone) or error.
	Outcome string `json:"outcome"`
	Detail  string `json:"detail,omitempty"`
}

// Counts says what became of the flows the extractor reported.
type Counts struct {
	Records     uint64 `json:"records"`      // initial feature records received
	Labelled    uint64 `json:"labelled"`     // labels written
	RateChanges uint64 `json:"rate_changes"` // shaper rates set
	OverCap     uint64 `json:"over_cap"`     // not classified: over the per-second cap
	ServiceDown uint64 `json:"service_down"` // not classified: no connection
	ServiceBusy uint64 `json:"service_busy"` // not classified: too many pending
	Timeouts    uint64 `json:"timeouts"`     // no answer within the deadline
	Refused     uint64 `json:"refused"`      // the service answered ok: false
	Rejected    uint64 `json:"rejected"`     // the shield said no
	FlowGone    uint64 `json:"flow_gone"`    // ended before it could be labelled
	Mismatch    uint64 `json:"mismatch"`     // read-back differed; undone
	Errors      uint64 `json:"errors"`
	Relabelled  uint64 `json:"relabelled"` // labels changed from one tin to another
	Windows     uint64 `json:"windows"`    // window records received
	Unchanged   uint64 `json:"unchanged"`  // window answers that agreed with the current label
	Held        uint64 `json:"held"`       // window answers waiting to be confirmed
	Triggers    uint64 `json:"triggers"`   // flows sent back for leaving their class's normal range
	Rechecks    uint64 `json:"rechecks"`   // routine rechecks of labelled flows
	Watched     uint64 `json:"watched"`    // changes the verifier judged
	Regressed   uint64 `json:"regressed"`  // of those, found to have made things worse
	Improved    uint64 `json:"improved"`   // and found to have made things better
	RolledBack  uint64 `json:"rolled_back"`
	HeldBack    uint64 `json:"held_back"` // promotions not made because the same one was just undone
}

// Controller owns the state between the parts. OnRecord and OnGone are
// called by the extractor's goroutine; Run does everything else in its own.
type Controller struct {
	cfg     Config
	service Service
	shield  *shield.Tracker
	labels  Labeller

	mu     sync.Mutex
	flows  map[string]enforce.Flow // flow_id to the conntrack tuple; addresses stay here
	asked  map[string]asked        // a new tin a flow's last window answers asked for
	hosts  map[string]bool         // host ids that have had a flow
	second int64
	sent   int // classifications asked for in this second
	counts Counts

	relabelled  uint64       // in the current second, for the metric
	lastExtract atomic.Int64 // when the extractor last completed a second
	metricsMu   sync.Mutex

	sentBytes map[string]float64 // CAKE's byte counter per direction at the last reading
	sentAtNs  map[string]int64

	windows map[string]int          // window records seen per flow
	again   map[string]bool         // flows to ask about at their next window record
	why     map[string]*reclass.Ask // why a flow's pending question was asked

	// The verifier's state, used by Run's goroutine only.
	history  []verify.Sample
	watches  []watch
	tinPkts  map[string]float64 // packets sent by the Voice and Video tins at the last reading
	leases   map[string]bool    // leases seen so far
	heldBack map[string]int64   // flows whose promotion was undone, and until when
}

func New(cfg Config, service Service, tracker *shield.Tracker, labels Labeller) *Controller {
	if cfg.Confirm < 1 {
		cfg.Confirm = 2
	}
	if cfg.ReadCake == nil {
		cfg.ReadCake = cake.Read
	}
	c := &Controller{cfg: cfg, service: service, shield: tracker, labels: labels,
		flows: map[string]enforce.Flow{}, asked: map[string]asked{}, hosts: map[string]bool{},
		sentBytes: map[string]float64{}, sentAtNs: map[string]int64{},
		windows: map[string]int{}, again: map[string]bool{}, why: map[string]*reclass.Ask{},
		tinPkts: map[string]float64{}, leases: map[string]bool{}, heldBack: map[string]int64{}}
	if c.cfg.AskEvery < 1 {
		c.cfg.AskEvery = 5
	}
	c.lastExtract.Store(runclock.NowNs())
	return c
}

// OnRecord takes a flow's initial feature record from the extractor.
func (c *Controller) OnRecord(f *extract.Flow, rec *contracts.FeatureRecord) {
	c.mu.Lock()
	c.counts.Records++
	c.flows[rec.FlowID] = enforce.Flow{Protocol: f.Key.Protocol, Src: f.Key.A, Dst: f.Key.B}
	c.hosts[rec.HostID] = true
	c.mu.Unlock()
	c.shield.FlowSeen(rec.FlowID, rec.HostID)
	c.classify(rec, nil)
}

// OnWindow takes a window record: the same flow some seconds on, now with
// its rates. The rules can say nothing without a rate, and the model says
// more with one. This plain "ask again every few seconds" is what chunk 7A
// replaces with a check against each class's normal range.
func (c *Controller) OnWindow(f *extract.Flow, rec *contracts.FeatureRecord) {
	c.count(func(n *Counts) { n.Windows++ })
	var previous *contracts.Tin
	if flow, ok := c.shield.Flow(rec.FlowID); ok {
		if flow.Pinned {
			return // nothing may change it, so nothing needs asking
		}
		if flow.Tin != "" {
			previous = &flow.Tin
		}
	}
	if c.cfg.Reclass != nil && !c.due(rec, previous) {
		return
	}
	c.classify(rec, previous)
}

// due says whether a window record is worth a question to the classifier,
// when the reclassifier is on. A labelled flow is asked about when it has
// left its class's normal range, at its routine recheck, or to confirm an
// answer that wanted it moved. An unlabelled flow is asked about every
// few seconds until it has a label.
func (c *Controller) due(rec *contracts.FeatureRecord, previous *contracts.Tin) bool {
	c.mu.Lock()
	c.windows[rec.FlowID]++
	seen, again := c.windows[rec.FlowID], c.again[rec.FlowID]
	delete(c.again, rec.FlowID)
	c.mu.Unlock()
	if previous == nil {
		return seen%c.cfg.AskEvery == 0
	}
	ask := c.cfg.Reclass.Window(rec.FlowID, *previous, rec.Window)
	if ask == nil {
		return again
	}
	c.mu.Lock()
	c.why[rec.FlowID] = ask
	if ask.Recheck {
		c.counts.Rechecks++
	} else {
		c.counts.Triggers++
	}
	c.mu.Unlock()
	return true
}

// classify sends a record to the ML service unless this second's quota is
// used up.
func (c *Controller) classify(rec *contracts.FeatureRecord, previous *contracts.Tin) {
	c.mu.Lock()
	now := runclock.NowNs() / int64(time.Second)
	if now != c.second {
		c.second, c.sent = now, 0
	}
	over := c.cfg.MaxClassifyPerS > 0 && c.sent >= c.cfg.MaxClassifyPerS
	if over {
		c.counts.OverCap++
	} else {
		c.sent++
	}
	c.mu.Unlock()
	if over {
		return
	}
	switch err := c.service.Classify(rec, previous, c.cfg.Leases.Leased(rec.HostID)); {
	case err == nil:
	case errors.Is(err, ErrBusy):
		c.count(func(n *Counts) { n.ServiceBusy++ })
	default:
		c.count(func(n *Counts) { n.ServiceDown++ })
	}
}

// HostSeen reports whether a device with this host id has had a flow.
func (c *Controller) HostSeen(hostID string) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.hosts[hostID]
}

// asked is a change of tin waiting to be confirmed by further windows.
type asked struct {
	tin   contracts.Tin
	times int
}

// confirmed says whether a proposal may go to the shield now. A first
// label always may. A change of tin may once enough answers in a row have
// asked for that same tin. A promotion on a device with a lease may at
// once: the user asked for it, and should not wait two windows to see it.
func (c *Controller) confirmed(flowID string, want contracts.Tin, waited bool) (ok, unchanged bool) {
	flow, known := c.shield.Flow(flowID)
	leased := known && c.cfg.Leases.Leased(flow.HostID) != nil
	c.mu.Lock()
	defer c.mu.Unlock()
	if !known || flow.Tin == "" {
		delete(c.asked, flowID)
		return true, false
	}
	if flow.Tin == want {
		delete(c.asked, flowID)
		return false, true
	}
	// A flow sent back for leaving its class's range has already been
	// outside it for several windows. That was the waiting.
	if waited || leased && (want == contracts.TinVoice || want == contracts.TinVideo) {
		delete(c.asked, flowID)
		return true, false
	}
	a := c.asked[flowID]
	if a.tin != want {
		a = asked{tin: want}
	}
	a.times++
	if a.times < c.cfg.Confirm {
		c.asked[flowID] = a
		return false, false
	}
	delete(c.asked, flowID)
	return true, false
}

// OnGone forgets a flow that has ended.
func (c *Controller) OnGone(f *extract.Flow) {
	c.mu.Lock()
	delete(c.flows, f.FlowID)
	delete(c.asked, f.FlowID)
	delete(c.windows, f.FlowID)
	delete(c.again, f.FlowID)
	delete(c.why, f.FlowID)
	c.mu.Unlock()
	if c.cfg.Reclass != nil {
		c.cfg.Reclass.Gone(f.FlowID)
	}
	c.shield.FlowGone(f.FlowID)
}

// OnSecond is the extractor saying it is alive.
func (c *Controller) OnSecond(nowNs int64) { c.lastExtract.Store(nowNs) }

func (c *Controller) count(fn func(*Counts)) {
	c.mu.Lock()
	fn(&c.counts)
	c.mu.Unlock()
}

// Counts returns what became of the flows so far.
func (c *Controller) Counts() Counts {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.counts
}

// handle takes one result from the ML service through the shield to the
// enforcer.
func (c *Controller) handle(r Result) {
	switch {
	case errors.Is(r.Err, ErrTimeout):
		c.count(func(n *Counts) { n.Timeouts++ })
		return
	case r.Err != nil:
		c.count(func(n *Counts) { n.ServiceDown++ })
		return
	case !r.Resp.OK || r.Resp.Proposal == nil || r.Resp.Classification == nil:
		c.count(func(n *Counts) { n.Refused++ })
		return
	}
	c.sample("inference_us", r.Resp.Classification.InferenceUs)
	proposal := r.Resp.Proposal
	// The flow a proposal names is the one that was asked about, whatever
	// the answer says.
	if proposal.FlowLabel == nil || proposal.FlowLabel.FlowID != r.FlowID {
		c.count(func(n *Counts) { n.Refused++ })
		return
	}
	c.mu.Lock()
	ask := c.why[r.FlowID]
	delete(c.why, r.FlowID)
	c.mu.Unlock()
	triggered := ask != nil && !ask.Recheck
	if triggered {
		proposal.Source = "reclassifier"
		proposal.Reason = ask.Reason + ". " + proposal.Reason
	}
	tin := proposal.FlowLabel.Tin
	if until, held := c.heldBack[r.FlowID]; held && (tin == contracts.TinVoice || tin == contracts.TinVideo) {
		if runclock.NowNs() < until {
			c.count(func(n *Counts) { n.HeldBack++ })
			return
		}
		delete(c.heldBack, r.FlowID)
	}
	settled := false
	switch ok, unchanged := c.confirmed(r.FlowID, tin, triggered); {
	case unchanged:
		c.count(func(n *Counts) { n.Unchanged++ })
		settled = true
	case !ok:
		c.count(func(n *Counts) { n.Held++ })
		// The next window record settles it, whatever the reclassifier
		// thinks of that window.
		c.mu.Lock()
		c.again[r.FlowID] = true
		c.mu.Unlock()
	default:
		settled = c.Propose(proposal).Outcome == "applied"
	}
	if settled && c.cfg.Reclass != nil && proposal.FlowLabel.TopClass != nil {
		c.cfg.Reclass.Answered(r.FlowID, *proposal.FlowLabel.TopClass)
	}
}

// Propose takes any flow-label proposal through the shield and, if it
// passes, to the enforcer. It is the only way a label reaches CAKE.
func (c *Controller) Propose(proposal *contracts.PolicyProposal) Action {
	now := runclock.NowNs()
	out := c.shield.Decide(proposal, now)
	action := Action{TS: c.cfg.Clock.NowS(), Proposal: proposal, Verdict: out.Verdict, Code: out.Code}
	flowID := ""
	if proposal != nil && proposal.FlowLabel != nil {
		flowID = proposal.FlowLabel.FlowID
	}
	c.mu.Lock()
	flow, known := c.flows[flowID]
	c.mu.Unlock()

	switch {
	case out.Unlabel && known:
		// The flow hit the flip cap: back to plain CAKE for good.
		action.Outcome = "rejected"
		if err := c.labels.SetLabel(flow, ""); err != nil && !errors.Is(err, enforce.ErrFlowGone) {
			action.Detail = "clearing the label failed: " + err.Error()
		}
		c.count(func(n *Counts) { n.Rejected++ })
	case out.Verdict.Verdict == "reject" || out.Verdict.Applied == nil:
		action.Outcome = "rejected"
		c.count(func(n *Counts) { n.Rejected++ })
	case !known:
		action.Outcome = "flow_gone"
		c.count(func(n *Counts) { n.FlowGone++ })
	default:
		previous, _ := c.shield.Flow(flowID)
		err := c.labels.SetLabel(flow, out.Verdict.Applied.Tin)
		switch {
		case err == nil:
			action.Outcome = "applied"
			c.shield.Applied(proposal, out.Verdict, runclock.NowNs())
			changed := previous.Tin != "" && previous.Tin != out.Verdict.Applied.Tin
			c.count(func(n *Counts) {
				n.Labelled++
				if changed {
					n.Relabelled++
				}
			})
			if changed {
				atomic.AddUint64(&c.relabelled, 1)
			}
			c.watchLabel(proposal, previous.Tin, out.Verdict.Applied.Tin)
		case errors.Is(err, enforce.ErrFlowGone):
			action.Outcome = "flow_gone"
			c.count(func(n *Counts) { n.FlowGone++ })
		case errors.Is(err, enforce.ErrMismatch):
			action.Outcome, action.Detail = "mismatch", err.Error()
			c.count(func(n *Counts) { n.Mismatch++ })
		default:
			action.Outcome, action.Detail = "error", err.Error()
			c.count(func(n *Counts) { n.Errors++ })
		}
	}
	c.write(c.cfg.Actions, action)
	return action
}

// ProposeRate takes a shaper-rate proposal through the shield and, if it
// passes, to the enforcer. It is the only way a rate reaches CAKE.
func (c *Controller) ProposeRate(proposal *contracts.PolicyProposal) Action {
	if proposal.ProposalID == "" {
		proposal.ProposalID = newID()
	}
	out := c.shield.Decide(proposal, runclock.NowNs())
	action := Action{TS: c.cfg.Clock.NowS(), Proposal: proposal, Verdict: out.Verdict, Code: out.Code}
	switch {
	case out.Verdict.Verdict == "reject" || out.Verdict.Applied == nil || proposal.ShaperRate == nil:
		action.Outcome = "rejected"
		c.count(func(n *Counts) { n.Rejected++ })
	case c.cfg.Shaper == nil:
		action.Outcome, action.Detail = "error", "no shaper to apply rates with"
		c.count(func(n *Counts) { n.Errors++ })
	default:
		direction, rate := proposal.ShaperRate.Direction, out.Verdict.Applied.RateKbit
		before, _ := c.shield.Rate(direction)
		err := c.cfg.Shaper.SetRate(direction, rate)
		switch {
		case err == nil:
			action.Outcome = "applied"
			c.shield.Applied(proposal, out.Verdict, runclock.NowNs())
			if c.cfg.Estimator != nil {
				c.cfg.Estimator.Applied(direction, rate)
			}
			c.count(func(n *Counts) { n.RateChanges++ })
			c.watchRate(proposal, direction, before, rate)
		case errors.Is(err, enforce.ErrMismatch):
			action.Outcome, action.Detail = "mismatch", err.Error()
			c.count(func(n *Counts) { n.Mismatch++ })
		default:
			action.Outcome, action.Detail = "error", err.Error()
			c.count(func(n *Counts) { n.Errors++ })
		}
	}
	c.write(c.cfg.Actions, action)
	return action
}

func newID() string {
	raw := make([]byte, 8)
	rand.Read(raw)
	return hex.EncodeToString(raw)
}

// probes feeds one round of delay probes to the estimator and applies
// what it decides.
func (c *Controller) probes(round capacity.Round) {
	for i := range round.RTTMs {
		c.cfg.Estimator.Probe(i, round.RTTMs[i], round.Answered[i], round.WaitingMs[i], round.SilentMs[i])
	}
	for _, proposal := range c.cfg.Estimator.Decide(runclock.NowNs()) {
		c.ProposeRate(&proposal)
	}
}

// load tells the estimator, several times a second, what went through
// CAKE since it last looked.
func (c *Controller) load() {
	now := runclock.NowNs()
	for direction, dev := range c.cfg.Devices {
		stats, err := c.cfg.ReadCake(dev)
		if err != nil || stats == nil {
			continue
		}
		sent := 0.0
		for _, tin := range stats.Tins {
			sent += tin.SentBytes
		}
		before, at := c.sentBytes[direction], c.sentAtNs[direction]
		c.sentBytes[direction], c.sentAtNs[direction] = sent, now
		if at == 0 || now <= at || sent < before {
			continue // the first reading, or the counters were reset
		}
		achieved := (sent - before) * 8 / 1000 / (float64(now-at) / 1e9)
		c.cfg.Estimator.Load(direction, int(stats.RateKbit+0.5), achieved, now)
	}
}

func (c *Controller) write(w io.Writer, v any) {
	if w == nil {
		return
	}
	line, err := json.Marshal(v)
	if err != nil {
		return
	}
	c.metricsMu.Lock()
	defer c.metricsMu.Unlock()
	w.Write(append(line, '\n')) // one write per line, so lines from other processes cannot land inside
}

func (c *Controller) sample(metric string, value float64) {
	if c.cfg.Metrics == nil {
		return
	}
	t := c.cfg.Clock.NowS()
	if t < 0 {
		return // before the run's zero: the contract has no negative time
	}
	c.write(c.cfg.Metrics, contracts.MetricSample{
		RunID: c.cfg.RunID, TS: t, Source: "controller", Metric: metric, Value: value,
	})
}

// everySecond writes the controller's own metrics.
func (c *Controller) everySecond() {
	labelled, unlabelled := 0, 0
	c.mu.Lock()
	ids := make([]string, 0, len(c.flows))
	for id := range c.flows {
		ids = append(ids, id)
	}
	c.mu.Unlock()
	for _, id := range ids {
		if flow, ok := c.shield.Flow(id); ok && flow.Tin != "" {
			labelled++
		} else {
			unlabelled++
		}
	}
	c.sample("flows_labelled", float64(labelled))
	c.sample("flows_unlabelled", float64(unlabelled))
	c.sample("reclassifications", float64(atomic.SwapUint64(&c.relabelled, 0)))
	if c.cfg.Verify != nil {
		c.observe()
		c.watchLeases()
		c.judge()
	}
}

// LoadEvery is how often CAKE's counters are read for the estimator. Each
// reading runs tc twice, a few milliseconds of work.
const LoadEvery = 200 * time.Millisecond

// ExtractorStall is how long the extractor may be silent before the
// controller stops beating, so that the watchdog steps in.
const ExtractorStall = 3 * time.Second

// Run handles results until stop is closed. The heartbeat comes from this
// loop, and only while the extractor's loop is alive too: either one
// hanging stops it.
func (c *Controller) Run(stop <-chan struct{}) {
	beat := time.NewTicker(heartbeat.Interval)
	second := time.NewTicker(time.Second)
	defer beat.Stop()
	defer second.Stop()
	var reading <-chan time.Time // never ready without an estimator
	if c.cfg.Estimator != nil {
		readings := time.NewTicker(LoadEvery)
		defer readings.Stop()
		reading = readings.C
	}
	for {
		select {
		case <-stop:
			return
		case r := <-c.service.Results():
			c.handle(r)
		case round := <-c.cfg.Rounds: // nil without an estimator, and then never ready
			c.probes(round)
		case <-beat.C:
			if c.cfg.Heartbeat != "" && runclock.NowNs()-c.lastExtract.Load() < int64(ExtractorStall) {
				heartbeat.Beat(c.cfg.Heartbeat)
			}
		case <-second.C:
			c.everySecond()
		case <-reading:
			c.load()
		}
	}
}

// Summary is what the controller writes when it stops.
func (c *Controller) Summary() string {
	n := c.Counts()
	return fmt.Sprintf("%d rate changes; %d flows seen, %d labelled, %d rejected by the shield, %d over the cap, %d without an answer "+
		"(%d timeouts, %d down, %d busy, %d refused), %d ended first, %d mismatches, %d errors",
		n.RateChanges, n.Records, n.Labelled, n.Rejected, n.OverCap, n.Timeouts+n.ServiceDown+n.ServiceBusy+n.Refused,
		n.Timeouts, n.ServiceDown, n.ServiceBusy, n.Refused, n.FlowGone, n.Mismatch, n.Errors)
}
