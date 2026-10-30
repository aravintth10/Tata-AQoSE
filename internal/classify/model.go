// Package classify evaluates the trained classifier and the decision rule
// in Go, so that a router needs no Python. The model is the LightGBM
// ensemble written as plain data by `python -m aqos_ml.export`; the
// decision rule is ml/aqos_ml/decide.py's. Both must give the Python
// service's answers exactly: testdata/fixture.json.gz holds those answers
// for 2,000 dataset records, and the tests compare against them.
package classify

import (
	"encoding/json"
	"fmt"
	"math"
	"os"

	"adaptiveqos/contracts"
)

// Classes is the taxonomy's class order, which probabilities follow.
var Classes = []contracts.Class{
	contracts.Telephony, contracts.RealtimeInteractive, contracts.MultimediaConferencing,
	contracts.MultimediaStreaming, contracts.Standard, contracts.HighThroughputData, contracts.LowPriorityData,
}

// Features is how many numbers the model reads (aqos_ml.features.NAMES).
const Features = 28

type tree struct {
	Class int `json:"class"`
	Root  int `json:"root"`
	// Nodes are [feature, threshold, default_left, missing_is_nan, left,
	// right]; a child n >= 0 is node n and -(i + 1) is leaf i.
	Nodes  [][6]any  `json:"nodes"`
	Leaves []float64 `json:"leaves"`

	feature   []int
	threshold []float64
	defLeft   []bool
	nanMiss   []bool
	left      []int
	right     []int
}

// Model is a gradient-boosted tree ensemble with temperature calibration.
type Model struct {
	Version     string `json:"model_version"`
	FeaturesVer int    `json:"features_version"`
	// ModelClasses is the order the trees' classes come in; Classes the
	// order of the answer.
	ModelClasses []contracts.Class `json:"model_classes"`
	Temperature  float64           `json:"temperature"`
	Trees        []*tree           `json:"trees"`

	toTaxonomy []int // model class index to taxonomy index
}

// Load reads a trees.json written by aqos_ml.export.
func Load(path string) (*Model, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var m Model
	if err := json.Unmarshal(raw, &m); err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	if m.FeaturesVer != 1 {
		return nil, fmt.Errorf("%s: features version %d; this code reads version 1", path, m.FeaturesVer)
	}
	index := map[contracts.Class]int{}
	for i, c := range Classes {
		index[c] = i
	}
	for _, c := range m.ModelClasses {
		i, ok := index[c]
		if !ok {
			return nil, fmt.Errorf("%s: unknown class %q", path, c)
		}
		m.toTaxonomy = append(m.toTaxonomy, i)
	}
	for _, t := range m.Trees {
		if t.Class < 0 || t.Class >= len(m.ModelClasses) {
			return nil, fmt.Errorf("%s: a tree for class %d", path, t.Class)
		}
		for _, n := range t.Nodes {
			f, ok1 := n[0].(float64)
			th, ok2 := n[1].(float64)
			dl, ok3 := n[2].(bool)
			nan, ok4 := n[3].(bool)
			l, ok5 := n[4].(float64)
			r, ok6 := n[5].(float64)
			if !(ok1 && ok2 && ok3 && ok4 && ok5 && ok6) || f < 0 || f >= Features {
				return nil, fmt.Errorf("%s: a malformed node %v", path, n)
			}
			t.feature = append(t.feature, int(f))
			t.threshold = append(t.threshold, th)
			t.defLeft = append(t.defLeft, dl)
			t.nanMiss = append(t.nanMiss, nan)
			t.left = append(t.left, int(l))
			t.right = append(t.right, int(r))
		}
		t.Nodes = nil
	}
	return &m, nil
}

// leaf walks one tree as LightGBM does for a "<=" split: a missing value
// (NaN) goes the default way where the split was trained with missing
// values, and counts as zero where it was not.
func (t *tree) leaf(x *[Features]float64) float64 {
	n := t.Root
	for n >= 0 {
		v := x[t.feature[n]]
		var goLeft bool
		switch {
		case math.IsNaN(v) && t.nanMiss[n]:
			goLeft = t.defLeft[n]
		case math.IsNaN(v):
			goLeft = 0 <= t.threshold[n]
		default:
			goLeft = v <= t.threshold[n]
		}
		if goLeft {
			n = t.left[n]
		} else {
			n = t.right[n]
		}
	}
	return t.Leaves[-n-1]
}

// Probabilities returns one probability per class, in Classes order.
func (m *Model) Probabilities(x *[Features]float64) []float64 {
	raw := make([]float64, len(m.ModelClasses))
	for _, t := range m.Trees {
		raw[t.Class] += t.leaf(x)
	}
	// Temperature-scaled softmax, as scikit-learn's calibration does.
	top := math.Inf(-1)
	for i := range raw {
		raw[i] *= m.Temperature
		top = math.Max(top, raw[i])
	}
	sum := 0.0
	for i := range raw {
		raw[i] = math.Exp(raw[i] - top)
		sum += raw[i]
	}
	out := make([]float64, len(Classes))
	for i, v := range raw {
		out[m.toTaxonomy[i]] = v / sum
	}
	return out
}

// portClass is IANA's three port ranges: 0 system, 1 registered, 2 dynamic.
func portClass(port int) float64 {
	switch {
	case port < 1024:
		return 0
	case port < 49152:
		return 1
	}
	return 2
}

// Vector turns a feature record into the model's input, exactly as
// aqos_ml.features.vector does: missing window statistics are NaN.
func Vector(r *contracts.FeatureRecord) [Features]float64 {
	var x [Features]float64
	for i, s := range r.PktSizes {
		x[i] = float64(s)
	}
	for i, g := range r.PktGapsUs {
		x[8+i] = float64(g)
	}
	x[15], x[16] = float64(r.IPVersion), float64(r.Protocol)
	x[17], x[18] = portClass(r.SrcPort), portClass(r.DstPort)
	w := r.Window
	if w == nil {
		for i := 19; i < Features; i++ {
			x[i] = math.NaN()
		}
		return x
	}
	copy(x[19:], []float64{w.PktsUpPerS, w.PktsDownPerS, w.BytesUpPerS, w.BytesDownPerS,
		w.MeanPktSizeUp, w.MeanPktSizeDown, w.PktSizeSpread, w.RateCV, w.Burstiness})
	return x
}
