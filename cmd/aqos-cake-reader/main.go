// Command aqos-cake-reader writes CAKE's statistics to a run's
// metrics.jsonl once per second. It runs inside the router namespace, for
// the configurations that have CAKE but no controller to do this.
package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"syscall"

	"adaptiveqos/internal/cake"
	"adaptiveqos/internal/runclock"
)

func main() {
	runID := flag.String("run-id", "", "run id written to every sample")
	t0 := flag.Int64("t0-ns", 0, "CLOCK_MONOTONIC time at which the run reads zero")
	until := flag.Float64("until-s", 0, "run time to stop at")
	out := flag.String("out", "", "metrics.jsonl to append to")
	up := flag.String("up", "wan0", "device shaping the upload")
	down := flag.String("down", "ifb0", "device shaping the download")
	flag.Parse()
	if *runID == "" || *t0 == 0 || *until <= 0 || *out == "" {
		flag.Usage()
		os.Exit(2)
	}
	file, err := os.OpenFile(*out, os.O_WRONLY|os.O_APPEND|os.O_CREATE, 0o644)
	if err != nil {
		fmt.Fprintln(os.Stderr, "aqos-cake-reader:", err)
		os.Exit(1)
	}
	defer file.Close()

	stop := make(chan struct{})
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGTERM, syscall.SIGINT)
	go func() { <-sig; close(stop) }()

	clock := runclock.Clock{T0Ns: *t0}
	readers := map[string]*cake.Reader{
		*up:   {RunID: *runID, Direction: "up"},
		*down: {RunID: *runID, Direction: "down"},
	}
	for second := 1.0; second <= *until; second++ {
		if clock.NowS() > second {
			continue // started late: do not write samples for the past
		}
		if !clock.SleepUntil(second, stop) {
			return
		}
		// One write per second, so lines from other processes appending to
		// the same file cannot land inside ours.
		var batch bytes.Buffer
		for dev, reader := range readers {
			stats, err := cake.Read(dev)
			if err != nil {
				continue // the device does not exist yet, or not at all
			}
			for _, sample := range reader.Samples(stats, second) {
				line, _ := json.Marshal(sample)
				batch.Write(line)
				batch.WriteByte('\n')
			}
		}
		file.Write(batch.Bytes())
	}
}
