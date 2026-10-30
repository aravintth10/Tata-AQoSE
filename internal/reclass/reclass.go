// Package reclass watches labelled flows for a change of behaviour. A
// flow's window statistics are compared, once a second, with the normal
// range of the class it was labelled as. A flow that stays outside that
// range is sent back to the classifier; one that stays inside costs a few
// comparisons and nothing else.
//
// The ranges (envelopes.json, written by training) and the test cases in
// testdata come from the ML side. This is the check they describe.
package reclass

import (
	"encoding/json"
	"fmt"
	"math"
	"math/rand"
	"os"
	"strconv"
	"strings"
	"sync"

	"adaptiveqos/contracts"
)

// Features are the nine window statistics, in the contract's order.
var Features = []string{"pkts_up_per_s", "pkts_down_per_s", "bytes_up_per_s", "bytes_down_per_s",
	"mean_pkt_size_up", "mean_pkt_size_down", "pkt_size_spread", "rate_cv", "burstiness"}

func values(w *contracts.Window) [9]float64 {
	return [9]float64{w.PktsUpPerS, w.PktsDownPerS, w.BytesUpPerS, w.BytesDownPerS,
		w.MeanPktSizeUp, w.MeanPktSizeDown, w.PktSizeSpread, w.RateCV, w.Burstiness}
}

// Envelopes holds, per class, the range each window statistic normally
// lies in: the 5th to the 95th percentile over that class's training flows.
type Envelopes map[contracts.Class]map[string][2]float64

func Load(path string) (Envelopes, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var env Envelopes
	if err := json.Unmarshal(raw, &env); err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	for class, ranges := range env {
		for _, name := range Features {
			if r, ok := ranges[name]; !ok || r[0] > r[1] {
				return nil, fmt.Errorf("%s: class %s has no usable range for %s", path, class, name)
			}
		}
	}
	return env, nil
}

// Excursion is one statistic outside its class's range.
type Excursion struct {
	Feature   string
	Value     float64
	Low, High float64
	// Far is how far outside, in widths of the range.
	Far float64
}

// Outside counts how many of the nine statistics lie outside the range of
// class, and returns the one furthest outside. known is false for a class
// without an envelope, which is then never checked.
func (e Envelopes) Outside(class contracts.Class, w *contracts.Window) (n int, worst Excursion, known bool) {
	ranges, known := e[class]
	if !known || w == nil {
		return 0, worst, false
	}
	for i, value := range values(w) {
		r := ranges[Features[i]]
		beyond := math.Max(r[0]-value, value-r[1])
		if beyond <= 0 {
			continue
		}
		n++
		// A range can have no width (a statistic that is constant for the
		// class), so the scale has a floor.
		far := beyond / math.Max(r[1]-r[0], math.Max(math.Abs(r[1])*0.05, 1e-9))
		if far > worst.Far {
			worst = Excursion{Feature: Features[i], Value: value, Low: r[0], High: r[1], Far: far}
		}
	}
	return n, worst, true
}

// Config holds the trigger settings. Starting values, from the technical
// specification, section 9.1.
type Config struct {
	// K: a second counts as outside when at least this many statistics are
	// out of range. One alone is out of range in most seconds of a
	// perfectly normal flow.
	K int
	// Windows is how many outside checks in a row trigger a
	// reclassification; Interactive is the same for a flow in Voice or
	// Video, where being wrong costs more. 1 switches the waiting off.
	Windows, Interactive int
	// SlowEvery: a flow in Best Effort or Bulk is checked only every this
	// many seconds. It is watched for becoming interactive, which can wait
	// a moment; a call that stopped being one cannot.
	SlowEvery int
	// A flow is sent to the classifier anyway at a random moment between
	// RecheckMin and RecheckMax seconds after its last answer, to catch a
	// wrong label that happens to sit inside its envelope. 0 for never.
	RecheckMin, RecheckMax int
	// After a trigger the flow is not triggered again for this many
	// seconds, doubling each time up to BackoffMax while it stays outside.
	// A flow the classifier keeps calling the same thing, though it does
	// not look like the training flows of that class, must not be asked
	// about every second for the rest of its life.
	Backoff, BackoffMax int
}

func DefaultConfig() Config {
	return Config{K: 2, Windows: 3, Interactive: 2, SlowEvery: 2, RecheckMin: 30, RecheckMax: 60, Backoff: 5, BackoffMax: 40}
}

type state struct {
	class     contracts.Class
	age       int // seconds watched since the last answer
	outside   int // outside checks in a row
	worst     Excursion
	cooldown  int
	backoff   int
	recheckAt int
}

// Ask says why a flow should go back to the classifier.
type Ask struct {
	// Recheck is set for the periodic safety recheck, which has seen
	// nothing wrong. Its answer needs confirming like any other; a trigger
	// has already waited for several windows.
	Recheck bool
	Reason  string
}

// Watcher holds the state of every labelled flow. It is safe for
// concurrent use.
type Watcher struct {
	cfg Config
	env Envelopes

	mu    sync.Mutex
	flows map[string]*state
	rng   *rand.Rand
	// Triggers and Rechecks count what Window has asked for.
	Triggers, Rechecks uint64
}

func NewWatcher(cfg Config, env Envelopes, seed int64) *Watcher {
	if cfg.K < 1 {
		cfg.K = 1
	}
	if cfg.Windows < 1 {
		cfg.Windows = 1
	}
	if cfg.Interactive < 1 {
		cfg.Interactive = 1
	}
	if cfg.SlowEvery < 1 {
		cfg.SlowEvery = 1
	}
	return &Watcher{cfg: cfg, env: env, flows: map[string]*state{}, rng: rand.New(rand.NewSource(seed))}
}

func (w *Watcher) recheckAfter() int {
	if w.cfg.RecheckMax <= 0 {
		return math.MaxInt
	}
	return w.cfg.RecheckMin + w.rng.Intn(max(w.cfg.RecheckMax-w.cfg.RecheckMin, 0)+1)
}

// Answered records what the classifier last called a flow. The flow is
// watched against that class's range from now on. An answer naming the
// class the flow already has keeps its back-off, so that a flow which
// never fits its envelope is asked about less and less often.
func (w *Watcher) Answered(flowID string, class contracts.Class) {
	w.mu.Lock()
	defer w.mu.Unlock()
	st := w.flows[flowID]
	if st == nil || st.class != class {
		st = &state{class: class}
		w.flows[flowID] = st
	}
	st.age, st.outside, st.worst = 0, 0, Excursion{}
	st.recheckAt = w.recheckAfter()
}

// Gone forgets a flow that has ended.
func (w *Watcher) Gone(flowID string) {
	w.mu.Lock()
	defer w.mu.Unlock()
	delete(w.flows, flowID)
}

// Counts returns how many triggers and safety rechecks there have been.
func (w *Watcher) Counts() (triggers, rechecks uint64) {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.Triggers, w.Rechecks
}

// Window takes one second's window statistics for a flow labelled into
// tin. It returns nil in the usual case, when nothing needs asking.
func (w *Watcher) Window(flowID string, tin contracts.Tin, window *contracts.Window) *Ask {
	w.mu.Lock()
	defer w.mu.Unlock()
	st := w.flows[flowID]
	if st == nil {
		return nil
	}
	st.age++
	if st.cooldown > 0 {
		st.cooldown--
	}
	if st.age >= st.recheckAt {
		st.recheckAt = st.age + w.recheckAfter()
		w.Rechecks++
		return &Ask{Recheck: true, Reason: fmt.Sprintf("Routine recheck after %d s", st.age)}
	}
	interactive := tin == contracts.TinVoice || tin == contracts.TinVideo
	every, need := 1, w.cfg.Interactive
	if !interactive {
		every, need = w.cfg.SlowEvery, w.cfg.Windows
	}
	if st.age%every != 0 {
		return nil
	}
	n, worst, known := w.env.Outside(st.class, window)
	if !known {
		return nil
	}
	if n < w.cfg.K {
		// Back inside: whatever it was is over.
		st.outside, st.worst, st.backoff = 0, Excursion{}, 0
		return nil
	}
	st.outside++
	if worst.Far > st.worst.Far {
		st.worst = worst
	}
	if st.outside < need || st.cooldown > 0 {
		return nil
	}
	ask := &Ask{Reason: reason(st.class, st.worst, st.outside*every)}
	st.backoff = min(max(2*st.backoff, w.cfg.Backoff), max(w.cfg.BackoffMax, w.cfg.Backoff))
	st.cooldown, st.outside, st.worst = st.backoff, 0, Excursion{}
	w.Triggers++
	return ask
}

var names = map[contracts.Class]string{
	contracts.Telephony: "a voice call", contracts.RealtimeInteractive: "a game",
	contracts.MultimediaConferencing: "a video call", contracts.MultimediaStreaming: "streaming",
	contracts.Standard: "ordinary traffic", contracts.HighThroughputData: "a download",
	contracts.LowPriorityData: "a background transfer",
}

var units = map[string]string{
	"pkts_up_per_s": "Packets per second sent", "pkts_down_per_s": "Packets per second received",
	"bytes_up_per_s": "Bytes per second sent", "bytes_down_per_s": "Bytes per second received",
	"mean_pkt_size_up": "Mean size of packets sent", "mean_pkt_size_down": "Mean size of packets received",
	"pkt_size_spread": "Spread of packet sizes", "rate_cv": "Variation of the packet rate",
	"burstiness": "Burstiness",
}

// reason is the sentence a person reads in the action log.
func reason(class contracts.Class, e Excursion, seconds int) string {
	name := names[class]
	if name == "" {
		name = strings.ReplaceAll(string(class), "_", " ")
	}
	return fmt.Sprintf("%s was %s for %d s, outside the %s to %s normal for %s",
		units[e.Feature], number(e.Value), seconds, number(e.Low), number(e.High), name)
}

// number prints large values whole and small ones with two significant
// decimals, as a person would write them.
func number(v float64) string {
	if math.Abs(v) >= 100 {
		return strconv.FormatFloat(math.Round(v), 'f', 0, 64)
	}
	return strconv.FormatFloat(v, 'g', 3, 64)
}
