// Command aqos-exporter serves the current run's metrics.jsonl to
// Prometheus as aqos_<metric> series.
package main

import (
	"flag"
	"log"
	"net/http"
	"time"

	"adaptiveqos/internal/exporter"
)

func main() {
	file := flag.String("file", "results/current/metrics.jsonl", "metrics file to follow; the runner points results/current at the run in progress")
	listen := flag.String("listen", ":9108", "address to serve /metrics on")
	flag.Parse()

	store := exporter.NewStore()
	tail := &exporter.Tail{Path: *file, Store: store}
	go func() {
		for {
			tail.Poll()
			time.Sleep(250 * time.Millisecond)
		}
	}()
	http.HandleFunc("/metrics", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain; version=0.0.4")
		store.Render(w)
	})
	log.Printf("following %s, serving http://%s/metrics", *file, *listen)
	log.Fatal(http.ListenAndServe(*listen, nil))
}
