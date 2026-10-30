// Package cake reads CAKE's per-tin statistics from tc and turns them into
// metric samples.
package cake

import (
	"encoding/json"
	"fmt"
	"os/exec"

	"adaptiveqos/contracts"
)

// Tins is the order tc reports diffserv4's tins in.
var Tins = []contracts.Tin{contracts.TinBulk, contracts.TinBestEffort, contracts.TinVideo, contracts.TinVoice}

// Stats is one CAKE instance at one moment.
type Stats struct {
	RateKbit float64
	Tins     []TinStats
}

type TinStats struct {
	AvgDelayUs   float64 `json:"avg_delay_us"`
	PeakDelayUs  float64 `json:"peak_delay_us"`
	BacklogBytes float64 `json:"backlog_bytes"`
	Drops        float64 `json:"drops"`
	SentBytes    float64 `json:"sent_bytes"`
	SentPackets  float64 `json:"sent_packets"`
}

// Parse reads the output of `tc -j -s qdisc show dev <dev>`. It returns nil
// when the device's root qdisc is not CAKE in diffserv4 mode, as in B0.
func Parse(out []byte) (*Stats, error) {
	var qdiscs []struct {
		Kind    string `json:"kind"`
		Root    bool   `json:"root"`
		Options struct {
			// Bytes per second, whatever unit the rate was set in.
			Bandwidth float64 `json:"bandwidth"`
		} `json:"options"`
		Tins []TinStats `json:"tins"`
	}
	if err := json.Unmarshal(out, &qdiscs); err != nil {
		return nil, fmt.Errorf("tc output: %w", err)
	}
	for _, q := range qdiscs {
		if q.Kind == "cake" && q.Root && len(q.Tins) == len(Tins) {
			return &Stats{RateKbit: q.Options.Bandwidth * 8 / 1000, Tins: q.Tins}, nil
		}
	}
	return nil, nil
}

// Read runs tc for one device in the current namespace.
func Read(dev string) (*Stats, error) {
	out, err := exec.Command("tc", "-j", "-s", "qdisc", "show", "dev", dev).Output()
	if err != nil {
		return nil, fmt.Errorf("tc qdisc show dev %s: %w", dev, err)
	}
	return Parse(out)
}

// Reader turns successive readings of one device into samples. Drops are
// reported per interval, the rest as they stand.
type Reader struct {
	RunID     string
	Direction string
	prev      *Stats
}

func (r *Reader) Samples(now *Stats, tS float64) []contracts.MetricSample {
	if now == nil {
		r.prev = nil
		return nil
	}
	dir := r.Direction
	sample := func(metric string, value float64, tin *contracts.Tin) contracts.MetricSample {
		return contracts.MetricSample{RunID: r.RunID, TS: tS, Source: "cake", Metric: metric,
			Value: value, Tin: tin, Direction: &dir}
	}
	out := []contracts.MetricSample{sample("shaper_rate_kbit", now.RateKbit, nil)}
	for i := range now.Tins {
		tin := Tins[i]
		drops := 0.0
		// A counter that went backwards means the qdisc was replaced.
		if r.prev != nil && now.Tins[i].Drops >= r.prev.Tins[i].Drops {
			drops = now.Tins[i].Drops - r.prev.Tins[i].Drops
		}
		out = append(out,
			sample("queue_delay_ms", now.Tins[i].AvgDelayUs/1000, &tin),
			sample("queue_backlog_bytes", now.Tins[i].BacklogBytes, &tin),
			sample("tin_drops", drops, &tin))
	}
	r.prev = now
	return out
}
