package extract

import (
	"bufio"
	"compress/gzip"
	"crypto/sha256"
	"encoding/binary"
	"encoding/json"
	"io"
	"net/netip"
	"os"
	"strings"

	"adaptiveqos/contracts"
)

// A flow table is what the Kaggle script keeps of a public dataset: per
// flow, its first packets and per-second counts, and no addresses. This
// file turns those rows into feature records through the same Table as a
// capture or the live extractor, so public data gets the same features.

// MTU is the largest packet a link carries. A larger one in a capture was
// merged by receive offload and never crossed the wire as such.
const MTU = 1500

type tableRow struct {
	Source     string     `json:"source"`
	File       string     `json:"file"`
	Folder     string     `json:"folder"`
	Protocol   uint8      `json:"protocol"`
	IPVersion  int        `json:"ip_version"`
	SrcPort    uint16     `json:"src_port"`
	DstPort    uint16     `json:"dst_port"`
	SrcPrivate bool       `json:"src_private"`
	DstPrivate bool       `json:"dst_private"`
	StartS     float64    `json:"start_s"`
	DurationS  float64    `json:"duration_s"`
	Packets    uint64     `json:"packets"`
	Bytes      uint64     `json:"bytes"`
	Oversize   int        `json:"oversize"`
	FromStart  bool       `json:"from_start"`
	First      [][2]int64 `json:"first"`   // microseconds from the first packet, signed size
	Seconds    [][]uint64 `json:"seconds"` // second, packets and bytes each way
}

// DatasetRow is a feature record plus where it came from. The dataset
// builder adds the label and the session.
type DatasetRow struct {
	contracts.FeatureRecord
	Source string `json:"source"`
	File   string `json:"file"`
	Folder string `json:"folder"`
	// The whole flow's size, so the builder can tell a capture's main
	// flows from its background ones.
	FlowBytes   uint64 `json:"flow_bytes"`
	FlowPackets uint64 `json:"flow_packets"`
}

// TableStats says what became of a flow table's rows.
type TableStats struct {
	Rows, Initial, Window uint64
	// Skipped rows, by reason. A flow whose first packets were not captured
	// or were merged cannot give first-packet features at all.
	BeganBeforeCapture, MergedFirstPackets, TooShort uint64
	// WindowsWithheld counts flows that gave an initial record but no
	// window records, because merged packets make their packet counts
	// too low.
	WindowsWithheld uint64
}

// Addresses that only stand for "the LAN side" and "the other side". Real
// ones never leave the place the capture was made.
var tableLAN = mustPrefixes("10.0.0.0/8", "fd00::/8")

func fakeAddr(v6, lan bool, n uint32) netip.Addr {
	if v6 {
		b := [16]byte{0x20, 0x01, 0x0d, 0xb8}
		if lan {
			b = [16]byte{0xfd}
		}
		binary.BigEndian.PutUint32(b[12:], n)
		return netip.AddrFrom16(b)
	}
	b := [4]byte{203, byte(n >> 16), byte(n >> 8), byte(n)}
	if lan {
		b[0] = 10
	}
	return netip.AddrFrom4(b)
}

// FromFlowTable reads rows (JSON lines, gzip or not) and writes dataset
// rows. windowEvery is as for a capture.
func FromFlowTable(path string, out io.Writer, windowEvery int) (TableStats, error) {
	var stats TableStats
	file, err := os.Open(path)
	if err != nil {
		return stats, err
	}
	defer file.Close()
	var in io.Reader = file
	if strings.HasSuffix(path, ".gz") {
		zr, err := gzip.NewReader(file)
		if err != nil {
			return stats, err
		}
		defer zr.Close()
		in = zr
	}
	lines := bufio.NewScanner(in)
	lines.Buffer(make([]byte, 1<<20), 1<<28)
	w := bufio.NewWriter(out)
	defer w.Flush()
	write := func(rec *contracts.FeatureRecord, row *tableRow) {
		line, _ := json.Marshal(DatasetRow{*rec, row.Source, row.File, row.Folder, row.Bytes, row.Packets})
		w.Write(line)
		w.WriteByte('\n')
	}

	for lines.Scan() {
		var row tableRow
		if err := json.Unmarshal(lines.Bytes(), &row); err != nil {
			return stats, err
		}
		stats.Rows++
		switch {
		case !row.FromStart:
			stats.BeganBeforeCapture++
			continue
		case len(row.First) < FirstPackets:
			stats.TooShort++
			continue
		}
		merged := false
		for _, p := range row.First[:FirstPackets] {
			if p[1] > MTU || p[1] < -MTU {
				merged = true
			}
		}
		if merged {
			stats.MergedFirstPackets++
			continue
		}

		// One table per flow: ids need only be distinct within a file, and
		// the key is the file's, so they do not link across files.
		key := sha256.Sum256([]byte(row.Source + "/" + row.Folder + "/" + row.File))
		table := NewTable(tableLAN, key[:], 0)
		v6 := row.IPVersion == 6
		srcIsLAN := row.SrcPrivate || !row.DstPrivate
		n := uint32(stats.Rows)
		src := netip.AddrPortFrom(fakeAddr(v6, srcIsLAN, n), row.SrcPort)
		dst := netip.AddrPortFrom(fakeAddr(v6, !srcIsLAN, n), row.DstPort)
		began := int64(row.StartS * 1e9)

		var flow *Flow
		var initial *contracts.FeatureRecord
		for _, p := range row.First[:FirstPackets] {
			packet := Packet{TsNs: began + p[0]*1000, Protocol: row.Protocol, Src: src, Dst: dst, L3Len: int(p[1])}
			if p[1] < 0 {
				packet.Src, packet.Dst, packet.L3Len = dst, src, int(-p[1])
			}
			flow, initial = table.Packet(packet)
		}
		if initial == nil {
			continue
		}
		write(initial, &row)
		stats.Initial++
		if windowEvery <= 0 {
			continue
		}
		if row.Oversize > 0 {
			stats.WindowsWithheld++
			continue
		}

		// The counters, as conntrack would have shown them at the end of
		// each whole second of the flow. As when live, the first reading
		// only sets the baseline, so the flow's first second gives no
		// sample. The last, partial second is left out.
		perSecond := map[uint64][]uint64{}
		for _, s := range row.Seconds {
			if len(s) == 5 {
				perSecond[s[0]] = s[1:]
			}
		}
		var total Counters
		for second := uint64(0); second < uint64(row.DurationS); second++ {
			if s := perSecond[second]; s != nil {
				fwdPkts, revPkts, fwdBytes, revBytes := s[0], s[1], s[2], s[3]
				if !flow.LANIsA {
					fwdPkts, revPkts, fwdBytes, revBytes = revPkts, fwdPkts, revBytes, fwdBytes
				}
				total.PktsUp += fwdPkts
				total.PktsDown += revPkts
				total.BytesUp += fwdBytes
				total.BytesDown += revBytes
			}
			flow.Counters(total)
			if (second+1)%uint64(windowEvery) != 0 {
				continue
			}
			rec := table.Record(flow, "window", began+int64(second+1)*1e9)
			if rec != nil && rec.Window.PktsUpPerS+rec.Window.PktsDownPerS > 0 {
				write(rec, &row)
				stats.Window++
			}
		}
	}
	return stats, lines.Err()
}
