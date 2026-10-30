package capacity

import (
	"strings"
	"testing"
	"time"

	"adaptiveqos/contracts"
)

const ms = int64(time.Millisecond)

// link is a crude model of the path, enough to drive the estimator: a
// bottleneck with a FIFO beyond the router, a shaper in the router, and
// reflectors whose delay is the path's plus the FIFO's.
type link struct {
	capacityKbit float64 // true download capacity
	shaperKbit   int     // what the router shapes the download to
	offeredKbit  float64 // what the hosts would take if they could
	queueKbit    float64 // bits waiting in the FIFO beyond the router, in kbit
	pathMs       float64
	silent       map[int]bool
	// Kept between calls of run, so that it can be called a tick at a time.
	tick      int
	passed    float64
	lastRaise int64
	silentMs  [3]float64
}

// probe gives the estimator what a prober would report for one reflector
// after one 100 ms interval: an answer if the round trip fits inside the
// interval, and otherwise only that a probe has been out this long.
func (l *link) probe(e *Estimator, r int) {
	if l.silent[r] {
		l.silentMs[r] += 100
		e.Probe(r, 0, false, 100, l.silentMs[r])
		return
	}
	l.silentMs[r] = 0
	rtt := l.rtt()
	e.Probe(r, rtt, true, max(rtt, 100)-100, 0)
}

// step advances the model by one probe interval and returns what got
// through, in kbit/s.
func (l *link) step(dt float64) float64 {
	// The download's bottleneck is before the router. The far end sends
	// what our shaper lets through and the link passes what it can.
	sending := min(l.offeredKbit, float64(l.shaperKbit))
	passed := min(sending+l.queueKbit/dt, l.capacityKbit)
	l.queueKbit = max(0, l.queueKbit+(sending-passed)*dt)
	l.queueKbit = min(l.queueKbit, l.capacityKbit*0.5) // a 500 ms buffer
	return min(passed, float64(l.shaperKbit))
}

func (l *link) rtt() float64 { return 2*l.pathMs + l.queueKbit/l.capacityKbit*1000 }

// run drives the estimator for some seconds, applying its proposals as a
// shield would (at most half down per step, one raise per 10 s), and
// returns every proposal made.
func run(t *testing.T, e *Estimator, l *link, nowNs *int64, seconds float64) []contracts.PolicyProposal {
	t.Helper()
	var all []contracts.PolicyProposal
	for i := 0; i < int(seconds*10+0.5); i++ {
		*nowNs += 100 * ms
		l.tick++
		got := l.step(0.1)
		for r := 0; r < 3; r++ {
			l.probe(e, r)
		}
		if l.tick%2 == 0 {
			// Five times a second, as CAKE's counters are read.
			e.Load("down", l.shaperKbit, (l.passed+got)/2, *nowNs)
			e.Load("up", 19_000, 500, *nowNs)
		}
		l.passed = got
		for _, p := range e.Decide(*nowNs) {
			all = append(all, p)
			if p.Source != "capacity_estimator" || p.Kind != "shaper_rate" || p.Reason == "" ||
				*p.ShaperRate.PreviousRateKbit < 1 || p.ShaperRate.RateKbit < 1 {
				t.Fatalf("malformed proposal: %+v %+v", p, p.ShaperRate)
			}
			if p.ShaperRate.Direction != "down" {
				continue
			}
			want := p.ShaperRate.RateKbit
			switch {
			case want < l.shaperKbit:
				l.shaperKbit = max(want, l.shaperKbit/2)
			case *nowNs-l.lastRaise >= 10_000*ms:
				l.shaperKbit = min(want, l.shaperKbit*11/10)
				l.lastRaise = *nowNs
			}
			e.Applied("down", l.shaperKbit)
		}
	}
	return all
}

func newRun() (*Estimator, *link, *int64) {
	e := New(DefaultConfig(19_000, 95_000, 500, 1_000))
	l := &link{capacityKbit: 100_000, shaperKbit: 95_000, offeredKbit: 1e9, pathMs: 10, silent: map[int]bool{}}
	now := int64(1e12)
	return e, l, &now
}

func TestQuietLinkIsLeftAlone(t *testing.T) {
	e, l, now := newRun()
	if got := run(t, e, l, now, 30); len(got) != 0 {
		t.Fatalf("%d proposals on a link that is fine: %+v", len(got), got[0])
	}
	if l.shaperKbit != 95_000 {
		t.Fatalf("shaper moved to %d", l.shaperKbit)
	}
}

func TestCapacityDropIsFollowedAndNotChasedDown(t *testing.T) {
	e, l, now := newRun()
	run(t, e, l, now, 5)
	l.capacityKbit = 20_000 // S2: the link falls to a fifth
	started := *now
	var settled int64
	for i := 0; i < 50 && settled == 0; i++ {
		run(t, e, l, now, 0.1)
		if l.shaperKbit <= 20_000 {
			settled = *now
		}
	}
	if settled == 0 {
		t.Fatalf("shaper still at %d kbit 5 s after the drop", l.shaperKbit)
	}
	if took := time.Duration(settled - started); took > 2*time.Second {
		t.Fatalf("took %v to get under the link", took)
	}
	// The link's own buffer takes seconds to drain, and the delay stays
	// high all that time. The rate must not be chased down meanwhile.
	run(t, e, l, now, 8)
	if l.shaperKbit < 15_000 || l.shaperKbit > 20_000 {
		t.Fatalf("shaper at %d kbit on a 20,000 kbit link", l.shaperKbit)
	}
	if l.queueKbit > 100 {
		t.Fatalf("the queue beyond the router did not drain: %.0f kbit", l.queueKbit)
	}
}

func TestRateClimbsBackWhenTheLinkRecovers(t *testing.T) {
	e, l, now := newRun()
	l.capacityKbit = 20_000
	run(t, e, l, now, 15)
	low := l.shaperKbit
	l.capacityKbit = 100_000
	proposals := run(t, e, l, now, 120)
	if l.shaperKbit <= low*2 {
		t.Fatalf("shaper only went from %d to %d in two minutes", low, l.shaperKbit)
	}
	if l.shaperKbit > 95_000 {
		t.Fatalf("shaper %d is above the configured rate", l.shaperKbit)
	}
	raises := 0
	for _, p := range proposals {
		if p.ShaperRate.RateKbit > *p.ShaperRate.PreviousRateKbit {
			raises++
		}
	}
	if raises > 13 { // one per 10 s
		t.Fatalf("%d raises asked for in 120 s", raises)
	}
}

func TestProbingAboveTheLinkIsCutBackAgain(t *testing.T) {
	e, l, now := newRun()
	l.capacityKbit = 20_000
	run(t, e, l, now, 90) // long enough for several raises to overshoot and be cut
	if l.shaperKbit > 22_500 || l.shaperKbit < 14_000 {
		t.Fatalf("shaper at %d kbit on a 20,000 kbit link after 90 s", l.shaperKbit)
	}
}

func TestSilentReflectorsLeaveTheRatesAlone(t *testing.T) {
	e, l, now := newRun()
	l.capacityKbit = 20_000
	run(t, e, l, now, 20) // the estimator has followed the link down
	held := l.shaperKbit
	if held > 22_500 {
		t.Fatalf("shaper at %d kbit on a 20,000 kbit link", held)
	}
	l.silent[0], l.silent[1] = true, true
	if got := run(t, e, l, now, 30); !e.Deaf() || len(got) != 0 || l.shaperKbit != held {
		t.Fatalf("deaf=%v, %d proposals, shaper %d: want the rate left at %d", e.Deaf(), len(got), l.shaperKbit, held)
	}
	// Hearing again, it goes back to following the link.
	l.silent = map[int]bool{}
	l.capacityKbit = 95_000
	run(t, e, l, now, 60)
	if e.Deaf() || l.shaperKbit <= held {
		t.Fatalf("deaf=%v shaper=%d after the reflectors came back", e.Deaf(), l.shaperKbit)
	}
}

// The first design: with DeafHold off, silence sends both rates to the
// minimum.
func TestSilentReflectorsMeanTheMinimumWhenAskedFor(t *testing.T) {
	e, l, now := newRun()
	e.cfg.DeafHold = false
	run(t, e, l, now, 3)
	l.silent[0] = true // one silent: two still answer, nothing changes
	if got := run(t, e, l, now, 3); len(got) != 0 || e.Deaf() {
		t.Fatalf("one silent reflector caused %d proposals", len(got))
	}
	l.silent[1] = true
	proposals := run(t, e, l, now, 5)
	if !e.Deaf() || l.shaperKbit != 1_000 {
		t.Fatalf("deaf=%v shaper=%d, want the minimum", e.Deaf(), l.shaperKbit)
	}
	up := false
	for _, p := range proposals {
		if !strings.Contains(p.Reason, "minimum") {
			t.Fatalf("reason: %q", p.Reason)
		}
		up = up || (p.ShaperRate.Direction == "up" && p.ShaperRate.RateKbit == 500)
	}
	if !up {
		t.Fatal("the upload was not sent to its minimum")
	}
	// It stays there while they are silent, and returns when they answer.
	run(t, e, l, now, 30)
	if l.shaperKbit != 1_000 {
		t.Fatalf("left the minimum while deaf: %d", l.shaperKbit)
	}
	l.silent = map[int]bool{}
	run(t, e, l, now, 60)
	if l.shaperKbit <= 1_000 {
		t.Fatal("never left the minimum after the reflectors came back")
	}
}

func TestDelayWithNothingBusyIsNotAnsweredWithACut(t *testing.T) {
	e, l, now := newRun()
	l.offeredKbit = 300 // nearly idle
	run(t, e, l, now, 3)
	l.pathMs = 60 // the path itself got slower: no rate would cure that
	if got := run(t, e, l, now, 5); len(got) != 0 {
		t.Fatalf("%d proposals for delay that is not a queue: %s", len(got), got[0].Reason)
	}
}

func TestBusyUploadIsCutBlind(t *testing.T) {
	e := New(DefaultConfig(19_000, 95_000, 500, 1_000))
	now := int64(1e12)
	e.Load("up", 19_000, 18_500, now)
	e.Load("down", 95_000, 400, now)
	for i := 0; i < 20; i++ {
		for r := 0; r < 3; r++ {
			e.Probe(r, 20, true, 0, 0)
		}
	}
	var cuts []contracts.PolicyProposal
	for i := 0; i < 15; i++ { // 1.5 s of raised delay
		now += 100 * ms
		for r := 0; r < 3; r++ {
			e.Probe(r, 120, true, 20, 0)
		}
		e.Load("up", 19_000, 18_500, now)
		cuts = append(cuts, e.Decide(now)...)
	}
	if len(cuts) != 2 { // at once, and again after the hold
		t.Fatalf("%d cuts in 1.5 s, want 2", len(cuts))
	}
	if c := cuts[0].ShaperRate; c.Direction != "up" || c.RateKbit != 14_250 {
		t.Fatalf("first cut: %+v", c)
	}
}

func TestOddInputsAreIgnored(t *testing.T) {
	e := New(DefaultConfig(19_000, 95_000, 500, 1_000))
	e.Probe(-1, 10, true, 0, 0)
	e.Probe(7, 10, true, 0, 0)
	e.Probe(0, -5, true, 0, 0)
	e.Load("sideways", 1_000, 10, 1e12)
	e.Load("down", 0, 10, 1e12)
	if got := e.Decide(1e12); len(got) != 0 {
		t.Fatalf("proposals from nothing: %+v", got)
	}
}

// What happened in the testbed the first time: the link's buffer filled,
// answers took longer than the probe interval, and the estimator took late
// for lost and sent both rates to the minimum.
func TestLateAnswersAreDelayNotSilence(t *testing.T) {
	e := New(DefaultConfig(19_000, 95_000, 500, 1_000))
	now := int64(1e12)
	e.Load("down", 95_000, 20_000, now)
	e.Load("up", 19_000, 500, now)
	for i := 0; i < 20; i++ {
		for r := 0; r < 3; r++ {
			e.Probe(r, 20, true, 0, 0)
		}
	}
	// Half a second of queue: for five intervals nothing comes back at
	// all, and what does come back afterwards is 500 ms old.
	var proposals []contracts.PolicyProposal
	for i := 1; i <= 12; i++ {
		now += 100 * ms
		for r := 0; r < 3; r++ {
			if i <= 5 {
				e.Probe(r, 0, false, float64(i*100), float64(i*100))
			} else {
				e.Probe(r, 500, true, 400, 0)
			}
		}
		e.Load("down", 95_000, 20_000, now)
		e.Load("up", 19_000, 500, now)
		proposals = append(proposals, e.Decide(now)...)
	}
	if e.Deaf() {
		t.Fatal("late answers were taken for silence")
	}
	if len(proposals) == 0 {
		t.Fatal("half a second of delay caused no cut")
	}
	first := proposals[0]
	if first.ShaperRate.Direction != "down" || first.ShaperRate.RateKbit != 18_000 {
		t.Fatalf("first proposal: %+v (%s)", first.ShaperRate, first.Reason)
	}
	for _, p := range proposals {
		if p.ShaperRate.Direction == "up" {
			t.Fatalf("the idle upload was cut: %s", p.Reason)
		}
	}
	// The cut came while the first probes were still out: delay is seen
	// before any answer returns.
	if first.TsNs > int64(1e12)+400*ms {
		t.Fatalf("first cut %d ms after the queue began", (first.TsNs-int64(1e12))/ms)
	}
}

// What happened in the testbed the second time: the shield allows a rate
// to halve at most, and the last half-step left it at 20,532 kbit/s on a
// 20,000 kbit/s link. Nearly everything the shaper allowed was getting
// through, the estimator took that for "the shaper is the limit", and the
// queue beyond the router stayed full for the rest of the run.
func TestRateLeftJustAboveTheLinkIsCutAgain(t *testing.T) {
	e, l, now := newRun()
	run(t, e, l, now, 5)
	l.capacityKbit = 20_000
	l.shaperKbit = 20_532
	e.Applied("down", 20_532)
	l.queueKbit = 10_000 // the buffer is already full
	run(t, e, l, now, 4)
	if l.shaperKbit >= 20_000 {
		t.Fatalf("shaper stuck at %d kbit, above the 20,000 kbit link", l.shaperKbit)
	}
	run(t, e, l, now, 10)
	if l.shaperKbit < 14_000 {
		t.Fatalf("shaper chased down to %d kbit while the buffer drained", l.shaperKbit)
	}
	if l.queueKbit > 100 {
		t.Fatalf("the queue did not drain: %.0f kbit", l.queueKbit)
	}
}

// What happened in the testbed the third time, in S7: a game was using
// 17 Mbit/s of a 100 Mbit/s link when eight connections of a download
// started at once. Their first burst queued at the link for a moment. The
// estimator, reading CAKE once a second, sized its cut by the second
// before the burst and asked for 16 Mbit/s on a link that was carrying 95.
func TestBurstOfNewConnectionsIsNotTakenForADrop(t *testing.T) {
	e := New(DefaultConfig(19_000, 95_000, 500, 1_000))
	now := int64(1e12)
	feed := func(kbit float64, rtt float64, rounds int) (out []contracts.PolicyProposal) {
		for i := 0; i < rounds; i++ {
			now += 100 * ms
			for r := 0; r < 3; r++ {
				e.Probe(r, rtt, true, max(rtt, 100)-100, 0)
			}
			if i%2 == 1 {
				e.Load("down", 95_000, kbit, now)
				e.Load("up", 19_000, 800, now)
			}
			out = append(out, e.Decide(now)...)
		}
		return out
	}
	if got := feed(17_000, 20, 30); len(got) != 0 {
		t.Fatalf("proposals while only the game runs: %+v", got[0].ShaperRate)
	}
	// The burst: the link is full and delay jumps for half a second.
	cuts := feed(95_000, 120, 5)
	for _, p := range cuts {
		if p.ShaperRate.RateKbit < 80_000 {
			t.Fatalf("cut to %d kbit on a link carrying 95,000: %s", p.ShaperRate.RateKbit, p.Reason)
		}
	}
	// It passes, and nothing more is asked for.
	if got := feed(93_000, 21, 20); len(got) != 0 && got[0].ShaperRate.RateKbit < 80_000 {
		t.Fatalf("after the burst: %+v", got[0].ShaperRate)
	}
}

// What happened in S10 the first time: two of three reflectors were made
// unreachable. Their probes went unanswered, which for the three seconds
// before they counted as silent read as ever-growing delay, and the rate
// was cut eight times on a link that was fine, as the third reflector was
// saying all along.
func TestUnreachableReflectorsAreNotAQueue(t *testing.T) {
	e := New(DefaultConfig(19_000, 95_000, 1_900, 9_500))
	now := int64(1e12)
	e.Load("down", 95_000, 80_000, now)
	e.Load("up", 19_000, 2_000, now)
	for i := 0; i < 20; i++ {
		for r := 0; r < 3; r++ {
			e.Probe(r, 20, true, 0, 0)
		}
	}
	var before []contracts.PolicyProposal
	for i := 1; i <= 29; i++ { // up to just before the silence limit
		now += 100 * ms
		e.Probe(0, 0, false, float64(i*100), float64(i*100))
		e.Probe(1, 0, false, float64(i*100), float64(i*100))
		e.Probe(2, 20, true, 0, 0) // the one still reachable: no delay at all
		e.Load("down", 95_000, 80_000, now)
		before = append(before, e.Decide(now)...)
	}
	if len(before) != 0 {
		t.Fatalf("%d cuts while one reflector answered on time: %s", len(before), before[0].Reason)
	}
	// Past the limit they count as silent, and with one left the rate goes
	// to the minimum, when that is the setting.
	e.cfg.DeafHold = false
	var after []contracts.PolicyProposal
	for i := 30; i <= 34; i++ {
		now += 100 * ms
		e.Probe(0, 0, false, 3000, float64(i*100))
		e.Probe(1, 0, false, 3000, float64(i*100))
		e.Probe(2, 20, true, 0, 0)
		after = append(after, e.Decide(now)...)
	}
	if !e.Deaf() || len(after) == 0 || !strings.Contains(after[0].Reason, "minimum") {
		t.Fatalf("deaf=%v, %d proposals", e.Deaf(), len(after))
	}
}

// A real queue delays every probe, including while none has come back
// yet. One reflector answering late and two still waiting is congestion.
func TestAllProbesWaitingIsAQueue(t *testing.T) {
	e := New(DefaultConfig(19_000, 95_000, 1_900, 9_500))
	now := int64(1e12)
	e.Load("down", 95_000, 20_000, now)
	for i := 0; i < 20; i++ {
		for r := 0; r < 3; r++ {
			e.Probe(r, 20, true, 0, 0)
		}
	}
	var cuts []contracts.PolicyProposal
	for i := 1; i <= 5; i++ {
		now += 100 * ms
		for r := 0; r < 3; r++ {
			e.Probe(r, 0, false, float64(i*100), float64(i*100))
		}
		e.Load("down", 95_000, 20_000, now)
		cuts = append(cuts, e.Decide(now)...)
	}
	if len(cuts) == 0 {
		t.Fatal("every probe waiting for half a second caused no cut")
	}
}

// Both directions busy and the probes' delay high: round trips cannot say
// which way the queue is. The download's can be measured and cut properly,
// so it goes first; the upload is cut blind only if the delay outlasts
// that. In S5 the upload was cut eleven times for a queue that was on the
// way down, and the call on it broke.
func TestTheDownloadIsCutBeforeTheUpload(t *testing.T) {
	e := New(DefaultConfig(4_750, 23_750, 500, 1_000))
	now := int64(1e12)
	cuts := map[string]int{}
	tick := func(delayMs float64) {
		now += 100 * ms
		for r := 0; r < 3; r++ {
			e.Probe(r, 20+delayMs, true, 0, 0)
		}
		e.Load("down", e.dirs["down"].shaperKbit, 20_000, now)
		e.Load("up", e.dirs["up"].shaperKbit, 4_500, now)
		for _, p := range e.Decide(now) {
			cuts[p.ShaperRate.Direction]++
			e.Applied(p.ShaperRate.Direction, p.ShaperRate.RateKbit)
		}
	}
	for i := 0; i < 30; i++ {
		tick(0) // a baseline with no queue
	}
	e.Applied("down", 23_750)
	e.Applied("up", 4_750)
	for i := 0; i < 15; i++ {
		tick(60) // a queue, 1.5 s of it
	}
	if cuts["down"] == 0 || cuts["up"] != 0 {
		t.Fatalf("in the first 1.5 s: %d download cuts, %d upload cuts; want the download only", cuts["down"], cuts["up"])
	}
	for i := 0; i < 30; i++ {
		tick(60) // still there 3 s later: now the upload may be the cause
	}
	if cuts["up"] == 0 {
		t.Fatal("the upload was never cut although the delay outlasted the download's cut")
	}
}

// With the download quiet the upload is the only candidate and is cut at once.
func TestAnUploadAloneIsCutAtOnce(t *testing.T) {
	e := New(DefaultConfig(4_750, 23_750, 500, 1_000))
	now := int64(1e12)
	up := 0
	for i := 0; i < 45; i++ {
		now += 100 * ms
		delay := 0.0
		if i >= 30 {
			delay = 60
		}
		for r := 0; r < 3; r++ {
			e.Probe(r, 20+delay, true, 0, 0)
		}
		e.Load("down", 23_750, 100, now)
		e.Load("up", 4_750, 4_500, now)
		for _, p := range e.Decide(now) {
			if p.ShaperRate.Direction == "up" {
				up++
			}
		}
	}
	if up == 0 {
		t.Fatal("a busy upload with a quiet download was not cut")
	}
}
