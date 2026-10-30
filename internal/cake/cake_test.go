package cake

import (
	"os"
	"testing"
)

func TestParse(t *testing.T) {
	out, err := os.ReadFile("testdata/ifb0.json")
	if err != nil {
		t.Fatal(err)
	}
	st, err := Parse(out)
	if err != nil || st == nil {
		t.Fatalf("Parse: %v, %v", st, err)
	}
	// 95 Mbit/s is reported as 11,875,000 bytes per second.
	if st.RateKbit != 95000 {
		t.Errorf("rate %v kbit, want 95000", st.RateKbit)
	}
	if len(st.Tins) != 4 {
		t.Fatalf("%d tins", len(st.Tins))
	}
	if be := st.Tins[1]; be.AvgDelayUs != 1946 || be.Drops != 3 {
		t.Errorf("best effort tin: %+v", be)
	}
}

func TestParseNotCake(t *testing.T) {
	for _, out := range []string{
		`[{"kind":"noqueue","handle":"0:","root":true}]`,
		`[{"kind":"cake","root":true,"tins":[{},{},{}]}]`, // diffserv3: not ours
		`[]`,
	} {
		if st, err := Parse([]byte(out)); st != nil || err != nil {
			t.Errorf("%s: got %v, %v", out, st, err)
		}
	}
	if _, err := Parse([]byte("garbage")); err == nil {
		t.Error("garbage accepted")
	}
}

func TestSamples(t *testing.T) {
	r := Reader{RunID: "S1-B2-s0007", Direction: "down"}
	first := &Stats{RateKbit: 95000, Tins: []TinStats{{}, {AvgDelayUs: 4800, BacklogBytes: 3028, Drops: 10}, {}, {}}}
	s := r.Samples(first, 61)
	if len(s) != 1+3*4 {
		t.Fatalf("%d samples", len(s))
	}
	find := func(metric, tin string) float64 {
		for _, x := range s {
			if x.Metric == metric && (x.Tin == nil && tin == "" || x.Tin != nil && string(*x.Tin) == tin) {
				if *x.Direction != "down" || x.Source != "cake" || x.TS != 61 && x.TS != 62 {
					t.Errorf("bad sample %+v", x)
				}
				return x.Value
			}
		}
		t.Fatalf("no %s %s", metric, tin)
		return 0
	}
	if find("shaper_rate_kbit", "") != 95000 || find("queue_delay_ms", "best_effort") != 4.8 ||
		find("queue_backlog_bytes", "best_effort") != 3028 {
		t.Errorf("wrong values: %+v", s)
	}
	// The first reading has nothing to take a difference from.
	if find("tin_drops", "best_effort") != 0 {
		t.Error("first reading reported drops")
	}
	second := &Stats{RateKbit: 95000, Tins: []TinStats{{}, {Drops: 17}, {}, {}}}
	s = r.Samples(second, 62)
	if find("tin_drops", "best_effort") != 7 {
		t.Error("drops are not per interval")
	}
	if r.Samples(nil, 63) != nil {
		t.Error("samples without cake")
	}
}
