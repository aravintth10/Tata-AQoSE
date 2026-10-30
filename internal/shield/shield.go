// Package shield is the gatekeeper between everything that wants to change
// CAKE's state and the enforcer. Every proposal, whatever its source, gets
// one of three verdicts: accept, clamp (accept with a safer value) or
// reject.
//
// Check is a pure function of the configuration, the state and the
// proposal. It changes nothing, which is what lets a fuzzer throw millions
// of proposals at it. Tracker (tracker.go) keeps the state between calls.
package shield

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math"
	"os"
	"time"

	"gopkg.in/yaml.v3"

	"adaptiveqos/contracts"
)

// Code says which check decided a verdict. The reason string is for
// people; this is for counters and for the controller.
type Code string

const (
	OK           Code = "ok"
	Invalid      Code = "invalid"       // not a proposal the contract allows
	UnknownFlow  Code = "unknown_flow"  // not in the flow table, or wrong host
	Stale        Code = "stale"         // the proposer's view of the current state is out of date
	Pinned       Code = "pinned"        // the flow hit the flip cap earlier
	FlipCap      Code = "flip_cap"      // this change would be one too many
	RemarkBudget Code = "remark_budget" // too many label changes this second
	Starved      Code = "starvation"    // promotions are suspended
	RateBounds   Code = "rate_bounds"   // clamped to the configured minimum or maximum
	RateStep     Code = "rate_step"     // clamped to one step
	RateInterval Code = "rate_interval" // an increase too soon after the last one
	NoChange     Code = "no_change"     // nothing left of the change after clamping
	Internal     Code = "internal"      // the shield itself failed
)

// Config holds the bounds. Every value is a starting value from the
// specification and lives in configs/shield.yaml.
type Config struct {
	MinRateKbit map[string]int `yaml:"min_rate_kbit"` // by direction: up, down
	MaxRateKbit map[string]int `yaml:"max_rate_kbit"`
	// Largest change of a shaper rate in one proposal, as a fraction of
	// the current rate.
	MaxIncrease float64 `yaml:"max_increase"`
	MaxDecrease float64 `yaml:"max_decrease"`
	// Shortest time between two increases in one direction. Decreases are
	// never delayed: lowering the rate is the safe direction.
	IncreaseIntervalMs int64 `yaml:"increase_interval_ms"`
	// A flow whose label has already changed FlipLimit times within
	// FlipWindowMs is pinned to unlabelled at the next change.
	FlipLimit    int   `yaml:"flip_limit"`
	FlipWindowMs int64 `yaml:"flip_window_ms"`
	// Label changes allowed per second over all flows.
	RemarkBudget int `yaml:"remark_budget"`
}

// DefaultConfig is the specification's starting values around the given
// rate bounds.
func DefaultConfig(minUp, maxUp, minDown, maxDown int) Config {
	return Config{
		MinRateKbit: map[string]int{"up": minUp, "down": minDown},
		MaxRateKbit: map[string]int{"up": maxUp, "down": maxDown},
		MaxIncrease: 0.10, MaxDecrease: 0.50, IncreaseIntervalMs: 10_000,
		FlipLimit: 2, FlipWindowMs: 60_000, RemarkBudget: 50,
	}
}

// LoadConfig reads and validates a configuration file.
func LoadConfig(path string) (Config, error) {
	var cfg Config
	data, err := os.ReadFile(path)
	if err != nil {
		return cfg, err
	}
	dec := yaml.NewDecoder(bytes.NewReader(data))
	dec.KnownFields(true)
	if err := dec.Decode(&cfg); err != nil {
		return cfg, fmt.Errorf("%s: %w", path, err)
	}
	if err := cfg.Validate(); err != nil {
		return cfg, fmt.Errorf("%s: %w", path, err)
	}
	return cfg, nil
}

// Validate refuses a configuration the shield could not keep its promises
// under, such as a minimum above the maximum.
func (c Config) Validate() error {
	for _, dir := range directions {
		lo, hi := c.MinRateKbit[dir], c.MaxRateKbit[dir]
		if lo < 1 || hi < lo {
			return fmt.Errorf("rate bounds for %s must satisfy 1 <= min <= max, got %d and %d", dir, lo, hi)
		}
	}
	switch {
	case !(c.MaxIncrease > 0 && c.MaxIncrease <= 1):
		return fmt.Errorf("max_increase must be in (0, 1], got %v", c.MaxIncrease)
	case !(c.MaxDecrease > 0 && c.MaxDecrease < 1):
		return fmt.Errorf("max_decrease must be in (0, 1), got %v", c.MaxDecrease)
	case c.IncreaseIntervalMs < 0 || c.FlipWindowMs <= 0:
		return fmt.Errorf("intervals must be positive")
	case c.FlipLimit < 0 || c.RemarkBudget < 1:
		return fmt.Errorf("flip_limit must be >= 0 and remark_budget >= 1")
	}
	return nil
}

// Flow is what the shield knows about one live flow.
type Flow struct {
	HostID string
	Tin    contracts.Tin // "" while unlabelled
	// When its label was changed from one tin to another, CLOCK_MONOTONIC
	// nanoseconds. The first label is not a change.
	Changes []int64
	Pinned  bool
}

// Rate is the shaper's state in one direction.
type Rate struct {
	Kbit           int   // 0 while unknown
	LastIncreaseNs int64 // meaningful only when Increased is set
	Increased      bool
}

// State is everything Check reads besides the proposal.
type State struct {
	NowNs   int64
	Flows   map[string]Flow
	Rates   map[string]Rate // by direction
	Remarks []int64         // times of recent label changes, any flow
	// Starvation is set while a host has a queue and almost no throughput.
	Starvation bool
}

var (
	directions = []string{"up", "down"}
	sources    = map[string]bool{
		"decision_engine": true, "reclassifier": true, "capacity_estimator": true,
		"intent_service": true, "verifier": true, "user_override": true,
	}
	rules    = map[string]bool{"top_label": true, "expected_damage": true, "rules": true, "override": true, "fallback": true}
	taxonomy = mustTaxonomy()
)

func mustTaxonomy() *contracts.Taxonomy {
	t, err := contracts.LoadTaxonomy()
	if err != nil {
		panic(err)
	}
	return t
}

// noID stands in for a proposal id that cannot be repeated in a verdict.
const noID = "0000000000000000"

// Check gives the verdict for one proposal. It never panics and never
// changes cfg, st or p.
func Check(cfg Config, st *State, p *contracts.PolicyProposal) (verdict contracts.PolicyVerdict, code Code) {
	id, now := noID, int64(0)
	if p != nil && isID(p.ProposalID) {
		id = p.ProposalID
	}
	if st != nil && st.NowNs > 0 {
		now = st.NowNs
	}
	reject := func(code Code, format string, args ...any) (contracts.PolicyVerdict, Code) {
		return contracts.PolicyVerdict{
			SchemaVersion: contracts.SchemaVersion, ProposalID: id, TsNs: now,
			Verdict: "reject", Reason: fmt.Sprintf(format, args...),
		}, code
	}
	defer func() {
		if r := recover(); r != nil {
			verdict, code = reject(Internal, "The safety shield failed (%v); nothing was changed.", r)
		}
	}()
	if st == nil {
		return reject(Internal, "The safety shield has no state; nothing was changed.")
	}
	if err := cfg.Validate(); err != nil {
		return reject(Internal, "The safety shield is misconfigured (%v); nothing was changed.", err)
	}
	if problem := invalid(p); problem != "" {
		return reject(Invalid, "Not a valid proposal: %s.", problem)
	}
	pass := func(code Code, reason string, applied contracts.Applied, clamped bool) (contracts.PolicyVerdict, Code) {
		v := contracts.PolicyVerdict{
			SchemaVersion: contracts.SchemaVersion, ProposalID: id, TsNs: now,
			Verdict: "accept", Reason: reason, Applied: &applied,
		}
		if clamped {
			v.Verdict = "clamp"
		}
		return v, code
	}

	if p.Kind == "flow_label" {
		l := p.FlowLabel
		flow, known := st.Flows[l.FlowID]
		switch {
		case !known:
			return reject(UnknownFlow, "The flow is not in the flow table; it may have ended.")
		case flow.HostID != l.HostID:
			return reject(UnknownFlow, "The flow belongs to a different device than the proposal says.")
		case flow.Pinned:
			return reject(Pinned, "The flow changed label too often and is pinned to unlabelled.")
		case (l.PreviousTin == nil && flow.Tin != "") || (l.PreviousTin != nil && *l.PreviousTin != flow.Tin):
			return reject(Stale, "The flow's label changed since this proposal was made.")
		}
		applied := contracts.Applied{Tin: l.Tin, DSCP: l.DSCP}
		if l.Tin == flow.Tin {
			return pass(OK, "The flow is already in that tin.", applied, false)
		}
		if st.Starvation && (l.Tin == contracts.TinVoice || l.Tin == contracts.TinVideo) {
			return reject(Starved, "A device is being starved; promotions to Voice and Video are suspended.")
		}
		if flow.Tin != "" {
			if recent(flow.Changes, now, cfg.FlipWindowMs*int64(time.Millisecond)) >= cfg.FlipLimit {
				return reject(FlipCap, "The flow's label has already changed %d times in %d s; it is pinned to unlabelled.",
					cfg.FlipLimit, cfg.FlipWindowMs/1000)
			}
			if recent(st.Remarks, now, int64(time.Second)) >= cfg.RemarkBudget {
				return reject(RemarkBudget, "More than %d label changes in the last second; this one waits.", cfg.RemarkBudget)
			}
		}
		return pass(OK, "Within every bound.", applied, false)
	}

	r := p.ShaperRate
	current := st.Rates[r.Direction]
	if current.Kbit < 1 {
		return reject(Stale, "The current %sload rate is not known yet.", r.Direction)
	}
	if r.PreviousRateKbit != nil && *r.PreviousRateKbit != current.Kbit {
		return reject(Stale, "The %sload rate changed since this proposal was made.", r.Direction)
	}
	lo, hi := Allowed(cfg, r.Direction, current.Kbit)
	target, code, reason := r.RateKbit, OK, "Within every bound."
	if target < lo || target > hi {
		target = min(max(target, lo), hi)
		if target == cfg.MinRateKbit[r.Direction] || target == cfg.MaxRateKbit[r.Direction] {
			code, reason = RateBounds, fmt.Sprintf("Rate limited to the configured range, %d to %d kbit/s.",
				cfg.MinRateKbit[r.Direction], cfg.MaxRateKbit[r.Direction])
		} else if target > current.Kbit {
			code, reason = RateStep, "Rate increase limited to the maximum upward step."
		} else {
			code, reason = RateStep, "Rate decrease limited to the maximum downward step."
		}
	}
	if target == current.Kbit && r.RateKbit != current.Kbit {
		return reject(NoChange, "The %sload rate is already at the limit in that direction.", r.Direction)
	}
	if target > current.Kbit && current.Increased {
		since := now - current.LastIncreaseNs
		if since < cfg.IncreaseIntervalMs*int64(time.Millisecond) {
			return reject(RateInterval, "The %sload rate was raised less than %d s ago.", r.Direction, cfg.IncreaseIntervalMs/1000)
		}
	}
	return pass(code, reason, contracts.Applied{RateKbit: target}, target != r.RateKbit)
}

// Allowed is the range a shaper rate may move to in one proposal from
// current. One step either way, inside the configured bounds. The bounds
// win over the step: a rate that is somehow outside them may return in one
// move.
func Allowed(cfg Config, direction string, current int) (lo, hi int) {
	floor, ceiling := cfg.MinRateKbit[direction], cfg.MaxRateKbit[direction]
	down := int(math.Ceil(float64(current) * (1 - cfg.MaxDecrease)))
	// Compared as floats first: a step up from a very large rate must not
	// overflow on its way to the ceiling.
	up := int(math.Min(float64(ceiling), math.Floor(float64(current)*(1+cfg.MaxIncrease))))
	lo = min(max(floor, down), ceiling)
	hi = max(min(ceiling, up), floor)
	return lo, hi
}

// CheckJSON is Check for a proposal as it arrives. Anything that does not
// decode, or carries a field the contract does not have, is rejected.
func CheckJSON(cfg Config, st *State, raw []byte) (contracts.PolicyVerdict, Code) {
	var p contracts.PolicyProposal
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&p); err != nil {
		return Check(cfg, st, nil)
	}
	if dec.More() {
		return Check(cfg, st, nil)
	}
	return Check(cfg, st, &p)
}

// recent counts the times within window before now.
func recent(times []int64, now, window int64) int {
	n := 0
	for _, t := range times {
		if t <= now && now-t < window {
			n++
		}
	}
	return n
}

func isID(s string) bool {
	if len(s) != 16 {
		return false
	}
	for _, c := range []byte(s) {
		if !(c >= '0' && c <= '9' || c >= 'a' && c <= 'f') {
			return false
		}
	}
	return true
}

// invalid returns what is wrong with p against the contract, or "".
func invalid(p *contracts.PolicyProposal) string {
	switch {
	case p == nil:
		return "it could not be read"
	case p.SchemaVersion != contracts.SchemaVersion:
		return "unknown schema version"
	case !isID(p.ProposalID):
		return "bad proposal id"
	case p.TsNs < 0:
		return "negative time"
	case !sources[p.Source]:
		return "unknown source"
	case p.Reason == "":
		return "no reason given"
	}
	switch p.Kind {
	case "flow_label":
		l := p.FlowLabel
		if l == nil || p.ShaperRate != nil {
			return "a flow label proposal carries exactly a flow label"
		}
		dscp, knownDSCP := taxonomy.DSCP[l.DSCP]
		_, knownTin := taxonomy.Tins[l.Tin]
		switch {
		case !isID(l.FlowID) || !isID(l.HostID):
			return "bad flow or host id"
		case !knownTin:
			return "unknown tin"
		case !knownDSCP || dscp.Tin != l.Tin:
			return "the DSCP does not belong to the tin"
		case !rules[l.DecisionRule]:
			return "unknown decision rule"
		}
		if l.PreviousTin != nil {
			if _, ok := taxonomy.Tins[*l.PreviousTin]; !ok {
				return "unknown previous tin"
			}
		}
		if l.TopClass != nil {
			if _, ok := taxonomy.Classes[*l.TopClass]; !ok {
				return "unknown class"
			}
		}
		if d := l.ExpectedDamage; d != nil && !(*d >= 0 && !math.IsInf(*d, 0)) {
			return "expected damage is not a number from zero up"
		}
	case "shaper_rate":
		r := p.ShaperRate
		switch {
		case r == nil || p.FlowLabel != nil:
			return "a shaper rate proposal carries exactly a shaper rate"
		case r.Direction != "up" && r.Direction != "down":
			return "unknown direction"
		case r.RateKbit < 1:
			return "the rate must be at least 1 kbit/s"
		case r.PreviousRateKbit != nil && *r.PreviousRateKbit < 1:
			return "the previous rate must be at least 1 kbit/s"
		}
	default:
		return "unknown kind"
	}
	return ""
}
