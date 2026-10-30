package classify

import (
	"compress/gzip"
	"encoding/json"
	"math"
	"os"
	"strings"
	"testing"

	"adaptiveqos/contracts"
)

type fixture struct {
	ModelVersion string `json:"model_version"`
	Cases        []struct {
		Record contracts.FeatureRecord  `json:"record"`
		Probs  []float64                `json:"probs"`
		Tins   map[string]contracts.Tin `json:"tins"`
	} `json:"cases"`
}

func load(t *testing.T) (*Model, fixture) {
	t.Helper()
	f, err := os.Open("testdata/fixture.json.gz")
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	gz, err := gzip.NewReader(f)
	if err != nil {
		t.Fatal(err)
	}
	var fx fixture
	if err := json.NewDecoder(gz).Decode(&fx); err != nil {
		t.Fatal(err)
	}
	m, err := Load("../../models/" + fx.ModelVersion + "/trees.json")
	if err != nil {
		t.Fatal(err)
	}
	return m, fx
}

// The Go model gives the Python service's probabilities on every record
// of the fixture, and the Go decision rule its tins.
func TestTheSameAnswersAsThePythonService(t *testing.T) {
	m, fx := load(t)
	damage, err := LoadDamage("../../configs/damage_table.yaml")
	if err != nil {
		t.Fatal(err)
	}
	o2, _ := NewEngine(damage, false)
	o1, _ := NewEngine(damage, true)
	worst, initial, window := 0.0, 0, 0
	for i, c := range fx.Cases {
		x := Vector(&c.Record)
		probs := m.Probabilities(&x)
		for k := range probs {
			worst = math.Max(worst, math.Abs(probs[k]-c.Probs[k]))
		}
		for mode, e := range map[string]*Engine{"expected_damage": o2, "top_label": o1} {
			// Decided on Python's probabilities, so that a disagreement
			// here is the rule's and not the model's.
			ch, err := e.Choose(c.Probs, nil, &c.Record)
			if err != nil {
				t.Fatalf("case %d: %v", i, err)
			}
			if ch.Tin != c.Tins[mode] {
				t.Fatalf("case %d, %s: Go chose %s, Python %s (probs %v)", i, mode, ch.Tin, c.Tins[mode], c.Probs)
			}
		}
		if c.Record.Kind == "initial" {
			initial++
		} else {
			window++
		}
	}
	if worst > 1e-9 {
		t.Fatalf("probabilities differ from Python's by up to %g", worst)
	}
	if len(fx.Cases) < 1000 || initial == 0 || window == 0 {
		t.Fatalf("fixture too thin: %d cases, %d initial, %d window", len(fx.Cases), initial, window)
	}
	t.Logf("%d records (%d initial, %d window): probabilities within %.1e, every tin the same", len(fx.Cases), initial, window, worst)
}

func TestTheGuardAndTheBars(t *testing.T) {
	damage, err := LoadDamage("../../configs/damage_table.yaml")
	if err != nil {
		t.Fatal(err)
	}
	e, _ := NewEngine(damage, false)
	sure := []float64{0, 0, 0, 0, 0.03, 0, 0.97} // background transfer
	tcp := &contracts.FeatureRecord{Kind: "window", Protocol: 6}
	if ch, _ := e.Choose(sure, nil, tcp); ch.Tin != contracts.TinBulk {
		t.Fatalf("a sure TCP window went to %s", ch.Tin)
	}
	udp := &contracts.FeatureRecord{Kind: "window", Protocol: 17}
	if ch, _ := e.Choose(sure, nil, udp); ch.Tin != contracts.TinBestEffort || !ch.Guarded {
		t.Fatalf("a UDP flow went to %s", ch.Tin)
	}
	if _, err := e.Choose([]float64{0.5, 0.5, 0.5, 0, 0, 0, 0}, nil, nil); err == nil || !strings.Contains(err.Error(), "sum") {
		t.Fatal("probabilities that do not sum to one were decided on")
	}
}

func BenchmarkClassifyOne(b *testing.B) {
	t := &testing.T{}
	m, fx := load(t)
	x := Vector(&fx.Cases[0].Record)
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		m.Probabilities(&x)
	}
}
