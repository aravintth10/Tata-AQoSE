package contracts

import (
	"bufio"
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func decodeStrict(t *testing.T, data []byte, v any) {
	t.Helper()
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		t.Fatalf("decode: %v", err)
	}
}

func readExample(t *testing.T, name string) []byte {
	t.Helper()
	data, err := os.ReadFile(filepath.Join("examples", name))
	if err != nil {
		t.Fatal(err)
	}
	return data
}

func TestExamplesDecode(t *testing.T) {
	cases := []struct {
		file string
		into any
	}{
		{"feature_record.initial.json", &FeatureRecord{}},
		{"feature_record.window.json", &FeatureRecord{}},
		{"classification.json", &Classification{}},
		{"policy_proposal.flow_label.json", &PolicyProposal{}},
		{"policy_proposal.shaper_rate.json", &PolicyProposal{}},
		{"policy_verdict.clamp.json", &PolicyVerdict{}},
		{"policy_verdict.reject.json", &PolicyVerdict{}},
		{"run_manifest.json", &RunManifest{}},
		{"service_request.classify.json", &ServiceRequest{}},
		{"service_request.health.json", &ServiceRequest{}},
		{"service_response.classify.json", &ServiceResponse{}},
		{"service_response.health.json", &ServiceResponse{}},
		{"service_response.error.json", &ServiceResponse{}},
	}
	for _, c := range cases {
		t.Run(c.file, func(t *testing.T) {
			decodeStrict(t, readExample(t, c.file), c.into)
		})
	}
}

func TestMetricSamplesDecode(t *testing.T) {
	sc := bufio.NewScanner(bytes.NewReader(readExample(t, "metric_samples.jsonl")))
	n := 0
	for sc.Scan() {
		var s MetricSample
		decodeStrict(t, sc.Bytes(), &s)
		n++
	}
	if n == 0 {
		t.Fatal("no samples")
	}
}

func TestScenarioDecodes(t *testing.T) {
	s, err := LoadScenario(filepath.Join("examples", "scenario.S1.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	if s.ID != "S1" || len(s.Hosts) != 2 || len(s.Traffic) != 2 {
		t.Errorf("unexpected scenario: %+v", s)
	}
	if s.Traffic[0].StopS != nil {
		t.Error("stop_s: null should decode to nil")
	}
}

func TestFeatureRecordWindow(t *testing.T) {
	var initial, window FeatureRecord
	decodeStrict(t, readExample(t, "feature_record.initial.json"), &initial)
	decodeStrict(t, readExample(t, "feature_record.window.json"), &window)
	if initial.Window != nil {
		t.Error("initial record: window should be nil")
	}
	if window.Window == nil {
		t.Error("window record: window should be set")
	}
}

func TestProposalHasOnePayload(t *testing.T) {
	for _, f := range []string{"policy_proposal.flow_label.json", "policy_proposal.shaper_rate.json"} {
		var p PolicyProposal
		decodeStrict(t, readExample(t, f), &p)
		if (p.FlowLabel != nil) == (p.ShaperRate != nil) {
			t.Errorf("%s: want exactly one of flow_label and shaper_rate", f)
		}
	}
}

func TestTaxonomy(t *testing.T) {
	tax, err := LoadTaxonomy()
	if err != nil {
		t.Fatal(err)
	}
	classes := []Class{Telephony, RealtimeInteractive, MultimediaConferencing,
		MultimediaStreaming, Standard, HighThroughputData, LowPriorityData}
	if len(tax.Classes) != len(classes) {
		t.Errorf("got %d classes, want %d", len(tax.Classes), len(classes))
	}
	for _, c := range classes {
		entry, ok := tax.Classes[c]
		if !ok {
			t.Errorf("class %q missing", c)
			continue
		}
		if tax.DSCP[entry.DSCP].Tin != entry.Tin {
			t.Errorf("class %q: dscp %s does not map to tin %s", c, entry.DSCP, entry.Tin)
		}
	}
	tins := []Tin{TinVoice, TinVideo, TinBestEffort, TinBulk}
	if len(tax.Tins) != len(tins) {
		t.Errorf("got %d tins, want %d", len(tax.Tins), len(tins))
	}
	for _, tin := range tins {
		entry, ok := tax.Tins[tin]
		if !ok {
			t.Errorf("tin %q missing", tin)
			continue
		}
		if tax.DSCP[entry.DSCP].Tin != tin {
			t.Errorf("tin %q: default dscp %s maps elsewhere", tin, entry.DSCP)
		}
	}
	for profile, class := range tax.Profiles {
		if _, ok := tax.Classes[class]; !ok {
			t.Errorf("profile %q: unknown class %q", profile, class)
		}
	}
	if tax.Classes[tax.DefaultClass].Tin != TinBestEffort {
		t.Error("default class must land in the best_effort tin")
	}
}
