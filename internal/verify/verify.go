// Package verify judges whether a change the controller made left things
// better or worse. It sees only what a real router can see: the delay in
// CAKE's Voice and Video tins and the delay of the capacity estimator's
// probes. The generators' true quality figures are used afterwards, to
// score how often these verdicts were right, never here.
package verify

import "fmt"

// Sample is one second of what the router observed.
type Sample struct {
	TS float64 // seconds on the run's clock
	// TinMs is the delay in the Voice and Video tins, the worse of the two
	// and of the two directions; HasTin is false in a second when neither
	// tin carried a packet, and CAKE's figure is then stale.
	TinMs  float64
	HasTin bool
	// ProbeMs is the probes' delay above their baseline; HasProbe is false
	// without an estimator or with too few reflectors answering.
	ProbeMs  float64
	HasProbe bool
}

// Config holds the settings. Starting values: the noise band is to come
// from A/A runs, the spread of the same figure between identical runs.
type Config struct {
	BeforeS float64 // how far back the "before" reaches (10 s)
	WindowS float64 // each of the two "after" windows (5 s)
	// MinSamples a window needs of a figure before that figure is judged.
	MinSamples int
	// A figure counts as changed only when it moved by more than BandMs
	// and by more than BandRel of what it was before.
	BandMs  float64
	BandRel float64
}

func DefaultConfig() Config {
	return Config{BeforeS: 10, WindowS: 5, MinSamples: 3, BandMs: 5, BandRel: 0.5}
}

type Verdict string

const (
	Improved  Verdict = "improved"
	Neutral   Verdict = "neutral"
	Regressed Verdict = "regressed"
)

// Result is a verdict with the figures behind it. A mean of -1 means the
// window had too few samples of that figure.
type Result struct {
	Verdict Verdict    `json:"verdict"`
	Reason  string     `json:"reason"`
	TinMs   [3]float64 `json:"tin_delay_ms"`   // before, first window, second window
	ProbeMs [3]float64 `json:"probe_delay_ms"` // the same
}

func mean(samples []Sample, low, high float64, minimum int, pick func(Sample) (float64, bool)) float64 {
	sum, n := 0.0, 0
	for _, s := range samples {
		if s.TS < low || s.TS >= high {
			continue
		}
		if v, ok := pick(s); ok {
			sum += v
			n++
		}
	}
	if n < minimum || n == 0 {
		return -1
	}
	return sum / float64(n)
}

// Judge compares the BeforeS seconds before a change made at time at with
// the two windows after it. Worse in both windows is regressed; better in
// both, and nothing worse, is improved; anything else is neutral,
// including too little to go on.
func Judge(cfg Config, samples []Sample, at float64) Result {
	figures := []struct {
		name string
		pick func(Sample) (float64, bool)
		out  *[3]float64
	}{
		{"delay in the Voice and Video tins", func(s Sample) (float64, bool) { return s.TinMs, s.HasTin }, nil},
		{"probe delay", func(s Sample) (float64, bool) { return s.ProbeMs, s.HasProbe }, nil},
	}
	var res Result
	figures[0].out, figures[1].out = &res.TinMs, &res.ProbeMs
	edges := [4]float64{at - cfg.BeforeS, at, at + cfg.WindowS, at + 2*cfg.WindowS}
	worse, better, judged := "", "", 0
	for _, f := range figures {
		for i := 0; i < 3; i++ {
			f.out[i] = mean(samples, edges[i], edges[i+1], cfg.MinSamples, f.pick)
		}
		before, first, second := f.out[0], f.out[1], f.out[2]
		if before < 0 || first < 0 || second < 0 {
			continue
		}
		judged++
		band := max(cfg.BandMs, cfg.BandRel*before)
		switch {
		case first-before > band && second-before > band:
			worse = fmt.Sprintf("%s rose from %.1f ms to %.1f and %.1f ms", f.name, before, first, second)
		case before-first > band && before-second > band:
			better = fmt.Sprintf("%s fell from %.1f ms to %.1f and %.1f ms", f.name, before, first, second)
		}
	}
	switch {
	case judged == 0:
		res.Verdict, res.Reason = Neutral, "too few samples to judge"
	case worse != "":
		res.Verdict, res.Reason = Regressed, worse
	case better != "":
		res.Verdict, res.Reason = Improved, better
	default:
		res.Verdict, res.Reason = Neutral, "no change beyond the noise band"
	}
	return res
}
