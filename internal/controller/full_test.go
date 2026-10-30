package controller

import (
	"encoding/json"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"adaptiveqos/contracts"
	"adaptiveqos/internal/cake"
	"adaptiveqos/internal/reclass"
	"adaptiveqos/internal/verify"
)

// envelopeCases reads the ML side's envelopes and two of its windows: one
// inside a video call's normal range and one with two statistics outside.
func envelopeCases(t *testing.T) (env reclass.Envelopes, inside, outside contracts.Window) {
	t.Helper()
	raw, err := os.ReadFile("../reclass/testdata/cases.json")
	if err != nil {
		t.Fatal(err)
	}
	var file struct {
		Envelopes reclass.Envelopes
		Cases     []struct {
			Name    string
			Windows []contracts.Window
		}
	}
	if err := json.Unmarshal(raw, &file); err != nil {
		t.Fatal(err)
	}
	for _, c := range file.Cases {
		switch {
		case strings.HasPrefix(c.Name, "a flow inside its range"):
			inside = c.Windows[0]
		case strings.HasPrefix(c.Name, "two outside for two windows"):
			outside = c.Windows[1]
		}
	}
	return file.Envelopes, inside, outside
}

func (s *fakeService) asked() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.requests)
}

func TestALabelledFlowIsAskedAboutOnlyWhenItLeavesItsRange(t *testing.T) {
	env, inside, outside := envelopeCases(t)
	settings := reclass.DefaultConfig()
	settings.RecheckMax = 0 // no routine recheck in this test
	r := newRig(t, Config{Reclass: reclass.NewWatcher(settings, env, 1)})
	f, rec := flow(0)
	r.c.OnRecord(f, rec)
	eventually(t, "labelled", func() bool { return r.c.Counts().Labelled == 1 })

	// From now on the classifier calls it a voice call.
	video := contracts.TinVideo
	r.service.set(func(s *fakeService) {
		s.answer = func(id uint64, rec *contracts.FeatureRecord) *contracts.ServiceResponse {
			resp := proposalFor(id, rec.FlowID, contracts.TinVoice, "EF", &video)
			top := contracts.Telephony
			resp.Proposal.FlowLabel.TopClass = &top
			return resp
		}
	})
	record := func(w contracts.Window) *contracts.FeatureRecord {
		out := window(rec)
		out.Window = &w
		return out
	}
	for i := 0; i < 8; i++ {
		r.c.OnWindow(f, record(inside))
	}
	time.Sleep(50 * time.Millisecond)
	if n := r.service.asked(); n != 1 {
		t.Fatalf("%d questions about a flow that stayed inside its range, want the first one only", n)
	}

	// Two windows outside: one question, and its answer moves the flow at
	// once. The waiting was done by the check.
	r.c.OnWindow(f, record(outside))
	r.c.OnWindow(f, record(outside))
	eventually(t, "moved to Voice", func() bool { tin, _ := r.labels.get(tuple(f)); return tin == contracts.TinVoice })
	if n := r.service.asked(); n != 2 {
		t.Fatalf("%d questions, want 2", n)
	}
	lines := strings.Split(strings.TrimSpace(r.actions.String()), "\n")
	var action Action
	json.Unmarshal([]byte(lines[len(lines)-1]), &action)
	if action.Proposal.Source != "reclassifier" || !strings.Contains(action.Proposal.Reason, "normal for a video call") ||
		!strings.Contains(action.Proposal.Reason, "Looks like a video call.") {
		t.Fatalf("proposal: source %q, reason %q", action.Proposal.Source, action.Proposal.Reason)
	}
	if n := r.c.Counts(); n.Triggers != 1 || n.Relabelled != 1 {
		t.Fatalf("counts: %+v", n)
	}
}

func TestAFlowWithoutALabelIsAskedAboutEveryFewWindows(t *testing.T) {
	env, inside, _ := envelopeCases(t)
	r := newRig(t, Config{Reclass: reclass.NewWatcher(reclass.DefaultConfig(), env, 1), AskEvery: 3})
	r.service.set(func(s *fakeService) {
		s.answer = func(uint64, *contracts.FeatureRecord) *contracts.ServiceResponse { return nil }
	})
	f, rec := flow(0)
	r.c.OnRecord(f, rec)
	for i := 0; i < 9; i++ {
		w := window(rec)
		w.Window = &inside
		r.c.OnWindow(f, w)
	}
	eventually(t, "asked", func() bool { return r.service.asked() == 4 }) // the first packets, then windows 3, 6 and 9
}

// fakeCake is CAKE's statistics for one device: the Voice and Video tins
// keep sending, with the delay the test sets.
type fakeCake struct {
	delayUs atomic.Int64
	mu      sync.Mutex
	sent    float64
}

func (c *fakeCake) read(string) (*cake.Stats, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.sent += 100
	tins := make([]cake.TinStats, len(cake.Tins))
	for i := range tins {
		tins[i] = cake.TinStats{AvgDelayUs: float64(c.delayUs.Load()), SentPackets: c.sent}
	}
	return &cake.Stats{RateKbit: 95000, Tins: tins}, nil
}

func quick() *verify.Config {
	return &verify.Config{BeforeS: 3, WindowS: 1, MinSamples: 1, BandMs: 5, BandRel: 0.5}
}

func TestAPromotionThatMadeThingsWorseIsUndone(t *testing.T) {
	tins := &fakeCake{}
	tins.delayUs.Store(2000)
	verdicts := &syncBuffer{}
	r := newRig(t, Config{Verify: quick(), Verdicts: verdicts, Rollback: true,
		Devices: map[string]string{"down": "ifb0"}, ReadCake: tins.read})
	time.Sleep(2200 * time.Millisecond) // a "before" to compare with

	f, rec := flow(0)
	r.c.OnRecord(f, rec)
	eventually(t, "promoted", func() bool { tin, _ := r.labels.get(tuple(f)); return tin == contracts.TinVideo })
	tins.delayUs.Store(60000)
	eventually(t, "undone", func() bool { tin, _ := r.labels.get(tuple(f)); return tin == contracts.TinBestEffort })

	var line VerdictLine
	if err := json.Unmarshal([]byte(strings.TrimSpace(verdicts.String())), &line); err != nil {
		t.Fatalf("verdict log %q: %v", verdicts.String(), err)
	}
	if line.Verdict != verify.Regressed || !line.RolledBack || line.Kind != watchPromotion || line.To != "video" {
		t.Fatalf("verdict: %+v", line)
	}
	// The classifier still calls it a video call. It is not promoted again
	// straight away.
	best := contracts.TinBestEffort
	r.service.set(func(s *fakeService) {
		s.answer = func(id uint64, rec *contracts.FeatureRecord) *contracts.ServiceResponse {
			return proposalFor(id, rec.FlowID, contracts.TinVideo, "AF41", &best)
		}
	})
	r.c.OnWindow(f, window(rec))
	r.c.OnWindow(f, window(rec))
	eventually(t, "held back", func() bool { return r.c.Counts().HeldBack == 2 })
	if tin, _ := r.labels.get(tuple(f)); tin != contracts.TinBestEffort {
		t.Fatalf("promoted again at once: %q", tin)
	}
	if n := r.c.Counts(); n.Watched != 1 || n.Regressed != 1 || n.RolledBack != 1 {
		t.Fatalf("counts: %+v", n)
	}
}

func TestAPromotionThatHarmedNothingStays(t *testing.T) {
	tins := &fakeCake{}
	tins.delayUs.Store(2000)
	verdicts := &syncBuffer{}
	r := newRig(t, Config{Verify: quick(), Verdicts: verdicts, Rollback: true,
		Devices: map[string]string{"down": "ifb0"}, ReadCake: tins.read})
	time.Sleep(2200 * time.Millisecond)
	f, rec := flow(0)
	r.c.OnRecord(f, rec)
	eventually(t, "judged", func() bool { return r.c.Counts().Watched == 1 })
	if tin, _ := r.labels.get(tuple(f)); tin != contracts.TinVideo {
		t.Fatalf("moved to %q", tin)
	}
	if !strings.Contains(verdicts.String(), `"verdict":"neutral"`) || r.c.Counts().RolledBack != 0 {
		t.Fatalf("verdict log: %s", verdicts.String())
	}
}

func TestWithRollbackOffTheVerdictIsOnlyLogged(t *testing.T) {
	tins := &fakeCake{}
	tins.delayUs.Store(2000)
	verdicts := &syncBuffer{}
	r := newRig(t, Config{Verify: quick(), Verdicts: verdicts,
		Devices: map[string]string{"down": "ifb0"}, ReadCake: tins.read})
	time.Sleep(2200 * time.Millisecond)
	f, rec := flow(0)
	r.c.OnRecord(f, rec)
	eventually(t, "promoted", func() bool { tin, _ := r.labels.get(tuple(f)); return tin == contracts.TinVideo })
	tins.delayUs.Store(60000)
	eventually(t, "judged", func() bool { return r.c.Counts().Regressed == 1 })
	if tin, _ := r.labels.get(tuple(f)); tin != contracts.TinVideo || r.c.Counts().RolledBack != 0 {
		t.Fatalf("undone although rollback is off: %q", tin)
	}
}

func TestARateRaiseThatMadeThingsWorseIsUndone(t *testing.T) {
	tins := &fakeCake{}
	tins.delayUs.Store(2000)
	shaper := &fakeShaper{rates: map[string]int{"down": 80000}}
	r := newRig(t, Config{Verify: quick(), Verdicts: &syncBuffer{}, Rollback: true, Shaper: shaper,
		Devices: map[string]string{"down": "ifb0"}, ReadCake: tins.read})
	r.c.shield.RateRead("down", 80000)
	time.Sleep(2200 * time.Millisecond)
	was := 80000
	action := r.c.ProposeRate(&contracts.PolicyProposal{SchemaVersion: 1, Source: "capacity_estimator", Kind: "shaper_rate",
		Reason: "The link has room.", ShaperRate: &contracts.ShaperRate{Direction: "down", RateKbit: 88000, PreviousRateKbit: &was}})
	if action.Outcome != "applied" {
		t.Fatalf("raise: %+v", action)
	}
	tins.delayUs.Store(60000)
	eventually(t, "rate restored", func() bool { return shaper.rate("down") == 80000 })
	if n := r.c.Counts(); n.RolledBack != 1 {
		t.Fatalf("counts: %+v", n)
	}
}
