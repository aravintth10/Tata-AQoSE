package classify

import (
	"fmt"
	"math"
	"os"

	"gopkg.in/yaml.v3"

	"adaptiveqos/contracts"
)

// Tins in the order the decision rule considers them.
var Tins = []contracts.Tin{contracts.TinVoice, contracts.TinVideo, contracts.TinBestEffort, contracts.TinBulk}

// tieOrder is where a tie goes when the top class's own tin is not among
// the tied ones (decide.py's _TIE_ORDER).
var tieOrder = map[contracts.Tin]int{contracts.TinBestEffort: 0, contracts.TinVideo: 1, contracts.TinVoice: 2, contracts.TinBulk: 3}

// Interactive classes are the ones a lease protects.
var Interactive = map[contracts.Class]bool{
	contracts.Telephony: true, contracts.RealtimeInteractive: true, contracts.MultimediaConferencing: true,
}

// Damage is the measured damage table: class, then tin.
type Damage map[contracts.Class]map[contracts.Tin]float64

// LoadDamage reads configs/damage_table.yaml.
func LoadDamage(path string) (Damage, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var doc struct {
		Damage Damage `yaml:"damage"`
	}
	if err := yaml.Unmarshal(raw, &doc); err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	for _, c := range Classes {
		for _, t := range Tins {
			if _, ok := doc.Damage[c][t]; !ok {
				return nil, fmt.Errorf("%s: no damage for %s in %s", path, c, t)
			}
		}
	}
	return doc.Damage, nil
}

// Engine is decide.py's Engine: probabilities in, one tin out.
type Engine struct {
	Damage Damage
	// TopLabel decides by the most likely class (O1, and the rules);
	// otherwise by the lowest expected damage (O2).
	TopLabel bool
	// RuleName goes in proposals: "expected_damage", "top_label" or "rules".
	RuleName        string
	DemotionBar     float64 // 0.8
	VoiceBar        float64 // 0.5
	TieMargin       float64 // 0.05
	LeaseMultiplier float64 // 5
	// BulkGuard keeps a flow out of Bulk on its first packets and while it
	// is UDP, whatever the model says. Off for the rules.
	BulkGuard bool

	taxonomy *contracts.Taxonomy
	bulky    map[contracts.Class]bool // the classes whose place is Bulk
	voicy    map[contracts.Class]bool // the classes whose place is Voice
}

// NewEngine sets the starting values decide.py uses.
func NewEngine(damage Damage, topLabel bool) (*Engine, error) {
	tax, err := contracts.LoadTaxonomy()
	if err != nil {
		return nil, err
	}
	e := &Engine{Damage: damage, TopLabel: topLabel, RuleName: "expected_damage", DemotionBar: 0.8,
		VoiceBar: 0.5, TieMargin: 0.05, LeaseMultiplier: 5, BulkGuard: true, taxonomy: tax,
		bulky: map[contracts.Class]bool{}, voicy: map[contracts.Class]bool{}}
	if topLabel {
		e.RuleName = "top_label"
	}
	for _, c := range Classes {
		if tax.Classes[c].Tin == contracts.TinVoice {
			e.voicy[c] = true
		}
		if topLabel {
			e.bulky[c] = tax.Classes[c].Tin == contracts.TinBulk
		} else {
			lowest := math.Inf(1)
			for _, t := range Tins {
				lowest = math.Min(lowest, damage[c][t])
			}
			e.bulky[c] = damage[c][contracts.TinBulk] <= lowest
		}
	}
	return e, nil
}

// Choice is a decision with what went into it.
type Choice struct {
	Tin      contracts.Tin
	Top      contracts.Class
	Expected map[contracts.Tin]float64
	Held     bool // Bulk was chosen and then refused
	Guarded  bool // refused by the guard, not by the bar
}

// Choose is decide.py's Engine.choose. probs follow Classes; leased are
// the classes a lease protects on the flow's device; rec may be nil.
func (e *Engine) Choose(probs []float64, leased []contracts.Class, rec *contracts.FeatureRecord) (Choice, error) {
	sum := 0.0
	for _, p := range probs {
		if !(p >= 0 && p <= 1) {
			return Choice{}, fmt.Errorf("a probability is outside 0 to 1")
		}
		sum += p
	}
	if len(probs) != len(Classes) || math.Abs(sum-1) > 1e-6 {
		return Choice{}, fmt.Errorf("probabilities sum to %.6f, not 1", sum)
	}
	top := 0
	for i, p := range probs {
		if p > probs[top] {
			top = i // ties stay with the class listed first
		}
	}
	lease := map[contracts.Class]bool{}
	for _, c := range leased {
		lease[c] = Interactive[c]
	}
	ch := Choice{Top: Classes[top], Expected: map[contracts.Tin]float64{}}
	for _, t := range Tins {
		for i, c := range Classes {
			d := e.Damage[c][t]
			if lease[c] && (t == contracts.TinBestEffort || t == contracts.TinBulk) {
				d *= e.LeaseMultiplier
			}
			ch.Expected[t] += probs[i] * d
		}
	}
	if e.TopLabel {
		ch.Tin = e.taxonomy.Classes[ch.Top].Tin
	} else {
		voice := 0.0
		for i, c := range Classes {
			if e.voicy[c] {
				voice += probs[i]
			}
		}
		var allowed []contracts.Tin
		for _, t := range Tins {
			if t != contracts.TinVoice || voice > e.VoiceBar {
				allowed = append(allowed, t)
			}
		}
		lowest := math.Inf(1)
		for _, t := range allowed {
			lowest = math.Min(lowest, ch.Expected[t])
		}
		var tied []contracts.Tin
		for _, t := range allowed {
			if ch.Expected[t]-lowest <= e.TieMargin {
				tied = append(tied, t)
			}
		}
		own := e.taxonomy.Classes[ch.Top].Tin
		ch.Tin = ""
		for _, t := range tied {
			if t == own && own != contracts.TinBulk {
				ch.Tin = own
			}
		}
		if ch.Tin == "" {
			ch.Tin = tied[0]
			for _, t := range tied {
				if tieOrder[t] < tieOrder[ch.Tin] {
					ch.Tin = t
				}
			}
		}
	}
	if ch.Tin == contracts.TinBulk {
		bulk := 0.0
		for i, c := range Classes {
			if e.bulky[c] {
				bulk += probs[i]
			}
		}
		ch.Guarded = e.BulkGuard && rec != nil && (rec.Kind == "initial" || rec.Protocol == 17)
		ch.Held = bulk <= e.DemotionBar || ch.Guarded
		if ch.Held {
			ch.Tin = contracts.TinBestEffort
		}
	}
	return ch, nil
}

// DSCP is the code point a proposal carries: the class's own when the
// tin is the class's, the tin's otherwise.
func (e *Engine) DSCP(top contracts.Class, tin contracts.Tin) string {
	if c := e.taxonomy.Classes[top]; c.Tin == tin {
		return c.DSCP
	}
	return e.taxonomy.Tins[tin].DSCP
}
