package verify

import "testing"

// series builds one sample a second: the tin delay is before until the
// change at 10 s, then first for 5 s, then second.
func series(before, first, second float64, probes bool) []Sample {
	var out []Sample
	for t := 0.5; t < 20; t++ {
		v := before
		if t >= 15 {
			v = second
		} else if t >= 10 {
			v = first
		}
		out = append(out, Sample{TS: t, TinMs: v, HasTin: true, ProbeMs: 1, HasProbe: probes})
	}
	return out
}

func TestVerdicts(t *testing.T) {
	cfg := DefaultConfig()
	cases := []struct {
		name                  string
		before, first, second float64
		want                  Verdict
	}{
		{"worse in both windows", 5, 40, 35, Regressed},
		{"worse in the first only", 5, 40, 6, Neutral},
		{"worse in the second only", 5, 6, 40, Neutral},
		{"better in both", 60, 8, 9, Improved},
		{"better in one", 60, 8, 58, Neutral},
		{"inside the band", 5, 8, 9, Neutral},
		{"more than the band but not half as much again", 40, 48, 50, Neutral},
	}
	for _, c := range cases {
		if got := Judge(cfg, series(c.before, c.first, c.second, true), 10); got.Verdict != c.want {
			t.Errorf("%s: %s (%s), want %s", c.name, got.Verdict, got.Reason, c.want)
		}
	}
}

func TestTooFewSamplesIsNeutral(t *testing.T) {
	cfg := DefaultConfig()
	if got := Judge(cfg, nil, 10); got.Verdict != Neutral {
		t.Fatalf("no samples: %s", got.Verdict)
	}
	// A tin that carried nothing before the change has no "before".
	samples := series(0, 80, 80, false)
	for i := range samples {
		if samples[i].TS < 10 {
			samples[i].HasTin = false
		}
	}
	got := Judge(cfg, samples, 10)
	if got.Verdict != Neutral || got.TinMs[0] != -1 {
		t.Fatalf("no before: %s, before %.1f", got.Verdict, got.TinMs[0])
	}
}

func TestEitherFigureGettingWorseIsARegression(t *testing.T) {
	cfg := DefaultConfig()
	samples := series(5, 5, 5, true)
	for i := range samples {
		if samples[i].TS >= 10 {
			samples[i].ProbeMs = 90
		}
	}
	if got := Judge(cfg, samples, 10); got.Verdict != Regressed {
		t.Fatalf("probe delay rose: %s (%s)", got.Verdict, got.Reason)
	}
	// One figure better and the other worse is still worse.
	samples = series(60, 5, 5, true)
	for i := range samples {
		if samples[i].TS >= 10 {
			samples[i].ProbeMs = 90
		}
	}
	if got := Judge(cfg, samples, 10); got.Verdict != Regressed {
		t.Fatalf("tin better, probes worse: %s", got.Verdict)
	}
}
