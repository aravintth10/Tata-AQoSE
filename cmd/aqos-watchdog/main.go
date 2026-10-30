// Command aqos-watchdog returns the router to plain CAKE when the
// controller stops. It is deliberately tiny: the standard library, the tc
// and nft programs, and no code shared with the controller, so a fault in
// the controller cannot take it down too.
//
// The controller touches a heartbeat file four times a second. If the file
// is older than the timeout, the watchdog removes marking and sets both
// CAKE instances to fixed conservative rates. Marks stay in conntrack and
// are simply no longer read. When the heartbeat returns the watchdog does
// nothing: the controller reinstalls marking itself when it starts.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"os"
	"os/exec"
	"os/signal"
	"strings"
	"syscall"
	"time"
)

// Config is written by whoever starts the controller, before it starts.
type Config struct {
	Heartbeat string `json:"heartbeat"`
	// TimeoutMs of silence triggers the safe state. Detection takes up to
	// one check interval more, and applying takes a few hundred
	// milliseconds, inside the three seconds the PRD allows end to end.
	TimeoutMs int    `json:"timeout_ms"`
	WANDev    string `json:"wan_dev"`
	IFBDev    string `json:"ifb_dev"`
	// The fixed rates of the safe state, in kbit/s.
	SafeUpKbit   int `json:"safe_up_kbit"`
	SafeDownKbit int `json:"safe_down_kbit"`
}

const checkEvery = 250 * time.Millisecond

func main() {
	path := flag.String("config", "", "configuration file (JSON)")
	flag.Parse()
	data, err := os.ReadFile(*path)
	if err != nil {
		log.Fatalf("aqos-watchdog: %v", err)
	}
	cfg := Config{TimeoutMs: 2000, WANDev: "wan0", IFBDev: "ifb0"}
	if err := json.Unmarshal(data, &cfg); err != nil {
		log.Fatalf("aqos-watchdog: %s: %v", *path, err)
	}
	if cfg.Heartbeat == "" || cfg.SafeUpKbit < 1 || cfg.SafeDownKbit < 1 {
		log.Fatalf("aqos-watchdog: %s needs heartbeat, safe_up_kbit and safe_down_kbit", *path)
	}
	timeout := time.Duration(cfg.TimeoutMs) * time.Millisecond
	log.Printf("watching %s, timeout %v, safe rates %d kbit up and %d kbit down",
		cfg.Heartbeat, timeout, cfg.SafeUpKbit, cfg.SafeDownKbit)

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGTERM, syscall.SIGINT)
	ticker := time.NewTicker(checkEvery)
	seen, safe := false, false
	for {
		select {
		case <-stop:
			return
		case <-ticker.C:
		}
		info, err := os.Stat(cfg.Heartbeat)
		if err != nil {
			// No heartbeat yet means no controller yet. Once there has
			// been one, a missing file is a dead controller.
			if !seen || safe {
				continue
			}
		} else {
			seen = true
			if time.Since(info.ModTime()) <= timeout {
				safe = false
				continue
			}
			if safe {
				continue
			}
		}
		started := time.Now()
		problems := makeSafe(cfg)
		safe = true
		log.Printf("controller silent for more than %v: safe state applied in %v %s",
			timeout, time.Since(started).Round(time.Millisecond), strings.Join(problems, "; "))
		// For whoever needs to know when: tests, and the dashboard later.
		os.WriteFile(cfg.Heartbeat+".safe", []byte(fmt.Sprintf("%d\n", time.Now().UnixNano())), 0o644)
	}
}

// makeSafe applies the safe state and returns what went wrong, if anything.
// Each step is tried whatever happened to the one before.
func makeSafe(cfg Config) []string {
	var problems []string
	step := func(must bool, name string, args ...string) {
		out, err := exec.Command(name, args...).CombinedOutput()
		if err != nil && must {
			problems = append(problems, fmt.Sprintf("%s %s: %s", name, strings.Join(args, " "), strings.TrimSpace(string(out))))
		}
	}
	rate := func(kbit int) string { return fmt.Sprintf("%dkbit", kbit) }
	// Stop copying labels to packets, in both directions.
	step(false, "nft", "delete", "table", "inet", "aqos_mark")
	step(false, "tc", "filter", "del", "dev", cfg.WANDev, "parent", "ffff:", "prio", "10")
	step(true, "tc", "filter", "add", "dev", cfg.WANDev, "parent", "ffff:", "protocol", "all", "prio", "10",
		"matchall", "action", "mirred", "egress", "redirect", "dev", cfg.IFBDev)
	// Fixed rates, and no fwmark: every packet's tin comes from its DSCP.
	step(true, "tc", "qdisc", "replace", "dev", cfg.WANDev, "root", "cake",
		"bandwidth", rate(cfg.SafeUpKbit), "diffserv4", "dual-srchost", "nat", "fwmark", "0")
	step(true, "tc", "qdisc", "replace", "dev", cfg.IFBDev, "root", "cake",
		"bandwidth", rate(cfg.SafeDownKbit), "diffserv4", "dual-dsthost", "nat", "ingress", "fwmark", "0")
	return problems
}
