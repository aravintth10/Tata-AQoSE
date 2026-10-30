// Command aqosd is the controller. It runs inside the router namespace,
// next to the ML service (aqos_ml.serve), on top of CAKE as configuration
// B2 sets it up:
//
//	aqosd -socket ml.sock -run-id ID -t0-ns N -until-s 200 -metrics metrics.jsonl -actions actions.jsonl
//
// The first eight packets of each flow become a feature record; the
// service classifies it and proposes a tin; the safety shield checks the
// proposal; the enforcer writes the mark. Which configuration this is
// (rules, top label or expected damage) is decided by how the service was
// started, not here.
//
// Stopping it, by signal or at -until-s, removes the marking: every flow is
// plain CAKE's again.
package main

import (
	"crypto/rand"
	"encoding/json"
	"flag"
	"fmt"
	"net"
	"net/http"
	"net/netip"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"adaptiveqos/internal/capacity"
	"adaptiveqos/internal/capture"
	"adaptiveqos/internal/controller"
	"adaptiveqos/internal/enforce"
	"adaptiveqos/internal/extract"
	"adaptiveqos/internal/intent"
	"adaptiveqos/internal/reclass"
	"adaptiveqos/internal/runclock"
	"adaptiveqos/internal/shield"
	"adaptiveqos/internal/verify"
)

func die(err error) {
	fmt.Fprintln(os.Stderr, "aqosd:", err)
	os.Exit(1)
}

func appendTo(path string) *os.File {
	if path == "" {
		return nil
	}
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_APPEND|os.O_CREATE, 0o644)
	if err != nil {
		die(err)
	}
	return file
}

func main() {
	socket := flag.String("socket", "", "Unix socket of the ML service")
	lan := flag.String("lan", "10.77.0.0/24,fd77:0:0:1::/64", "LAN prefixes, comma-separated")
	shieldPath := flag.String("shield", "configs/shield.yaml", "safety shield bounds")
	runID := flag.String("run-id", "", "run id written to every metric sample")
	t0 := flag.Int64("t0-ns", 0, "CLOCK_MONOTONIC time at which the run reads zero (default: now)")
	until := flag.Float64("until-s", 0, "run time to stop at (default: run until signalled)")
	metrics := flag.String("metrics", "", "metrics.jsonl to append to")
	actions := flag.String("actions", "", "action log to append to")
	summary := flag.String("summary", "", "file to write the final counts to, as JSON")
	beat := flag.String("heartbeat", "", "heartbeat file for the watchdog")
	group := flag.Int("group", 5, "nflog group")
	maxFlows := flag.Int("max-flows", 65536, "flows tracked at once; more stay unlabelled")
	maxClassify := flag.Int("max-classify", 200, "classifications per second; more stay unlabelled")
	deadline := flag.Duration("deadline", 50*time.Millisecond, "time allowed for one answer from the ML service")
	maxPending := flag.Int("max-pending", 1024, "requests allowed to wait for the ML service")
	windowEvery := flag.Int("window-every", 5, "seconds between a live flow's window records; 0 for none")
	confirm := flag.Int("confirm", 2, "window answers in a row needed to move a labelled flow")
	reflectors := flag.String("reflectors", "", "addresses of delay reflectors, comma-separated; empty for no capacity estimator")
	intentAddr := flag.String("intent", "", "address to serve the lease API and form on, such as 10.77.0.1:8377; empty for none")
	leasesPath := flag.String("leases", "", "file that keeps leases across restarts")
	envelopes := flag.String("envelopes", "", "envelopes.json of the model in use: labelled flows are then watched against their class's "+
		"normal range once a second and asked about again only when they leave it; empty asks about every flow every -window-every seconds")
	outside := flag.Int("outside-windows", 0, "outside checks in a row before a flow is asked about again; 1 switches the waiting off; 0 for the defaults (3, and 2 in Voice and Video)")
	verdicts := flag.String("verdicts", "", "the verifier's log; empty with -verify=false")
	verifying := flag.Bool("verify", true, "judge risky changes ten seconds on")
	deaf := flag.String("deaf", "hold", "what the capacity estimator does while its probes go unanswered: hold the rates where they are, or go to the minimum")
	rollback := flag.Bool("rollback", true, "undo a change the verifier found made things worse")
	flag.Parse()
	if *socket == "" {
		flag.Usage()
		os.Exit(2)
	}
	if *t0 == 0 {
		*t0 = runclock.NowNs()
	}
	clock := runclock.Clock{T0Ns: *t0}
	var prefixes []netip.Prefix
	for _, s := range strings.Split(*lan, ",") {
		prefix, err := netip.ParsePrefix(strings.TrimSpace(s))
		if err != nil {
			die(err)
		}
		prefixes = append(prefixes, prefix)
	}
	cfg, err := shield.LoadConfig(*shieldPath)
	if err != nil {
		die(err)
	}
	tracker, err := shield.NewTracker(cfg)
	if err != nil {
		die(err)
	}

	enforcer, err := enforce.New()
	if err != nil {
		die(err)
	}
	defer enforcer.Close()
	base := map[string]int{}
	for _, direction := range []string{"up", "down"} {
		rate, err := enforcer.Rate(direction)
		if err != nil {
			die(fmt.Errorf("CAKE must be set up first (configuration B2): %w", err))
		}
		tracker.RateRead(direction, rate)
		base[direction] = rate
	}

	// The service must be there at the start. If it goes away later, flows
	// simply stay unlabelled until it is back.
	client := controller.NewClient(*socket, *deadline, *maxPending)
	defer client.Close()
	health, err := client.Health(20 * time.Second)
	if err != nil {
		die(err)
	}
	fmt.Fprintf(os.Stderr, "aqosd: ML service is %s, deciding by %s\n", health.ModelVersion, health.DecisionRule)

	if err := enforcer.InstallMarking(); err != nil {
		die(err)
	}
	defer enforcer.RemoveMarking()
	if err := capture.InstallRule(*group); err != nil {
		enforcer.RemoveMarking()
		die(err)
	}
	defer capture.RemoveRule()

	config := controller.Config{
		MaxClassifyPerS: *maxClassify, Confirm: *confirm, Heartbeat: *beat, RunID: *runID, Clock: clock,
		Metrics: writer(appendTo(*metrics)), Actions: writer(appendTo(*actions)),
		Devices: map[string]string{"up": enforcer.WANDev, "down": enforcer.IFBDev},
	}
	every := *windowEvery
	if *envelopes != "" {
		ranges, err := reclass.Load(*envelopes)
		if err != nil {
			die(err)
		}
		settings := reclass.DefaultConfig()
		if *outside > 0 {
			settings.Windows, settings.Interactive = *outside, *outside
		}
		config.Reclass, config.AskEvery = reclass.NewWatcher(settings, ranges, time.Now().UnixNano()), max(*windowEvery, 1)
		every = 1 // the check needs every second; the controller decides which ones to ask about
	}
	if *verifying {
		settings := verify.DefaultConfig()
		config.Verify, config.Verdicts, config.Rollback = &settings, writer(appendTo(*verdicts)), *rollback
	}
	stop := make(chan struct{})
	if *reflectors != "" {
		// The rates CAKE has now are the configured ones: the estimator
		// never goes above them, and returns to them when the link allows.
		// The rate held while the reflectors cannot be heard is a tenth of
		// the configured one: low enough to be under any link that is still
		// working, high enough that a call survives it. The shield's own
		// minimum is far lower and is the floor for everything.
		floor := func(direction string) int { return max(cfg.MinRateKbit[direction], base[direction]/10) }
		settings := capacity.DefaultConfig(base["up"], base["down"], floor("up"), floor("down"))
		prober := capacity.Prober{Interval: 100 * time.Millisecond, Lost: settings.Silent}
		for _, address := range strings.Split(*reflectors, ",") {
			prober.Reflectors = append(prober.Reflectors, net.JoinHostPort(strings.TrimSpace(address), strconv.Itoa(capacity.Port)))
		}
		settings.Reflectors = len(prober.Reflectors)
		if *deaf != "hold" && *deaf != "minimum" {
			die(fmt.Errorf("-deaf is hold or minimum, not %q", *deaf))
		}
		settings.DeafHold = *deaf == "hold"
		// Just over the shield's own interval, so that it does not reject
		// a raise for arriving a moment early.
		settings.RaiseEvery = time.Duration(cfg.IncreaseIntervalMs+500) * time.Millisecond
		rounds := make(chan capacity.Round, 16)
		config.Estimator, config.Rounds, config.Shaper = capacity.New(settings), rounds, enforcer
		go func() {
			err := prober.Run(stop, func(round capacity.Round) {
				select {
				case rounds <- round:
				default: // the controller is behind; an old round is worth nothing
				}
			})
			if err != nil {
				fmt.Fprintln(os.Stderr, "aqosd: delay probes stopped:", err)
			}
		}()
	}
	key := make([]byte, 32)
	if _, err := rand.Read(key); err != nil {
		die(err)
	}
	var ctl *controller.Controller
	if *intentAddr != "" {
		hostID := extract.Hasher{Key: key}.HostID
		config.Leases = intent.NewStore(*leasesPath,
			func(addr netip.Addr) bool { return ctl != nil && ctl.HostSeen(hostID(addr)) }, hostID)
		server := &http.Server{Addr: *intentAddr, Handler: config.Leases.Handler(), ReadHeaderTimeout: 5 * time.Second}
		go func() {
			if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
				fmt.Fprintln(os.Stderr, "aqosd: lease API stopped:", err)
			}
		}()
		defer server.Close()
	}
	ctl = controller.New(config, client, tracker, enforcer)

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGTERM, syscall.SIGINT)
	go func() {
		if *until > 0 {
			select {
			case <-sig:
			case <-time.After(time.Duration((*until - clock.NowS()) * float64(time.Second))):
			}
		} else {
			<-sig
		}
		close(stop)
	}()
	go ctl.Run(stop)

	live := capture.Live{
		Group: *group, LAN: prefixes, Key: key, Max: *maxFlows, WindowEvery: every,
		OnRecord: ctl.OnRecord, OnWindow: ctl.OnWindow, OnGone: ctl.OnGone, OnSecond: ctl.OnSecond,
	}
	stats, err := live.Run(stop)
	fmt.Fprintf(os.Stderr, "aqosd: %s; extractor read %d packets, %d overflows, %d flows untracked\n",
		ctl.Summary(), stats.Packets, stats.Overflows, stats.Untracked)
	if *summary != "" {
		// What the controller itself cost: CPU seconds over the whole run.
		var usage syscall.Rusage
		syscall.Getrusage(syscall.RUSAGE_SELF, &usage)
		cpu := float64(usage.Utime.Nano()+usage.Stime.Nano()) / 1e9
		out, _ := json.MarshalIndent(map[string]any{
			"model_version": health.ModelVersion, "decision_rule": health.DecisionRule, "cpu_s": cpu,
			"flows": ctl.Counts(), "shield": tracker.Counts(),
			"extractor": map[string]uint64{"packets": stats.Packets, "overflows": stats.Overflows, "untracked": stats.Untracked},
		}, "", "  ")
		os.WriteFile(*summary, append(out, '\n'), 0o644)
	}
	if err != nil {
		capture.RemoveRule()
		enforcer.RemoveMarking()
		die(err)
	}
}

// writer turns a nil file into a nil io.Writer, which the controller
// takes to mean "do not write".
func writer(file *os.File) interface{ Write([]byte) (int, error) } {
	if file == nil {
		return nil
	}
	return file
}
