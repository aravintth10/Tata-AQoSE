// Command aqos-core-bench is the Go reference for the matched Rust experiment.
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"strconv"
	"time"

	"adaptiveqos/contracts"
	"adaptiveqos/internal/classify"
)

func run() error {
	if len(os.Args) != 6 {
		return fmt.Errorf("usage: aqos-core-bench MODEL CORPUS DAMAGE ROUNDS infer|pipeline")
	}
	model, err := classify.Load(os.Args[1])
	if err != nil {
		return err
	}
	data, err := os.ReadFile(os.Args[2])
	if err != nil {
		return err
	}
	var corpus struct {
		Cases []struct {
			Record contracts.FeatureRecord `json:"record"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(data, &corpus); err != nil {
		return err
	}
	damage, err := classify.LoadDamage(os.Args[3])
	if err != nil {
		return err
	}
	engine, err := classify.NewEngine(damage, false)
	if err != nil {
		return err
	}
	rounds, err := strconv.Atoi(os.Args[4])
	if err != nil {
		return err
	}
	if rounds < 1 || len(corpus.Cases) == 0 || (os.Args[5] != "infer" && os.Args[5] != "pipeline") {
		return fmt.Errorf("invalid benchmark settings")
	}
	vectors := make([][classify.Features]float64, len(corpus.Cases))
	for i := range corpus.Cases {
		vectors[i] = classify.Vector(&corpus.Cases[i].Record)
	}
	pipeline := os.Args[5] == "pipeline"
	index := map[contracts.Tin]int{contracts.TinVoice: 0, contracts.TinVideo: 1, contracts.TinBestEffort: 2, contracts.TinBulk: 3}
	checksum := 0.0
	started := time.Now()
	for range rounds {
		for i := range corpus.Cases {
			x := vectors[i]
			if pipeline {
				x = classify.Vector(&corpus.Cases[i].Record)
			}
			p := model.Probabilities(&x)
			checksum += p[0]
			if pipeline {
				c, err := engine.Choose(p, nil, &corpus.Cases[i].Record)
				if err != nil {
					return err
				}
				checksum += float64(index[c.Tin]) + c.Expected[c.Tin]
			}
		}
	}
	elapsed := time.Since(started).Nanoseconds()
	operations := rounds * len(corpus.Cases)
	return json.NewEncoder(os.Stdout).Encode(map[string]any{"backend": "go", "mode": os.Args[5], "operations": operations,
		"elapsed_ns": elapsed, "ns_per_op": float64(elapsed) / float64(operations), "checksum": checksum})
}
func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
