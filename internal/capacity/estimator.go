// Package capacity keeps CAKE's shaper rates just under what the link can
// really carry. A shaper above the link's true rate moves the queue out of
// CAKE, into a buffer nobody manages; one far below it wastes the link.
//
// The estimator watches the round-trip delay to a few reflectors on the
// far side of the link. Delay rising above its usual value on most of
// them means a queue is building where CAKE cannot see it: cut the rate.
// Delay calm while the link is busy: try a little more.
//
// It only proposes. Every proposal goes through the safety shield, which
// limits how far and how often a rate may move.
package capacity

import (
	"fmt"
	"math"
	"sort"
	"time"

	"adaptiveqos/contracts"
)

// Config holds the estimator's settings. Starting values from the
// specification, section 9.3.
type Config struct {
	Reflectors int // how many reflectors are probed
	// ThresholdMs is how far above its baseline a reflector's delay must
	// be to count as a sign of a queue.
	ThresholdMs float64
	// Consecutive is how many probes in a row must show that.
	Consecutive int
	// MinAnswering is how many reflectors must be answering for the delay
	// to mean anything. With fewer, the rate goes to the minimum and stays.
	MinAnswering int
	// Silent is how long a reflector may go without any answer before it
	// counts as not answering. It must be longer than the link's buffer
	// can delay a probe, or a full buffer looks like a dead reflector.
	Silent time.Duration
	// CutTo is the share of the rate actually achieved that a cut asks
	// for, where the achieved rate can be measured.
	CutTo float64
	// CutBlind is the share of the current rate a cut asks for where it
	// cannot: CAKE counts what it sent, and for the upload that is before
	// the bottleneck, not after.
	CutBlind float64
	// CutHold is the shortest time between two such blind cuts, so that
	// one queue is not answered twice.
	CutHold time.Duration
	// UpWait: while the download is busy, the probes' delay may as well be
	// a queue on the way down, and that one can be measured and cut
	// properly. The upload is then cut only once the download has been
	// cut and the delay is still high this long after. Round-trip probes
	// cannot tell the two directions apart; in S5 three streams queued on
	// the way down, the upload was cut blind eleven times on a link whose
	// upload never changed, and the call on it lost 45% of its frames.
	UpWait time.Duration
	// DownBusy is the share of the download's rate in use above which the
	// download counts as a possible cause.
	DownBusy float64
	// Settle is the shortest time between two measured cuts: long enough
	// for the first to show in the delay.
	Settle time.Duration
	// DrainMs is how far the delay must have fallen over the last half
	// second for the queue to count as draining. While it is draining, the
	// last cut was enough and no further one is made, however high the
	// delay still is: a full buffer takes seconds to empty.
	DrainMs float64
	// Idle is the share of the shaper rate below which the download counts
	// as carrying nothing.
	Idle float64
	// Fresh is how far back readings of what got through are used.
	Fresh time.Duration
	// LoadHigh is the share of the shaper rate in use above which a
	// direction counts as busy. Only a busy direction can be the one whose
	// queue is building, and only a busy one is worth raising.
	LoadHigh float64
	// StepUp is the increase asked for at a time, as a fraction.
	StepUp float64
	// RaiseEvery is the shortest time between two increases in one
	// direction. The shield enforces its own; asking more often would only
	// fill the log with rejections.
	RaiseEvery time.Duration
	// Calm is how long the delay must have been low before an increase.
	Calm time.Duration
	// BaseKbit is the configured rate each direction drifts back to.
	BaseKbit map[string]int
	// MinKbit is the rate used when the reflectors cannot be heard, unless
	// DeafHold is set.
	MinKbit map[string]int
	// DeafHold: when the reflectors cannot be heard, leave the rates where
	// they are instead of going to MinKbit. Silence says nothing about the
	// link. In scenario S10 only the probe targets were unreachable, and
	// going to the minimum took a third of a call's upload away on a link
	// that was fine. With nothing to go on the system should do what it
	// was doing, which at worst is what plain CAKE does: shape to a rate
	// nobody is checking.
	DeafHold bool
	// BaselineRise is how much of an increase in delay the baseline takes
	// up per probe. Decreases are taken at once: the lowest delay seen
	// lately is the path with no queue.
	BaselineRise float64
}

// DefaultConfig is the starting values around the given rates.
func DefaultConfig(baseUp, baseDown, minUp, minDown int) Config {
	return Config{
		Reflectors: 3, ThresholdMs: 15, Consecutive: 3, MinAnswering: 2, Silent: 3 * time.Second,
		CutTo: 0.90, CutBlind: 0.75, CutHold: time.Second, UpWait: 2 * time.Second, DownBusy: 0.5, Settle: 300 * time.Millisecond, DrainMs: 10, Idle: 0.02,
		Fresh:    600 * time.Millisecond,
		LoadHigh: 0.75, StepUp: 0.10, RaiseEvery: 10 * time.Second, Calm: 2 * time.Second,
		BaseKbit:     map[string]int{"up": baseUp, "down": baseDown},
		MinKbit:      map[string]int{"up": minUp, "down": minDown},
		BaselineRise: 0.002,
		DeafHold:     true,
	}
}

type reflector struct {
	baseline  float64
	have      bool
	over      int     // intervals in a row with delay above the threshold
	delay     float64 // the latest delay above the baseline
	answering bool
	// onTime is set when the latest interval brought an answer with no
	// extra delay; waitingOnly when it brought none and the delay is only
	// inferred from a probe still being out.
	onTime, waitingOnly bool
}

type reading struct {
	atNs int64
	kbit float64
}

type direction struct {
	shaperKbit int
	// achievedKbit is the most that got through in any recent reading. The
	// most, because what the link carried a moment ago it can carry; the
	// least would be whatever a quiet moment happened to show.
	achievedKbit float64
	readings     []reading
	have         bool
	lastCutNs    int64
	firstCutNs   int64 // the first cut since the delay was last low
	lastRaiseNs  int64
	cut, raised  bool
}

// Estimator is the state. It is not safe for concurrent use; the caller
// serialises Probe, Load and Decide.
type Estimator struct {
	cfg        Config
	reflectors []reflector
	dirs       map[string]*direction
	calmSince  int64     // when the delay was last seen high
	deaf       bool      // too few reflectors answering
	recent     []float64 // the delay at the last few decisions, while congested
}

func New(cfg Config) *Estimator {
	return &Estimator{
		cfg: cfg, reflectors: make([]reflector, cfg.Reflectors),
		dirs: map[string]*direction{"up": {}, "down": {}},
	}
}

// Probe takes what one interval showed for one reflector: the round-trip
// time of an answer if one arrived, how long the oldest unanswered probe
// has been out, and how long since any answer.
func (e *Estimator) Probe(index int, rttMs float64, answered bool, waitingMs, silentMs float64) {
	if index < 0 || index >= len(e.reflectors) {
		return
	}
	r := &e.reflectors[index]
	// Silent means nothing has come back for a long time, not that an
	// answer is late. Late is delay, and delay is what is being measured.
	r.answering = silentMs < float64(e.cfg.Silent)/float64(time.Millisecond)
	if !r.answering {
		r.over = 0
		return
	}
	delay := waitingMs // at least this much, for the probe still out
	timed := answered && !math.IsNaN(rttMs) && rttMs >= 0
	r.onTime, r.waitingOnly = false, !timed
	if timed {
		switch {
		case !r.have || rttMs < r.baseline:
			r.baseline, r.have = rttMs, true
		default:
			r.baseline += (rttMs - r.baseline) * e.cfg.BaselineRise
		}
		delay = math.Max(delay, rttMs)
	}
	if !r.have {
		return // no answer yet to know the path by
	}
	r.delay = delay - r.baseline
	if r.delay > e.cfg.ThresholdMs {
		r.over++
	} else {
		r.over = 0
		r.onTime = timed
	}
}

// delay is the middle reflector's delay above its baseline.
func (e *Estimator) delay() float64 {
	var delays []float64
	for _, r := range e.reflectors {
		if r.answering && r.have {
			delays = append(delays, r.delay)
		}
	}
	if len(delays) == 0 {
		return 0
	}
	sort.Float64s(delays)
	return delays[len(delays)/2]
}

// Delay is the probes' delay above their baseline in milliseconds, for the
// verifier. ok is false while too few reflectors answer to say.
func (e *Estimator) Delay() (ms float64, ok bool) {
	answering := 0
	for _, r := range e.reflectors {
		if r.answering && r.have {
			answering++
		}
	}
	return e.delay(), answering >= 2 && !e.deaf
}

// Load takes what CAKE reports for a direction: the rate it is shaping to
// and the rate that went through it since the last reading. Readings
// should come several times a second. A cut is sized by what got through,
// and a reading a second old describes the link before whatever just
// happened to it: the first version read once a second, and when eight
// connections started at once it sized its cut by the quiet second before.
func (e *Estimator) Load(dir string, shaperKbit int, achievedKbit float64, nowNs int64) {
	d, ok := e.dirs[dir]
	if !ok || shaperKbit < 1 {
		return
	}
	kept := d.readings[:0]
	for _, r := range d.readings {
		if nowNs-r.atNs < int64(e.cfg.Fresh) {
			kept = append(kept, r)
		}
	}
	d.readings = append(kept, reading{nowNs, achievedKbit})
	d.shaperKbit, d.have, d.achievedKbit = shaperKbit, true, 0
	for _, r := range d.readings {
		d.achievedKbit = math.Max(d.achievedKbit, r.kbit)
	}
}

// Applied tells the estimator the rate a direction was really set to, as
// soon as the enforcer has set it. Without this it would go on proposing
// from the rate it last read, which the shield would reject as stale.
func (e *Estimator) Applied(dir string, shaperKbit int) {
	if d, ok := e.dirs[dir]; ok && shaperKbit >= 1 {
		d.shaperKbit = shaperKbit
		// What got through before was measured under the old rate; it
		// cannot be more than the new one from now on.
		d.achievedKbit = math.Min(d.achievedKbit, float64(shaperKbit))
		for i := range d.readings {
			d.readings[i].kbit = math.Min(d.readings[i].kbit, float64(shaperKbit))
		}
	}
}

// Congested reports whether most answering reflectors have shown raised
// delay for enough probes in a row, and how many reflectors are answering.
func (e *Estimator) Congested() (congested bool, answering int) {
	over, late, onTime := 0, 0, 0
	for _, r := range e.reflectors {
		if !r.answering {
			continue
		}
		answering++
		if r.onTime {
			onTime++
		}
		if r.over >= e.cfg.Consecutive {
			over++
			if !r.waitingOnly {
				late++
			}
		}
	}
	if answering == 0 || 2*over <= answering {
		return false, answering
	}
	// A queue on the link delays every probe. If some reflector is
	// answering on time while others say nothing at all, those others are
	// unreachable, not slow: an outage beyond the link, and no rate of
	// ours would help. Only answers that did come back late outvote one
	// that came back on time.
	if onTime > 0 && 2*late <= answering {
		return false, answering
	}
	return true, answering
}

// Decide returns the rate changes to propose now. Call it after each round
// of probes.
func (e *Estimator) Decide(nowNs int64) []contracts.PolicyProposal {
	if e.calmSince == 0 {
		e.calmSince = nowNs
	}
	congested, answering := e.Congested()
	var out []contracts.PolicyProposal
	propose := func(dir string, d *direction, kbit int, reason string) {
		previous := d.shaperKbit
		out = append(out, contracts.PolicyProposal{
			SchemaVersion: contracts.SchemaVersion, TsNs: nowNs, Source: "capacity_estimator",
			Kind: "shaper_rate", Reason: reason,
			ShaperRate: &contracts.ShaperRate{Direction: dir, RateKbit: kbit, PreviousRateKbit: &previous},
		})
	}

	// Deaf: without the reflectors nothing is known about the link. Either
	// the rates stay where they are, or they go to the minimum, the one
	// rate that is safe whatever the link is doing.
	if answering < e.cfg.MinAnswering {
		e.deaf, e.calmSince = true, nowNs
		if e.cfg.DeafHold {
			return nil
		}
		for _, dir := range []string{"up", "down"} {
			if d := e.dirs[dir]; d.have && d.shaperKbit > e.cfg.MinKbit[dir] {
				propose(dir, d, e.cfg.MinKbit[dir], fmt.Sprintf(
					"Only %d of %d delay probes are being answered; holding the %sload rate at its minimum.",
					answering, e.cfg.Reflectors, dir))
			}
		}
		return out
	}
	e.deaf = false

	if !congested {
		e.recent = e.recent[:0]
	}
	if congested {
		e.calmSince = nowNs
		// Is the queue draining? Compare the delay with what it was half
		// a second ago.
		e.recent = append(e.recent, e.delay())
		if len(e.recent) > drainWindow {
			e.recent = e.recent[len(e.recent)-drainWindow:]
		}
		draining := len(e.recent) == drainWindow && e.recent[drainWindow-1] < e.recent[0]-e.cfg.DrainMs
		for _, dir := range []string{"up", "down"} {
			d := e.dirs[dir]
			if !d.have {
				continue
			}
			var target int
			var how string
			if dir == "down" {
				// For the download CAKE sits after the bottleneck, so what
				// it passed in the last second is what the link carried.
				if d.achievedKbit < e.cfg.Idle*float64(d.shaperKbit) {
					continue // nothing is coming down: the queue is not here
				}
				// After a cut, wait for it to show; and if the delay is
				// then falling, the cut was enough. Cutting again for as
				// long as the buffer takes to empty would chase the rate
				// far below the link.
				if d.cut && (nowNs-d.lastCutNs < int64(e.cfg.Settle) || draining) {
					continue
				}
				target = int(e.cfg.CutTo * math.Min(d.achievedKbit, float64(d.shaperKbit)))
				how = fmt.Sprintf("%.0f kbit/s got through a moment ago", d.achievedKbit)
			} else {
				// For the upload CAKE sits before the bottleneck and
				// cannot see what got through. Cut by a fixed share, and
				// only if the upload is busy enough to be the cause.
				if d.achievedKbit < e.cfg.LoadHigh*float64(d.shaperKbit)/2 {
					continue
				}
				if d.cut && (nowNs-d.lastCutNs < int64(e.cfg.CutHold) || draining) {
					continue
				}
				if down := e.dirs["down"]; down.have && down.achievedKbit >= e.cfg.DownBusy*float64(down.shaperKbit) &&
					(!down.cut || nowNs-down.firstCutNs < int64(e.cfg.UpWait)) {
					continue // the download first: its queue can be measured
				}
				target = int(e.cfg.CutBlind * float64(d.shaperKbit))
				how = "what gets through cannot be measured from here, so by a fixed share"
			}
			target = max(target, e.cfg.MinKbit[dir])
			if target >= d.shaperKbit {
				continue
			}
			if !d.cut {
				d.firstCutNs = nowNs
			}
			d.cut, d.lastCutNs = true, nowNs
			propose(dir, d, target, fmt.Sprintf(
				"Delay rose on most probes, so a queue is building beyond the router; lowering the %sload rate (%s).",
				dir, how))
		}
		return out
	}

	if nowNs-e.calmSince < int64(e.cfg.Calm) {
		return out
	}
	for _, d := range e.dirs {
		d.cut = false // a new queue, when it comes, is a new matter
	}
	for _, dir := range []string{"up", "down"} {
		d := e.dirs[dir]
		base := e.cfg.BaseKbit[dir]
		if !d.have || d.shaperKbit >= base {
			continue
		}
		if d.raised && nowNs-d.lastRaiseNs < int64(e.cfg.RaiseEvery) {
			continue
		}
		busy := d.achievedKbit >= e.cfg.LoadHigh*float64(d.shaperKbit)
		target := min(base, max(d.shaperKbit+1, int(float64(d.shaperKbit)*(1+e.cfg.StepUp))))
		reason := fmt.Sprintf("Delay has stayed low with the %sload busy; trying a higher rate.", dir)
		if !busy {
			reason = fmt.Sprintf("Delay is low and the %sload is quiet; returning toward the configured rate.", dir)
		}
		d.raised, d.lastRaiseNs = true, nowNs
		propose(dir, d, target, reason)
	}
	return out
}

// drainWindow is how many decisions back the delay is compared with: six
// at 100 ms apart is half a second.
const drainWindow = 6

// Deaf reports whether too few reflectors are answering.
func (e *Estimator) Deaf() bool { return e.deaf }
