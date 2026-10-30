// Command aqos-extract turns packets into feature records.
//
//	aqos-extract -pcap capture.pcap -out features.jsonl [-keys keys.jsonl]
//	aqos-extract -live -out features.jsonl [-dump first8.pcap] [-until-s 30]
//	aqos-extract -flow-table flows.jsonl.gz -out rows.jsonl [-window-every 5]
//
// The first form reads a capture. The second runs inside the router
// namespace and reads the first eight packets of each flow from the kernel.
// The third reads the per-flow tables the Kaggle script makes from public
// datasets. All three go through the same flow table.
package main

import (
	"crypto/rand"
	"encoding/hex"
	"flag"
	"fmt"
	"net/netip"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"adaptiveqos/internal/capture"
	"adaptiveqos/internal/extract"
)

func main() {
	pcap := flag.String("pcap", "", "capture to read (pcap or pcapng; headers are enough)")
	out := flag.String("out", "features.jsonl", "feature records, one per line")
	keys := flag.String("keys", "", "optional file mapping each flow_id to its tuple, for joining labels; holds addresses")
	lan := flag.String("lan", "", "comma-separated LAN prefixes; default: the private ranges")
	every := flag.Int("window-every", 0, "also emit a window record per active flow every N seconds")
	keyHex := flag.String("key", "", "hash key in hex; default: random, so ids differ from run to run")
	max := flag.Int("max-flows", 1<<20, "flows tracked at once")
	live := flag.Bool("live", false, "read from the kernel in this namespace instead of a capture")
	group := flag.Int("group", 5, "live: nflog group")
	install := flag.Bool("install-rule", true, "live: add the nftables rule that feeds the group, and remove it on exit")
	dump := flag.String("dump", "", "live: also save every packet received, as a capture")
	until := flag.Float64("until-s", 0, "live: stop after this many seconds; default: run until interrupted")
	flowTable := flag.String("flow-table", "", "read per-flow tables (JSON lines, gzip or not) instead of a capture")
	flag.Parse()
	if *pcap == "" && !*live && *flowTable == "" {
		flag.Usage()
		os.Exit(2)
	}
	if *flowTable != "" {
		outFile, err := os.Create(*out)
		check(err)
		defer outFile.Close()
		stats, err := extract.FromFlowTable(*flowTable, outFile, *every)
		check(err)
		fmt.Fprintf(os.Stderr, "%s: %d flows, %d initial records, %d window records; left out: %d began before the capture, "+
			"%d with merged first packets, %d too short; %d kept without window records (merged packets)\n",
			*flowTable, stats.Rows, stats.Initial, stats.Window, stats.BeganBeforeCapture,
			stats.MergedFirstPackets, stats.TooShort, stats.WindowsWithheld)
		return
	}

	o := extract.Offline{LAN: extract.PrivateNets, Max: *max, WindowEvery: *every}
	if *lan != "" {
		o.LAN = nil
		for _, s := range strings.Split(*lan, ",") {
			prefix, err := netip.ParsePrefix(strings.TrimSpace(s))
			check(err)
			o.LAN = append(o.LAN, prefix)
		}
	}
	if *keyHex != "" {
		key, err := hex.DecodeString(*keyHex)
		check(err)
		o.Key = key
	} else {
		o.Key = make([]byte, 32)
		_, err := rand.Read(o.Key)
		check(err)
	}

	outFile, err := os.Create(*out)
	check(err)
	defer outFile.Close()
	var keysFile *os.File
	if *keys != "" {
		keysFile, err = os.Create(*keys)
		check(err)
		defer keysFile.Close()
	}
	if *live {
		l := capture.Live{Group: *group, LAN: o.LAN, Key: o.Key, Max: *max, WindowEvery: *every, Out: outFile}
		if keysFile != nil {
			l.Keys = keysFile
		}
		if *dump != "" {
			dumpFile, err := os.Create(*dump)
			check(err)
			defer dumpFile.Close()
			l.Dump = dumpFile
		}
		if *install {
			check(capture.InstallRule(*group))
			defer capture.RemoveRule()
		}
		stop := make(chan struct{})
		sig := make(chan os.Signal, 1)
		signal.Notify(sig, syscall.SIGTERM, syscall.SIGINT)
		go func() {
			if *until > 0 {
				select {
				case <-sig:
				case <-time.After(time.Duration(*until * float64(time.Second))):
				}
			} else {
				<-sig
			}
			close(stop)
		}()
		stats, err := l.Run(stop)
		if *install {
			capture.RemoveRule()
		}
		check(err)
		fmt.Fprintf(os.Stderr, "live: %d packets, %d initial records, %d window records, %d flows tracked at exit, %d buffer overflows, %d flows not tracked\n",
			stats.Packets, stats.Initial, stats.Window, stats.Flows, stats.Overflows, stats.Untracked)
		return
	}

	var stats extract.Stats
	if keysFile != nil {
		stats, err = o.Run(*pcap, outFile, keysFile)
	} else {
		stats, err = o.Run(*pcap, outFile, nil)
	}
	check(err)
	fmt.Fprintf(os.Stderr, "%s: %d packets, %d flows, %d initial records, %d window records",
		*pcap, stats.Packets, stats.Flows, stats.Initial, stats.Window)
	if stats.Untracked > 0 {
		fmt.Fprintf(os.Stderr, ", %d flows not tracked (table full)", stats.Untracked)
	}
	fmt.Fprintln(os.Stderr)
}

func check(err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, "aqos-extract:", err)
		os.Exit(1)
	}
}
