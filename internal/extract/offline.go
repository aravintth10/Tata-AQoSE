package extract

import (
	"bufio"
	"encoding/json"
	"io"
	"net/netip"
	"time"

	"adaptiveqos/contracts"
)

// PrivateNets is the default idea of "the LAN" for captures taken on some
// unknown home or lab network.
var PrivateNets = mustPrefixes("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")

func mustPrefixes(list ...string) []netip.Prefix {
	out := make([]netip.Prefix, len(list))
	for i, s := range list {
		out[i] = netip.MustParsePrefix(s)
	}
	return out
}

// How long a silent flow is kept, after conntrack's default timeouts.
const (
	udpIdle    = 120 * time.Second
	tcpIdle    = 300 * time.Second
	closedIdle = 10 * time.Second
)

// Offline holds the settings for running a capture through the table.
type Offline struct {
	LAN []netip.Prefix
	Key []byte
	Max int
	// WindowEvery emits a window record for each active flow this often
	// after its initial record. Zero emits none: live, window records are
	// only made for a reclassification check.
	WindowEvery int
}

// KeyLine maps a flow_id back to its tuple, so the dataset builder can join
// labels. It holds addresses and stays local; it is not a contract.
type KeyLine struct {
	FlowID   string  `json:"flow_id"`
	Protocol int     `json:"protocol"`
	SrcIP    string  `json:"src_ip"`
	SrcPort  int     `json:"src_port"`
	DstIP    string  `json:"dst_ip"`
	DstPort  int     `json:"dst_port"`
	FirstS   float64 `json:"first_s"` // first packet, from the start of the capture
	LANIsSrc bool    `json:"lan_is_src"`
}

// Stats says what a run saw.
type Stats struct {
	Packets, Initial, Window, Flows, Untracked uint64
}

// Run reads a capture and writes feature records to out and, if keys is not
// nil, one KeyLine per classified flow.
func (o Offline) Run(pcap string, out, keys io.Writer) (Stats, error) {
	var stats Stats
	table := NewTable(o.LAN, o.Key, o.Max)
	records, keyLines := bufio.NewWriter(out), bufio.NewWriter(io.Discard)
	if keys != nil {
		keyLines = bufio.NewWriter(keys)
	}
	write := func(w *bufio.Writer, v any) {
		line, _ := json.Marshal(v)
		w.Write(line)
		w.WriteByte('\n')
	}
	firstSeen := map[*Flow]int64{}
	initialAt := map[*Flow]int64{} // second index of the flow's initial record
	var began, nextTick, second int64

	tick := func() {
		table.Tick()
		second++
		now := began + second*1e9
		table.Flows(func(f *Flow) {
			idle := time.Duration(now - f.LastSeen)
			limit := udpIdle
			if f.Key.Protocol == ProtoTCP {
				limit = tcpIdle
				if f.closed == 3 {
					limit = closedIdle
				}
			}
			if idle > limit {
				table.Remove(f)
				delete(firstSeen, f)
				delete(initialAt, f)
				return
			}
			at, classified := initialAt[f]
			if !classified || o.WindowEvery <= 0 || (second-at)%int64(o.WindowEvery) != 0 || second == at {
				return
			}
			if w := Window(f.samples); w == nil || w.PktsUpPerS+w.PktsDownPerS == 0 {
				return // a flow that has gone quiet tells the classifier nothing
			}
			if rec := table.Record(f, "window", now); rec != nil {
				write(records, rec)
				stats.Window++
			}
		})
	}

	err := ReadPcap(pcap, func(tsNs int64, ip []byte) {
		p, ok := ParsePacket(ip, tsNs)
		if !ok {
			return
		}
		if stats.Packets == 0 {
			began, nextTick = tsNs, tsNs+1e9
		}
		stats.Packets++
		for tsNs >= nextTick {
			tick()
			nextTick += 1e9
		}
		f, rec := table.Packet(p)
		if f == nil {
			return
		}
		if _, known := firstSeen[f]; !known {
			firstSeen[f] = tsNs
			stats.Flows++
		}
		if rec != nil {
			write(records, rec)
			stats.Initial++
			initialAt[f] = second
			write(keyLines, keyLine(rec, f, float64(firstSeen[f]-began)/1e9))
		}
	})
	stats.Untracked = table.Untracked
	if flushErr := records.Flush(); err == nil {
		err = flushErr
	}
	if flushErr := keyLines.Flush(); err == nil {
		err = flushErr
	}
	return stats, err
}

func keyLine(rec *contracts.FeatureRecord, f *Flow, firstS float64) KeyLine {
	return KeyLine{
		FlowID: rec.FlowID, Protocol: rec.Protocol,
		SrcIP: f.Key.A.Addr().String(), SrcPort: rec.SrcPort,
		DstIP: f.Key.B.Addr().String(), DstPort: rec.DstPort,
		FirstS: firstS, LANIsSrc: f.LANIsA,
	}
}
