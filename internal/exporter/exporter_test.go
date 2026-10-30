package exporter

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func render(s *Store) string {
	var b strings.Builder
	s.Render(&b)
	return b.String()
}

func TestStore(t *testing.T) {
	s := NewStore()
	lines, err := os.ReadFile("../../contracts/examples/metric_samples.jsonl")
	if err != nil {
		t.Fatal(err)
	}
	for _, line := range strings.Split(strings.TrimSpace(string(lines)), "\n") {
		s.Add([]byte(line))
	}
	s.Add([]byte(`{"run_id":"S1-B2-s0007","t_s":63,"source":"cake","metric":"queue_delay_ms","value":2.5,"tin":"best_effort","direction":"down"}`))
	s.Add([]byte(`{"half a line`))
	out := render(s)
	for _, want := range []string{
		`aqos_run_info{run_id="S1-B2-s0007"} 1`,
		`aqos_throughput_kbit{host="desktop",traffic_id="download",profile="bulk_download",true_class="high_throughput_data",direction="down"} 91250`,
		// The later sample replaces the earlier one.
		`aqos_queue_delay_ms{tin="best_effort",direction="down"} 2.5`,
		`aqos_cpu_percent{component="cake"} 3.1`,
		"# TYPE aqos_latency_ms histogram",
		// 23.4 ms falls in the 30 ms bucket and every larger one.
		`direction="up",le="20"} 0`,
		`direction="up",le="30"} 1`,
		`direction="up",le="+Inf"} 1`,
		`aqos_latency_ms_sum{host="laptop",traffic_id="call",profile="video_call",true_class="multimedia_conferencing",direction="up"} 23.4`,
	} {
		if !strings.Contains(out, want) {
			t.Errorf("missing %q in:\n%s", want, out)
		}
	}
	if strings.Contains(out, "aqos_latency_ms{") {
		t.Error("latency must be a histogram only")
	}
}

func TestTailFollowsAndRestarts(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "metrics.jsonl")
	s := NewStore()
	tail := &Tail{Path: path, Store: s}
	tail.Poll() // no file yet: no panic

	sample := func(run string, v string) string {
		return `{"run_id":"` + run + `","t_s":1,"source":"cake","metric":"shaper_rate_kbit","value":` + v + `,"direction":"up"}` + "\n"
	}
	appendTo := func(text string) {
		f, _ := os.OpenFile(path, os.O_WRONLY|os.O_APPEND|os.O_CREATE, 0o644)
		f.WriteString(text)
		f.Close()
	}
	whole := sample("run1", "19000")
	appendTo(whole[:40])
	tail.Poll()
	if strings.Contains(render(s), "shaper_rate") {
		t.Error("half a line was taken as a sample")
	}
	appendTo(whole[40:])
	tail.Poll()
	if !strings.Contains(render(s), `aqos_shaper_rate_kbit{direction="up"} 19000`) {
		t.Errorf("line completed later was lost:\n%s", render(s))
	}

	// A new run writes a new file at the same path.
	os.Remove(path)
	appendTo(sample("run2", "9500"))
	tail.Poll()
	out := render(s)
	if !strings.Contains(out, `run_id="run2"`) || !strings.Contains(out, "} 9500") || strings.Contains(out, "19000") {
		t.Errorf("did not start over on the new file:\n%s", out)
	}
}
