// Command aqos-ctl drives the enforcer by hand, inside the router
// namespace. Tests and demonstrations use it; the controller uses the
// enforcer directly.
package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/netip"
	"os"
	"strconv"
	"time"

	"adaptiveqos/contracts"
	"adaptiveqos/internal/cake"
	"adaptiveqos/internal/enforce"
	"adaptiveqos/internal/heartbeat"
	"adaptiveqos/internal/netfilter"
)

const usage = `usage (inside the router namespace):
  aqos-ctl marking install|remove|status
  aqos-ctl rate up|down [kbit]                    read, or set and read back
  aqos-ctl label tcp|udp <src ip:port> <dst ip:port> [bulk|best_effort|video|voice|none]
  aqos-ctl tins                                   packets sent per tin and direction, as JSON
  aqos-ctl flows                                  conntrack entries with their labels
  aqos-ctl heartbeat <file>                       beat until killed, as the controller does

exit status: 0 done, 3 the flow has ended, 4 read-back mismatch (previous state restored)`

func main() {
	if len(os.Args) < 2 {
		die(2, usage)
	}
	args := os.Args[2:]
	if os.Args[1] == "heartbeat" && len(args) == 1 {
		for {
			check(heartbeat.Beat(args[0]))
			time.Sleep(heartbeat.Interval)
		}
	}
	e, err := enforce.New()
	check(err)
	defer e.Close()
	// AQOS_ENFORCE_FAULT makes the next change go wrong after it is
	// applied, to show that the read-back catches it.
	switch os.Getenv("AQOS_ENFORCE_FAULT") {
	case "rate":
		e.DisturbAfterApply(func() {
			for _, dev := range []string{e.WANDev, e.IFBDev} {
				fault("tc", "qdisc", "change", "dev", dev, "root", "cake", "bandwidth", "1234kbit")
			}
		})
	case "label":
		ct, err := netfilter.OpenConntrack()
		check(err)
		e.DisturbAfterApply(func() {
			// Collect first: a dump in progress owns the socket.
			var entries []netfilter.Entry
			ct.Dump(func(entry netfilter.Entry) { entries = append(entries, entry) })
			for _, entry := range entries {
				ct.SetMark(entry.Protocol, entry.Src, entry.Dst, 1, enforce.MarkMask)
			}
		})
	}

	switch {
	case os.Args[1] == "marking" && len(args) == 1:
		switch args[0] {
		case "install":
			check(e.InstallMarking())
		case "remove":
			check(e.RemoveMarking())
		case "status":
		default:
			die(2, usage)
		}
		ok, err := e.MarkingInstalled()
		check(err)
		fmt.Println(map[bool]string{true: "installed", false: "not installed"}[ok])
	case os.Args[1] == "rate" && (len(args) == 1 || len(args) == 2):
		if len(args) == 2 {
			kbit, err := strconv.Atoi(args[1])
			check(err)
			check(e.SetRate(args[0], kbit))
		}
		kbit, err := e.Rate(args[0])
		check(err)
		fmt.Println(kbit)
	case os.Args[1] == "label" && (len(args) == 3 || len(args) == 4):
		flow := enforce.Flow{Protocol: map[string]uint8{"tcp": 6, "udp": 17}[args[0]]}
		if flow.Protocol == 0 {
			die(2, usage)
		}
		flow.Src, err = netip.ParseAddrPort(args[1])
		check(err)
		flow.Dst, err = netip.ParseAddrPort(args[2])
		check(err)
		if len(args) == 4 {
			tin := contracts.Tin(args[3])
			if tin == "none" {
				tin = ""
			}
			check(e.SetLabel(flow, tin))
		}
		tin, ok, err := e.Label(flow)
		check(err)
		if !ok {
			tin = "none"
		}
		fmt.Println(tin)
	case os.Args[1] == "tins":
		out := map[string]map[contracts.Tin]float64{}
		for direction, dev := range map[string]string{"up": e.WANDev, "down": e.IFBDev} {
			stats, err := cake.Read(dev)
			check(err)
			if stats == nil {
				die(1, "aqos-ctl: "+dev+" has no CAKE in diffserv4 mode")
			}
			out[direction] = map[contracts.Tin]float64{}
			for i, tin := range cake.Tins {
				out[direction][tin] = stats.Tins[i].SentPackets
			}
		}
		line, _ := json.Marshal(out)
		fmt.Println(string(line))
	case os.Args[1] == "flows":
		ct, err := netfilter.OpenConntrack()
		check(err)
		check(ct.Dump(func(entry netfilter.Entry) {
			tin, ok := enforce.MarkTin(entry.Mark)
			if !ok {
				tin = "none"
			}
			fmt.Printf("%-4s %-28s -> %-28s %-11s up %d pkts, down %d pkts\n",
				map[uint8]string{6: "tcp", 17: "udp"}[entry.Protocol], entry.Src, entry.Dst, tin, entry.OrigPkts, entry.ReplyPkts)
		}))
	default:
		die(2, usage)
	}
}

func fault(name string, args ...string) {
	check(runQuiet(name, args...))
}

func check(err error) {
	switch {
	case err == nil:
	case errors.Is(err, enforce.ErrFlowGone):
		die(3, "aqos-ctl: the flow has ended")
	case errors.Is(err, enforce.ErrMismatch):
		die(4, "aqos-ctl: "+err.Error())
	default:
		die(1, "aqos-ctl: "+err.Error())
	}
}

func die(code int, msg string) {
	fmt.Fprintln(os.Stderr, msg)
	os.Exit(code)
}
