// Package capture is the live side of the feature extractor: it feeds the
// shared flow table from the kernel instead of from a capture file.
package capture

import (
	"bufio"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"net/netip"
	"os/exec"
	"strings"
	"time"

	"adaptiveqos/contracts"
	"adaptiveqos/internal/extract"
	"adaptiveqos/internal/netfilter"
	"adaptiveqos/internal/runclock"
)

// Rule sends the first eight packets of every forwarded flow to an nflog
// group, headers only. The kernel does the counting, so the extractor's
// work follows new flows per second and stops by itself after the eighth
// packet. It needs conntrack accounting, which the testbed turns on.
func Rule(group int) string {
	return fmt.Sprintf(`
table inet aqos_extract {
	chain forward {
		type filter hook forward priority filter + 5;
		ct packets < %d log group %d snaplen %d
	}
}`, extract.FirstPackets+1, group, extract.HeaderBytes)
}

// InstallRule adds Rule to the current namespace, replacing an older copy.
func InstallRule(group int) error {
	RemoveRule()
	cmd := exec.Command("nft", "-f", "-")
	cmd.Stdin = strings.NewReader(Rule(group))
	if out, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("nft: %w: %s", err, strings.TrimSpace(string(out)))
	}
	return nil
}

func RemoveRule() { exec.Command("nft", "delete", "table", "inet", "aqos_extract").Run() }

// Live holds the settings of a live extractor.
type Live struct {
	Group int
	LAN   []netip.Prefix
	Key   []byte
	Max   int
	// WindowEvery emits a window record per active flow this often. The
	// controller will ask for window records itself when it needs them;
	// this is for collecting data and for tests.
	WindowEvery int

	Out  io.Writer // feature records
	Keys io.Writer // optional: flow_id to tuple, holds addresses
	// Dump, if set, receives every packet exactly as the extractor got it,
	// as a capture file. Running that file through the offline tool must
	// give the same records: that is the check that the two paths are one.
	Dump io.Writer

	// The controller's hooks, all called from Run's own goroutine. Out may
	// be nil when they are used.
	OnRecord func(f *extract.Flow, rec *contracts.FeatureRecord) // a flow's initial record
	OnWindow func(f *extract.Flow, rec *contracts.FeatureRecord) // every WindowEvery seconds while it lives
	OnGone   func(f *extract.Flow)                               // the flow has ended
	OnSecond func(nowNs int64)                                   // after each conntrack read
}

// Stats says what a live run saw.
type Stats struct {
	Packets, Initial, Window, Overflows, Untracked uint64
	Flows                                          int
}

// Run extracts until stop is closed.
func (l Live) Run(stop <-chan struct{}) (Stats, error) {
	var stats Stats
	log, err := netfilter.ListenNFLog(uint16(l.Group), extract.HeaderBytes)
	if err != nil {
		return stats, err
	}
	defer log.Close()
	conntrack, err := netfilter.OpenConntrack()
	if err != nil {
		return stats, err
	}
	defer conntrack.Close()

	table := extract.NewTable(l.LAN, l.Key, l.Max)
	if l.Out == nil {
		l.Out = io.Discard
	}
	records := bufio.NewWriter(l.Out)
	keys, dump := bufio.NewWriter(io.Discard), bufio.NewWriter(io.Discard)
	if l.Keys != nil {
		keys = bufio.NewWriter(l.Keys)
	}
	if l.Dump != nil {
		dump = bufio.NewWriter(l.Dump)
		writeDumpHeader(dump)
	}
	write := func(w *bufio.Writer, v any) {
		line, _ := json.Marshal(v)
		w.Write(line)
		w.WriteByte('\n')
	}
	began := time.Now().UnixNano()
	missing := map[*extract.Flow]int{}
	classified := map[*extract.Flow]int{} // seconds since the initial record
	nextSecond := time.Now().Add(time.Second)

	second := func() error {
		seen := map[*extract.Flow]bool{}
		err := conntrack.Dump(func(e netfilter.Entry) {
			f, forward := table.Lookup(e.Protocol, e.Src, e.Dst)
			if f == nil || !forward {
				return
			}
			seen[f] = true
			c := extract.Counters{PktsUp: e.OrigPkts, BytesUp: e.OrigBytes, PktsDown: e.ReplyPkts, BytesDown: e.ReplyBytes}
			if !f.LANIsA {
				c = extract.Counters{PktsUp: e.ReplyPkts, BytesUp: e.ReplyBytes, PktsDown: e.OrigPkts, BytesDown: e.OrigBytes}
			}
			f.Counters(c)
		})
		if err != nil {
			return err
		}
		now := runclock.NowNs()
		table.Flows(func(f *extract.Flow) {
			if !seen[f] {
				// Its conntrack entry is gone, so the flow is over. One
				// missed dump is forgiven: the entry may be newer than it.
				if missing[f]++; missing[f] > 1 {
					table.Remove(f)
					delete(missing, f)
					delete(classified, f)
					if l.OnGone != nil {
						l.OnGone(f)
					}
				}
				return
			}
			delete(missing, f)
			age, ok := classified[f]
			if !ok {
				return
			}
			classified[f] = age + 1
			if l.WindowEvery <= 0 || (age+1)%l.WindowEvery != 0 {
				return
			}
			if rec := table.Record(f, "window", now); rec != nil && rec.Window.PktsUpPerS+rec.Window.PktsDownPerS > 0 {
				write(records, rec)
				stats.Window++
				if l.OnWindow != nil {
					l.OnWindow(f, rec)
				}
			}
		})
		records.Flush()
		keys.Flush()
		dump.Flush()
		if l.OnSecond != nil {
			l.OnSecond(now)
		}
		return nil
	}

	for {
		select {
		case <-stop:
			stats.Overflows, stats.Untracked, stats.Flows = log.Overflows, table.Untracked, table.Len()
			records.Flush()
			keys.Flush()
			return stats, dump.Flush()
		default:
		}
		err := log.Read(func(lp netfilter.LogPacket) {
			writeDumpPacket(dump, lp)
			p, ok := extract.ParsePacket(lp.IP, lp.TsNs)
			if !ok {
				return
			}
			stats.Packets++
			f, rec := table.Packet(p)
			if rec == nil {
				return
			}
			// The contract's ts_ns is the monotonic time of emission; the
			// packet times above only measure gaps.
			rec.TsNs = runclock.NowNs()
			write(records, rec)
			stats.Initial++
			classified[f] = 0
			if l.OnRecord != nil {
				l.OnRecord(f, rec)
			}
			write(keys, extract.KeyLine{
				FlowID: rec.FlowID, Protocol: rec.Protocol,
				SrcIP: f.Key.A.Addr().String(), SrcPort: rec.SrcPort,
				DstIP: f.Key.B.Addr().String(), DstPort: rec.DstPort,
				FirstS: float64(lp.TsNs-began) / 1e9, LANIsSrc: f.LANIsA,
			})
		})
		if err != nil {
			return stats, err
		}
		if time.Now().After(nextSecond) {
			nextSecond = nextSecond.Add(time.Second)
			if err := second(); err != nil {
				return stats, err
			}
		}
	}
}

// The dump is a nanosecond pcap of raw IP packets (link type 101).
func writeDumpHeader(w io.Writer) {
	head := make([]byte, 24)
	binary.LittleEndian.PutUint32(head[0:], 0xa1b23c4d)
	binary.LittleEndian.PutUint16(head[4:], 2)
	binary.LittleEndian.PutUint16(head[6:], 4)
	binary.LittleEndian.PutUint32(head[16:], extract.HeaderBytes)
	binary.LittleEndian.PutUint32(head[20:], 101)
	w.Write(head)
}

func writeDumpPacket(w io.Writer, p netfilter.LogPacket) {
	record := make([]byte, 16)
	binary.LittleEndian.PutUint32(record[0:], uint32(p.TsNs/1e9))
	binary.LittleEndian.PutUint32(record[4:], uint32(p.TsNs%1e9))
	binary.LittleEndian.PutUint32(record[8:], uint32(len(p.IP)))
	binary.LittleEndian.PutUint32(record[12:], uint32(len(p.IP)))
	w.Write(record)
	w.Write(p.IP)
}
