package shield

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"adaptiveqos/contracts"
)

const (
	flowID = "3fa9c1d2e4b50761"
	hostID = "a1b2c3d4e5f60718"
	second = int64(time.Second)
)

var (
	testCfg = DefaultConfig(1_000, 100_000, 2_000, 500_000)
	tinList = []contracts.Tin{contracts.TinBulk, contracts.TinBestEffort, contracts.TinVideo, contracts.TinVoice}
	dscps   = []string{"EF", "CS4", "AF41", "AF31", "CS0", "AF11", "CS1"}
)

func ptr[T any](v T) *T { return &v }

func labelProposal(tin contracts.Tin, dscp string, previous *contracts.Tin) *contracts.PolicyProposal {
	return &contracts.PolicyProposal{
		SchemaVersion: 1, ProposalID: "0000000000000a17", TsNs: 1, Source: "decision_engine",
		Kind: "flow_label", Reason: "test",
		FlowLabel: &contracts.FlowLabel{
			FlowID: flowID, HostID: hostID, Tin: tin, DSCP: dscp, PreviousTin: previous,
			DecisionRule: "expected_damage",
		},
	}
}

func rateProposal(direction string, kbit int) *contracts.PolicyProposal {
	return &contracts.PolicyProposal{
		SchemaVersion: 1, ProposalID: "0000000000000a18", TsNs: 1, Source: "capacity_estimator",
		Kind: "shaper_rate", Reason: "test",
		ShaperRate: &contracts.ShaperRate{Direction: direction, RateKbit: kbit},
	}
}

func baseState() *State {
	return &State{
		NowNs: 100 * second,
		Flows: map[string]Flow{flowID: {HostID: hostID}},
		Rates: map[string]Rate{"up": {Kbit: 10_000}, "down": {Kbit: 50_000}},
	}
}

// violations is the property the fuzzers hold Check to: a verdict is a
// reject, or it is inside every bound. It is written from the
// specification's tables, separately from Check, and kept plain on
// purpose.
func violations(cfg Config, st *State, p *contracts.PolicyProposal, v contracts.PolicyVerdict) []string {
	var bad []string
	fail := func(format string, args ...any) { bad = append(bad, fmt.Sprintf(format, args...)) }
	if !isID(v.ProposalID) || v.TsNs < 0 || v.Reason == "" || v.SchemaVersion != 1 {
		fail("verdict is not well formed: %+v", v)
	}
	switch v.Verdict {
	case "reject":
		if v.Applied != nil {
			fail("a reject carries something to apply")
		}
		return bad
	case "accept", "clamp":
	default:
		fail("unknown verdict %q", v.Verdict)
		return bad
	}
	if v.Applied == nil || p == nil {
		fail("%s with nothing to apply or no proposal", v.Verdict)
		return bad
	}
	if p.ProposalID != v.ProposalID {
		fail("verdict answers a different proposal")
	}
	if p.SchemaVersion != 1 || !isID(p.ProposalID) || p.TsNs < 0 || !sources[p.Source] || p.Reason == "" {
		fail("a proposal with a bad envelope passed")
	}
	a := v.Applied
	switch p.Kind {
	case "flow_label":
		l := p.FlowLabel
		if l == nil || p.ShaperRate != nil {
			fail("flow_label without exactly a flow label passed")
			return bad
		}
		if a.RateKbit != 0 || a.Tin != l.Tin || a.DSCP != l.DSCP || v.Verdict != "accept" {
			fail("a flow label is applied as proposed or not at all, got %+v", a)
		}
		if !strings.Contains(" voice video best_effort bulk ", " "+string(a.Tin)+" ") {
			fail("unknown tin %q passed", a.Tin)
		}
		if taxonomy.DSCP[a.DSCP].Tin != a.Tin {
			fail("DSCP %s does not map to tin %s", a.DSCP, a.Tin)
		}
		if !isID(l.FlowID) || !isID(l.HostID) || !rules[l.DecisionRule] {
			fail("bad ids or decision rule passed")
		}
		if l.ExpectedDamage != nil && (math.IsNaN(*l.ExpectedDamage) || *l.ExpectedDamage < 0 || math.IsInf(*l.ExpectedDamage, 0)) {
			fail("bad expected damage passed")
		}
		flow, known := st.Flows[l.FlowID]
		if !known || flow.HostID != l.HostID {
			fail("label for an unknown flow passed")
			return bad
		}
		if flow.Pinned {
			fail("label for a pinned flow passed")
		}
		if a.Tin == flow.Tin {
			return bad
		}
		if st.Starvation && (a.Tin == contracts.TinVoice || a.Tin == contracts.TinVideo) {
			fail("promotion passed during starvation")
		}
		if flow.Tin != "" {
			flips := 0
			for _, ts := range flow.Changes {
				if ts <= st.NowNs && st.NowNs-ts < cfg.FlipWindowMs*1_000_000 {
					flips++
				}
			}
			if flips >= cfg.FlipLimit {
				fail("change number %d inside the flip window passed", flips+1)
			}
			remarks := 0
			for _, ts := range st.Remarks {
				if ts <= st.NowNs && st.NowNs-ts < second {
					remarks++
				}
			}
			if remarks >= cfg.RemarkBudget {
				fail("re-mark passed with %d already this second", remarks)
			}
		}
	case "shaper_rate":
		r := p.ShaperRate
		if r == nil || p.FlowLabel != nil {
			fail("shaper_rate without exactly a shaper rate passed")
			return bad
		}
		if a.Tin != "" || a.DSCP != "" {
			fail("a rate verdict carries a tin")
		}
		if r.Direction != "up" && r.Direction != "down" {
			fail("unknown direction passed")
			return bad
		}
		floor, ceiling := cfg.MinRateKbit[r.Direction], cfg.MaxRateKbit[r.Direction]
		current := st.Rates[r.Direction]
		if a.RateKbit < floor || a.RateKbit > ceiling {
			fail("rate %d outside %d to %d", a.RateKbit, floor, ceiling)
		}
		if current.Kbit < 1 || r.RateKbit < 1 {
			fail("rate passed without a known current rate or with a rate below 1")
			return bad
		}
		if (v.Verdict == "accept") != (a.RateKbit == r.RateKbit) {
			fail("%s but proposed %d and applied %d", v.Verdict, r.RateKbit, a.RateKbit)
		}
		if a.RateKbit > current.Kbit {
			step := float64(current.Kbit) * (1 + cfg.MaxIncrease)
			if float64(a.RateKbit) > step && a.RateKbit > floor {
				fail("increase from %d to %d is more than one step", current.Kbit, a.RateKbit)
			}
			if current.Increased && st.NowNs-current.LastIncreaseNs < cfg.IncreaseIntervalMs*1_000_000 {
				fail("increase passed %d ms after the last one", (st.NowNs-current.LastIncreaseNs)/1_000_000)
			}
		}
		if a.RateKbit < current.Kbit {
			step := float64(current.Kbit) * (1 - cfg.MaxDecrease)
			if float64(a.RateKbit) < step && a.RateKbit < ceiling {
				fail("decrease from %d to %d is more than one step", current.Kbit, a.RateKbit)
			}
		}
		if current.Kbit >= floor && current.Kbit <= ceiling {
			lo, hi := min(current.Kbit, r.RateKbit), max(current.Kbit, r.RateKbit)
			if a.RateKbit < lo || a.RateKbit > hi {
				fail("applied %d is not between current %d and proposed %d", a.RateKbit, current.Kbit, r.RateKbit)
			}
		}
	default:
		fail("unknown kind %q passed", p.Kind)
	}
	return bad
}

func check(t *testing.T, st *State, p *contracts.PolicyProposal) (contracts.PolicyVerdict, Code) {
	t.Helper()
	before := fmt.Sprintf("%+v", *st)
	v, code := Check(testCfg, st, p)
	if after := fmt.Sprintf("%+v", *st); after != before {
		t.Fatalf("Check changed the state:\n%s\n%s", before, after)
	}
	for _, problem := range violations(testCfg, st, p, v) {
		t.Errorf("%s", problem)
	}
	return v, code
}

func TestFlowLabel(t *testing.T) {
	voice, video := contracts.TinVoice, contracts.TinVideo
	cases := []struct {
		name    string
		prepare func(*State)
		p       *contracts.PolicyProposal
		verdict string
		code    Code
	}{
		{"first label", nil, labelProposal(video, "AF41", nil), "accept", OK},
		{"unknown flow", func(s *State) { delete(s.Flows, flowID) }, labelProposal(video, "AF41", nil), "reject", UnknownFlow},
		{"wrong host", func(s *State) { s.Flows[flowID] = Flow{HostID: "ffffffffffffffff"} }, labelProposal(video, "AF41", nil), "reject", UnknownFlow},
		{"pinned", func(s *State) { s.Flows[flowID] = Flow{HostID: hostID, Pinned: true} }, labelProposal(video, "AF41", nil), "reject", Pinned},
		{"stale: labelled meanwhile", func(s *State) { s.Flows[flowID] = Flow{HostID: hostID, Tin: voice} }, labelProposal(video, "AF41", nil), "reject", Stale},
		{"stale: other tin", func(s *State) { s.Flows[flowID] = Flow{HostID: hostID, Tin: voice} }, labelProposal(video, "AF41", &video), "reject", Stale},
		{"same tin", func(s *State) { s.Flows[flowID] = Flow{HostID: hostID, Tin: video} }, labelProposal(video, "AF41", &video), "accept", OK},
		{"second change", func(s *State) {
			s.Flows[flowID] = Flow{HostID: hostID, Tin: voice, Changes: []int64{s.NowNs - 5*second}}
		}, labelProposal(video, "AF41", &voice), "accept", OK},
		{"third change", func(s *State) {
			s.Flows[flowID] = Flow{HostID: hostID, Tin: voice, Changes: []int64{s.NowNs - 50*second, s.NowNs - 5*second}}
		}, labelProposal(video, "AF41", &voice), "reject", FlipCap},
		{"old changes do not count", func(s *State) {
			s.Flows[flowID] = Flow{HostID: hostID, Tin: voice, Changes: []int64{s.NowNs - 61*second, s.NowNs - 60*second}}
		}, labelProposal(video, "AF41", &voice), "accept", OK},
		{"re-mark budget spent", func(s *State) {
			s.Flows[flowID] = Flow{HostID: hostID, Tin: voice}
			for i := 0; i < testCfg.RemarkBudget; i++ {
				s.Remarks = append(s.Remarks, s.NowNs-int64(i)*1_000_000)
			}
		}, labelProposal(video, "AF41", &voice), "reject", RemarkBudget},
		{"budget does not hold back first labels", func(s *State) {
			for i := 0; i < testCfg.RemarkBudget; i++ {
				s.Remarks = append(s.Remarks, s.NowNs)
			}
		}, labelProposal(video, "AF41", nil), "accept", OK},
		{"promotion during starvation", func(s *State) { s.Starvation = true }, labelProposal(voice, "EF", nil), "reject", Starved},
		{"demotion during starvation", func(s *State) { s.Starvation = true }, labelProposal(contracts.TinBulk, "CS1", nil), "accept", OK},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			st := baseState()
			if c.prepare != nil {
				c.prepare(st)
			}
			v, code := check(t, st, c.p)
			if v.Verdict != c.verdict || code != c.code {
				t.Fatalf("got %s (%s): %s; want %s (%s)", v.Verdict, code, v.Reason, c.verdict, c.code)
			}
		})
	}
}

func TestShaperRate(t *testing.T) {
	cases := []struct {
		name    string
		prepare func(*State)
		p       *contracts.PolicyProposal
		verdict string
		code    Code
		applied int
	}{
		{"small increase", nil, rateProposal("down", 54_000), "accept", OK, 54_000},
		{"increase over one step", nil, rateProposal("down", 90_000), "clamp", RateStep, 55_000},
		{"decrease", nil, rateProposal("down", 30_000), "accept", OK, 30_000},
		{"decrease over one step", nil, rateProposal("down", 5_000), "clamp", RateStep, 25_000},
		{"below the minimum", func(s *State) { s.Rates["down"] = Rate{Kbit: 3_000} }, rateProposal("down", 1_600), "clamp", RateBounds, 2_000},
		{"above the maximum", func(s *State) { s.Rates["up"] = Rate{Kbit: 95_000} }, rateProposal("up", 104_000), "clamp", RateBounds, 100_000},
		{"at the maximum already", func(s *State) { s.Rates["up"] = Rate{Kbit: 100_000} }, rateProposal("up", 104_000), "reject", NoChange, 0},
		{"increase too soon", func(s *State) {
			s.Rates["down"] = Rate{Kbit: 50_000, LastIncreaseNs: s.NowNs - 9*second, Increased: true}
		}, rateProposal("down", 52_000), "reject", RateInterval, 0},
		{"increase after the interval", func(s *State) {
			s.Rates["down"] = Rate{Kbit: 50_000, LastIncreaseNs: s.NowNs - 10*second, Increased: true}
		}, rateProposal("down", 52_000), "accept", OK, 52_000},
		{"decrease is never delayed", func(s *State) {
			s.Rates["down"] = Rate{Kbit: 50_000, LastIncreaseNs: s.NowNs - 1, Increased: true}
		}, rateProposal("down", 40_000), "accept", OK, 40_000},
		{"current rate unknown", func(s *State) { delete(s.Rates, "down") }, rateProposal("down", 40_000), "reject", Stale, 0},
		{"proposer's view is stale", nil, func() *contracts.PolicyProposal {
			p := rateProposal("down", 40_000)
			p.ShaperRate.PreviousRateKbit = ptr(20_000)
			return p
		}(), "reject", Stale, 0},
		{"no change asked", nil, rateProposal("down", 50_000), "accept", OK, 50_000},
		{"back inside the bounds in one move", func(s *State) { s.Rates["down"] = Rate{Kbit: 100} }, rateProposal("down", 150), "clamp", RateBounds, 2_000},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			st := baseState()
			if c.prepare != nil {
				c.prepare(st)
			}
			v, code := check(t, st, c.p)
			if v.Verdict != c.verdict || code != c.code {
				t.Fatalf("got %s (%s): %s; want %s (%s)", v.Verdict, code, v.Reason, c.verdict, c.code)
			}
			if c.applied != 0 && v.Applied.RateKbit != c.applied {
				t.Fatalf("applied %d, want %d", v.Applied.RateKbit, c.applied)
			}
		})
	}
}

// invalidVariants returns the mutations that each turn a valid proposal
// into one the contract forbids.
func invalidVariants() map[string]func(*contracts.PolicyProposal) {
	label := func(fn func(*contracts.FlowLabel)) func(*contracts.PolicyProposal) {
		return func(p *contracts.PolicyProposal) {
			if p.FlowLabel == nil {
				p.SchemaVersion = 0
				return
			}
			fn(p.FlowLabel)
		}
	}
	rate := func(fn func(*contracts.ShaperRate)) func(*contracts.PolicyProposal) {
		return func(p *contracts.PolicyProposal) {
			if p.ShaperRate == nil {
				p.SchemaVersion = 0
				return
			}
			fn(p.ShaperRate)
		}
	}
	return map[string]func(*contracts.PolicyProposal){
		"schema version": func(p *contracts.PolicyProposal) { p.SchemaVersion = 2 },
		"short id":       func(p *contracts.PolicyProposal) { p.ProposalID = "a17" },
		"upper-case id":  func(p *contracts.PolicyProposal) { p.ProposalID = "0000000000000A17" },
		"negative time":  func(p *contracts.PolicyProposal) { p.TsNs = -1 },
		"unknown source": func(p *contracts.PolicyProposal) { p.Source = "someone" },
		"no source":      func(p *contracts.PolicyProposal) { p.Source = "" },
		"no reason":      func(p *contracts.PolicyProposal) { p.Reason = "" },
		"unknown kind":   func(p *contracts.PolicyProposal) { p.Kind = "tin_weights" },
		"no payload":     func(p *contracts.PolicyProposal) { p.FlowLabel, p.ShaperRate = nil, nil },
		"both payloads":  func(p *contracts.PolicyProposal) { *p = bothPayloads(*p) },
		"kind swapped": func(p *contracts.PolicyProposal) {
			p.Kind = map[string]string{"flow_label": "shaper_rate", "shaper_rate": "flow_label"}[p.Kind]
		},
		"unknown tin":     label(func(l *contracts.FlowLabel) { l.Tin = "express" }),
		"empty tin":       label(func(l *contracts.FlowLabel) { l.Tin = "" }),
		"unknown dscp":    label(func(l *contracts.FlowLabel) { l.DSCP = "CS7" }),
		"dscp of another": label(func(l *contracts.FlowLabel) { l.Tin, l.DSCP = contracts.TinVoice, "CS1" }),
		"bad flow id":     label(func(l *contracts.FlowLabel) { l.FlowID = "10.0.0.1:443" }),
		"bad host id":     label(func(l *contracts.FlowLabel) { l.HostID = "" }),
		"bad previous":    label(func(l *contracts.FlowLabel) { l.PreviousTin = ptr(contracts.Tin("fast")) }),
		"unknown class":   label(func(l *contracts.FlowLabel) { l.TopClass = ptr(contracts.Class("gaming")) }),
		"negative damage": label(func(l *contracts.FlowLabel) { l.ExpectedDamage = ptr(-0.5) }),
		"damage NaN":      label(func(l *contracts.FlowLabel) { l.ExpectedDamage = ptr(math.NaN()) }),
		"damage infinite": label(func(l *contracts.FlowLabel) { l.ExpectedDamage = ptr(math.Inf(1)) }),
		"unknown rule":    label(func(l *contracts.FlowLabel) { l.DecisionRule = "coin_toss" }),
		"sideways":        rate(func(r *contracts.ShaperRate) { r.Direction = "sideways" }),
		"zero rate":       rate(func(r *contracts.ShaperRate) { r.RateKbit = 0 }),
		"negative rate":   rate(func(r *contracts.ShaperRate) { r.RateKbit = -20_000 }),
		"zero previous":   rate(func(r *contracts.ShaperRate) { r.PreviousRateKbit = ptr(0) }),
	}
}

func bothPayloads(p contracts.PolicyProposal) contracts.PolicyProposal {
	if p.FlowLabel == nil {
		p.FlowLabel = labelProposal(contracts.TinVideo, "AF41", nil).FlowLabel
	}
	if p.ShaperRate == nil {
		p.ShaperRate = rateProposal("down", 50_000).ShaperRate
	}
	return p
}

func variantNames() []string {
	var names []string
	for name := range invalidVariants() {
		names = append(names, name)
	}
	// Sorted, so that a fuzzer's index means the same variant every run.
	for i := range names {
		for j := i + 1; j < len(names); j++ {
			if names[j] < names[i] {
				names[i], names[j] = names[j], names[i]
			}
		}
	}
	return names
}

func TestEveryInvalidProposalIsRejected(t *testing.T) {
	for name, mutate := range invalidVariants() {
		for _, valid := range []*contracts.PolicyProposal{
			labelProposal(contracts.TinVideo, "AF41", nil), rateProposal("down", 52_000),
		} {
			t.Run(name+"/"+valid.Kind, func(t *testing.T) {
				if v, _ := check(t, baseState(), valid); v.Verdict != "accept" {
					t.Fatalf("the valid proposal was not accepted: %s", v.Reason)
				}
				mutate(valid)
				v, code := check(t, baseState(), valid)
				if v.Verdict != "reject" || code != Invalid {
					t.Fatalf("got %s (%s)", v.Verdict, code)
				}
			})
		}
	}
	if v, code := check(t, baseState(), nil); v.Verdict != "reject" || code != Invalid {
		t.Fatalf("nil proposal: %s (%s)", v.Verdict, code)
	}
}

func TestCheckJSON(t *testing.T) {
	for _, raw := range []string{
		``, `{`, `null`, `[]`, `"flow_label"`, `{"schema_version":1}`,
		`{"schema_version":1,"proposal_id":"0000000000000a18","ts_ns":1,"source":"verifier","kind":"shaper_rate","reason":"x","shaper_rate":{"direction":"down","rate_kbit":52000,"previous_rate_kbit":null},"extra":1}`,
		`{"schema_version":1,"proposal_id":"0000000000000a18","ts_ns":1,"source":"verifier","kind":"shaper_rate","reason":"x","shaper_rate":{"direction":"down","rate_kbit":52000.5,"previous_rate_kbit":null}}`,
		`{"schema_version":1,"proposal_id":"0000000000000a18","ts_ns":1,"source":"verifier","kind":"shaper_rate","reason":"x","shaper_rate":{"direction":"down","rate_kbit":52000,"previous_rate_kbit":null}} {}`,
	} {
		if v, _ := CheckJSON(testCfg, baseState(), []byte(raw)); v.Verdict != "reject" {
			t.Errorf("%q was not rejected", raw)
		}
	}
	for _, name := range []string{"policy_proposal.flow_label.json", "policy_proposal.shaper_rate.json"} {
		raw, err := os.ReadFile(filepath.Join("..", "..", "contracts", "examples", name))
		if err != nil {
			t.Fatal(err)
		}
		st := baseState()
		st.Rates["down"] = Rate{Kbit: 20_000}
		v, _ := CheckJSON(testCfg, st, raw)
		if v.Verdict == "reject" {
			t.Errorf("%s rejected: %s", name, v.Reason)
		}
	}
}

func TestBrokenShieldRejects(t *testing.T) {
	broken := testCfg
	broken.MinRateKbit = map[string]int{"up": 5_000, "down": 900_000}
	if v, code := Check(broken, baseState(), rateProposal("down", 52_000)); v.Verdict != "reject" || code != Internal {
		t.Fatalf("misconfigured shield: %s (%s)", v.Verdict, code)
	}
	if v, code := Check(testCfg, nil, rateProposal("down", 52_000)); v.Verdict != "reject" || code != Internal {
		t.Fatalf("no state: %s (%s)", v.Verdict, code)
	}
}

func TestConfigFile(t *testing.T) {
	cfg, err := LoadConfig(filepath.Join("..", "..", "configs", "shield.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	want := DefaultConfig(cfg.MinRateKbit["up"], cfg.MaxRateKbit["up"], cfg.MinRateKbit["down"], cfg.MaxRateKbit["down"])
	if !reflect.DeepEqual(cfg, want) {
		t.Fatalf("configs/shield.yaml differs from the starting values:\n%+v\n%+v", cfg, want)
	}
}

func TestTracker(t *testing.T) {
	tr, err := NewTracker(testCfg)
	if err != nil {
		t.Fatal(err)
	}
	now := 10 * second
	apply := func(p *contracts.PolicyProposal) Outcome {
		t.Helper()
		out := tr.Decide(p, now)
		if out.Verdict.Verdict != "reject" {
			tr.Applied(p, out.Verdict, now)
		}
		return out
	}
	voice, video, best := contracts.TinVoice, contracts.TinVideo, contracts.TinBestEffort

	if out := apply(labelProposal(video, "AF41", nil)); out.Code != UnknownFlow {
		t.Fatalf("label before the flow was seen: %s", out.Code)
	}
	tr.FlowSeen(flowID, hostID)
	tr.FlowSeen(flowID, "ffffffffffffffff") // seen again: nothing changes
	steps := []struct {
		after    int64
		tin      contracts.Tin
		dscp     string
		previous *contracts.Tin
		code     Code
	}{
		{0, video, "AF41", nil, OK},         // first label, not a change
		{second, voice, "EF", &video, OK},   // change 1
		{second, video, "AF41", &voice, OK}, // change 2
		{second, best, "CS0", &video, FlipCap},
		{second, best, "CS0", nil, Pinned},
		{120 * second, best, "CS0", nil, Pinned}, // for the rest of its life
	}
	for i, s := range steps {
		now += s.after
		out := apply(labelProposal(s.tin, s.dscp, s.previous))
		if out.Code != s.code {
			t.Fatalf("step %d: got %s (%s), want %s", i, out.Code, out.Verdict.Reason, s.code)
		}
		if out.Unlabel != (s.code == FlipCap) {
			t.Fatalf("step %d: unlabel = %v", i, out.Unlabel)
		}
	}
	if flow, _ := tr.Flow(flowID); !flow.Pinned || flow.Tin != "" {
		t.Fatalf("pinned flow should be unlabelled: %+v", flow)
	}
	tr.FlowGone(flowID)
	if _, known := tr.Flow(flowID); known {
		t.Fatal("flow not forgotten")
	}

	// Rates: a rise, a second rise too soon, a drop at once, then a rise
	// once the interval has passed.
	if out := apply(rateProposal("down", 40_000)); out.Code != Stale {
		t.Fatalf("rate before it was read: %s", out.Code)
	}
	tr.RateRead("down", 50_000)
	for i, s := range []struct {
		after   int64
		kbit    int
		code    Code
		applied int
	}{
		{0, 60_000, RateStep, 55_000},
		{second, 56_000, RateInterval, 0},
		{second, 20_000, RateStep, 27_500},
		{second, 28_000, RateInterval, 0},
		{8 * second, 28_000, OK, 28_000},
	} {
		now += s.after
		out := apply(rateProposal("down", s.kbit))
		if out.Code != s.code || (s.applied != 0 && out.Verdict.Applied.RateKbit != s.applied) {
			t.Fatalf("rate step %d: got %s %+v, want %s %d", i, out.Code, out.Verdict.Applied, s.code, s.applied)
		}
	}
	if counts := tr.Counts(); counts[RateInterval] != 2 || counts[Pinned] != 2 || counts[FlipCap] != 1 {
		t.Fatalf("counts: %v", counts)
	}

	// The budget: fifty flows may change label in one second, not fifty-one.
	for i := 0; i <= testCfg.RemarkBudget; i++ {
		id := fmt.Sprintf("%016x", i+1)
		tr.FlowSeen(id, hostID)
		first := labelProposal(video, "AF41", nil)
		first.FlowLabel.FlowID = id
		apply(first)
	}
	now += second
	rejected := 0
	for i := 0; i <= testCfg.RemarkBudget; i++ {
		change := labelProposal(voice, "EF", &video)
		change.FlowLabel.FlowID = fmt.Sprintf("%016x", i+1)
		if apply(change).Code == RemarkBudget {
			rejected++
		}
	}
	if rejected != 1 {
		t.Fatalf("%d changes held back by the budget, want 1", rejected)
	}
	tr.SetStarvation(true)
	tr.FlowSeen(flowID, hostID)
	if out := apply(labelProposal(voice, "EF", nil)); out.Code != Starved {
		t.Fatalf("promotion during starvation: %s", out.Code)
	}
}

func TestStarvation(t *testing.T) {
	s := NewStarvation()
	s.Observe("a", 0, true, 2)
	s.Observe("b", 0, true, 5_000) // queued, but moving
	s.Observe("c", 0, false, 0)    // idle
	if on, _ := s.Alarm(1 * second); on {
		t.Fatal("alarm after 1 s")
	}
	s.Observe("a", 1*second, true, 0)
	if on, hosts := s.Alarm(2 * second); !on || len(hosts) != 1 || hosts[0] != "a" {
		t.Fatalf("alarm at 2 s: %v %v", on, hosts)
	}
	s.Observe("a", 3*second, true, 900)
	if on, _ := s.Alarm(3 * second); on {
		t.Fatal("alarm after recovery")
	}
	s.Observe("a", 4*second, true, 0)
	s.Forget("a")
	if on, _ := s.Alarm(9 * second); on {
		t.Fatal("alarm for a host that left")
	}
}

// FuzzCheck builds a proposal and a state from the fuzzer's numbers, so
// that most inputs reach the checks behind validation.
func FuzzCheck(f *testing.F) {
	f.Add(true, uint8(2), uint8(2), uint8(9), true, true, false, false, uint8(9), uint8(0), uint32(0), uint8(0), int64(0), int64(0), int64(0), int64(0), false, uint8(255))
	f.Add(true, uint8(3), uint8(0), uint8(2), true, true, false, false, uint8(2), uint8(2), uint32(5_000), uint8(49), int64(0), int64(0), int64(0), int64(0), false, uint8(255))
	f.Add(true, uint8(3), uint8(0), uint8(9), true, true, false, true, uint8(9), uint8(0), uint32(0), uint8(60), int64(0), int64(0), int64(0), int64(0), false, uint8(255))
	f.Add(false, uint8(0), uint8(0), uint8(0), true, true, false, false, uint8(0), uint8(0), uint32(0), uint8(0), int64(60_000), int64(20_000), int64(20_000), int64(11_000), true, uint8(255))
	f.Add(false, uint8(1), uint8(0), uint8(0), true, true, false, false, uint8(0), uint8(0), uint32(0), uint8(0), int64(1), int64(0), int64(math.MaxInt64), int64(3), true, uint8(255))
	f.Add(false, uint8(0), uint8(0), uint8(0), true, true, false, false, uint8(0), uint8(0), uint32(0), uint8(0), int64(math.MaxInt64), int64(0), int64(1), int64(-5), false, uint8(3))
	names, variants := variantNames(), invalidVariants()

	f.Fuzz(func(t *testing.T, isLabel bool, tin, dscp, previous uint8, known, sameHost, pinned, starved bool,
		currentTin, flips uint8, flipAgeMs uint32, remarks uint8,
		rate, previousRate, currentRate, sinceIncreaseMs int64, increased bool, variant uint8) {
		st := &State{NowNs: 1_000 * second, Flows: map[string]Flow{}, Rates: map[string]Rate{}, Starvation: starved}
		pick := func(i uint8) *contracts.Tin {
			if int(i) >= len(tinList) {
				return nil
			}
			return &tinList[i]
		}
		var p *contracts.PolicyProposal
		if isLabel {
			p = labelProposal(tinList[int(tin)%len(tinList)], dscps[int(dscp)%len(dscps)], pick(previous))
			flow := Flow{HostID: hostID, Pinned: pinned}
			if !sameHost {
				flow.HostID = "ffffffffffffffff"
			}
			if current := pick(currentTin); current != nil {
				flow.Tin = *current
			}
			for i := 0; i < int(flips%6); i++ {
				flow.Changes = append(flow.Changes, st.NowNs-int64(flipAgeMs)*1_000_000*int64(i+1))
			}
			if known {
				st.Flows[flowID] = flow
			}
			for i := 0; i < int(remarks); i++ {
				st.Remarks = append(st.Remarks, st.NowNs-int64(i)*15_000_000)
			}
		} else {
			direction := directions[int(tin)%2]
			p = rateProposal(direction, int(rate))
			if previousRate != 0 {
				p.ShaperRate.PreviousRateKbit = ptr(int(previousRate))
			}
			if known {
				st.Rates[direction] = Rate{
					Kbit: int(currentRate), Increased: increased,
					LastIncreaseNs: st.NowNs - sinceIncreaseMs*1_000_000,
				}
			}
		}
		mutated := int(variant) < len(names)
		if mutated {
			variants[names[variant]](p)
		}
		before := fmt.Sprintf("%+v", *st)
		v, code := Check(testCfg, st, p)
		if fmt.Sprintf("%+v", *st) != before {
			t.Fatal("Check changed the state")
		}
		if mutated && (v.Verdict != "reject" || code != Invalid) {
			t.Fatalf("invalid proposal (%s) got %s (%s)", names[variant], v.Verdict, code)
		}
		if code == Internal {
			t.Fatalf("the shield failed: %s", v.Reason)
		}
		for _, problem := range violations(testCfg, st, p, v) {
			t.Errorf("%s\nproposal %s\nstate %s", problem, encode(p), before)
		}
	})
}

// FuzzCheckJSON feeds raw bytes, seeded with the contract examples: the
// proposals as they would arrive from another process.
func FuzzCheckJSON(f *testing.F) {
	examples, _ := filepath.Glob(filepath.Join("..", "..", "contracts", "examples", "policy_proposal.*.json"))
	if len(examples) == 0 {
		f.Fatal("no contract examples found")
	}
	for _, name := range examples {
		raw, err := os.ReadFile(name)
		if err != nil {
			f.Fatal(err)
		}
		f.Add(raw, int64(20_000), int64(60_000), false)
		var valid contracts.PolicyProposal
		if err := json.Unmarshal(raw, &valid); err != nil {
			f.Fatal(err)
		}
		for _, mutate := range invalidVariants() {
			p := valid
			if p.FlowLabel != nil {
				p.FlowLabel = ptr(*p.FlowLabel)
			}
			if p.ShaperRate != nil {
				p.ShaperRate = ptr(*p.ShaperRate)
			}
			mutate(&p)
			if raw, err := json.Marshal(p); err == nil { // NaN and infinity do not encode
				f.Add(raw, int64(20_000), int64(1_000), true)
			}
		}
	}
	f.Fuzz(func(t *testing.T, raw []byte, currentRate, sinceIncreaseMs int64, starved bool) {
		st := &State{
			NowNs: 1_000 * second, Starvation: starved,
			Flows: map[string]Flow{flowID: {HostID: hostID}},
			Rates: map[string]Rate{
				"up":   {Kbit: int(currentRate)},
				"down": {Kbit: int(currentRate), Increased: true, LastIncreaseNs: 1_000*second - sinceIncreaseMs*1_000_000},
			},
		}
		v, code := CheckJSON(testCfg, st, raw)
		if code == Internal {
			t.Fatalf("the shield failed: %s", v.Reason)
		}
		var p contracts.PolicyProposal
		dec := json.NewDecoder(bytes.NewReader(raw))
		dec.DisallowUnknownFields()
		if err := dec.Decode(&p); err != nil || dec.More() {
			if v.Verdict != "reject" {
				t.Fatalf("bytes that are not a proposal got %s", v.Verdict)
			}
			return
		}
		for _, problem := range violations(testCfg, st, &p, v) {
			t.Errorf("%s\nproposal %s", problem, raw)
		}
	})
}

func encode(p *contracts.PolicyProposal) string {
	raw, err := json.Marshal(p)
	if err != nil {
		return fmt.Sprintf("%+v", p)
	}
	return string(raw)
}
