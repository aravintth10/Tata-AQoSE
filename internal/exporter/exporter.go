// Package exporter serves a run's metrics.jsonl to Prometheus. Every source
// already writes to that one file, so the dashboard and the report read the
// same data.
package exporter

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"sort"
	"strings"
	"sync"
)

// Buckets for latency_ms, the one metric exposed as a histogram.
var Buckets = []float64{1, 2, 5, 10, 20, 30, 50, 75, 100, 150, 200, 300, 500, 1000, 2000}

var labelOrder = []string{"host", "traffic_id", "profile", "true_class", "tin", "direction", "component"}

type histogram struct {
	counts []uint64 // one per bucket, then +Inf
	sum    float64
}

// Store holds the latest value of every series of the current run.
type Store struct {
	mu     sync.Mutex
	runID  string
	gauges map[string]map[string]float64 // metric -> label set -> value
	hists  map[string]*histogram         // label set -> latency histogram
	lines  uint64
}

func NewStore() *Store {
	s := &Store{}
	s.Reset()
	return s
}

// Reset forgets every series, for when a new run's file replaces the old.
func (s *Store) Reset() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.runID, s.lines = "", 0
	s.gauges = map[string]map[string]float64{}
	s.hists = map[string]*histogram{}
}

// Add takes one line of metrics.jsonl. Lines that are not metric samples
// are ignored: a half-written last line is normal while a run is going.
func (s *Store) Add(line []byte) {
	var sample map[string]any
	if json.Unmarshal(line, &sample) != nil {
		return
	}
	metric, _ := sample["metric"].(string)
	value, ok := sample["value"].(float64)
	if metric == "" || !ok {
		return
	}
	var labels []string
	for _, name := range labelOrder {
		if v, ok := sample[name].(string); ok {
			labels = append(labels, fmt.Sprintf(`%s="%s"`, name, escape(v)))
		}
	}
	key := strings.Join(labels, ",")

	s.mu.Lock()
	defer s.mu.Unlock()
	s.lines++
	if id, ok := sample["run_id"].(string); ok {
		s.runID = id
	}
	if metric == "latency_ms" {
		h := s.hists[key]
		if h == nil {
			h = &histogram{counts: make([]uint64, len(Buckets)+1)}
			s.hists[key] = h
		}
		h.sum += value
		h.counts[sort.SearchFloat64s(Buckets, value)]++
		return
	}
	if s.gauges[metric] == nil {
		s.gauges[metric] = map[string]float64{}
	}
	s.gauges[metric][key] = value
}

func escape(v string) string {
	return strings.NewReplacer(`\`, `\\`, `"`, `\"`, "\n", `\n`).Replace(v)
}

// Render writes the Prometheus text format, in a stable order.
func (s *Store) Render(w io.Writer) {
	s.mu.Lock()
	defer s.mu.Unlock()
	fmt.Fprintf(w, "# TYPE aqos_run_info gauge\naqos_run_info{run_id=\"%s\"} 1\n", escape(s.runID))
	fmt.Fprintf(w, "# TYPE aqos_samples_read counter\naqos_samples_read %d\n", s.lines)

	metrics := make([]string, 0, len(s.gauges))
	for m := range s.gauges {
		metrics = append(metrics, m)
	}
	sort.Strings(metrics)
	for _, m := range metrics {
		fmt.Fprintf(w, "# TYPE aqos_%s gauge\n", m)
		for _, key := range sortedKeys(s.gauges[m]) {
			fmt.Fprintf(w, "aqos_%s%s %g\n", m, braces(key), s.gauges[m][key])
		}
	}
	if len(s.hists) == 0 {
		return
	}
	fmt.Fprintln(w, "# TYPE aqos_latency_ms histogram")
	for _, key := range sortedKeys(s.hists) {
		h := s.hists[key]
		var total uint64
		for i, bound := range Buckets {
			total += h.counts[i]
			fmt.Fprintf(w, "aqos_latency_ms_bucket%s %d\n", braces(join(key, fmt.Sprintf(`le="%g"`, bound))), total)
		}
		total += h.counts[len(Buckets)]
		fmt.Fprintf(w, "aqos_latency_ms_bucket%s %d\n", braces(join(key, `le="+Inf"`)), total)
		fmt.Fprintf(w, "aqos_latency_ms_sum%s %g\n", braces(key), h.sum)
		fmt.Fprintf(w, "aqos_latency_ms_count%s %d\n", braces(key), total)
	}
}

func sortedKeys[V any](m map[string]V) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

func braces(labels string) string {
	if labels == "" {
		return ""
	}
	return "{" + labels + "}"
}

func join(a, b string) string {
	if a == "" {
		return b
	}
	return a + "," + b
}

// Tail feeds a growing file into a Store, and starts over when the path
// points at a different file, which is how a new run is noticed.
type Tail struct {
	Path    string
	Store   *Store
	file    *os.File
	reader  *bufio.Reader
	info    os.FileInfo
	partial []byte
}

// Poll reads whatever was appended since the last call.
func (t *Tail) Poll() {
	info, err := os.Stat(t.Path)
	if err != nil {
		return // between runs the file does not exist; keep the last values
	}
	if t.file == nil || !os.SameFile(info, t.info) || info.Size() < t.info.Size() {
		if t.file != nil {
			t.file.Close()
		}
		file, err := os.Open(t.Path)
		if err != nil {
			return
		}
		t.file, t.reader, t.partial = file, bufio.NewReaderSize(file, 1<<20), nil
		t.Store.Reset()
	}
	t.info = info
	for {
		chunk, err := t.reader.ReadBytes('\n')
		if err != nil {
			// No newline yet: keep the piece until the rest is written.
			t.partial = append(t.partial, chunk...)
			return
		}
		t.Store.Add(append(t.partial, chunk...))
		t.partial = nil
	}
}
