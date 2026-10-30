package extract

import (
	"bytes"
	"encoding/binary"
	"encoding/json"
	"math"
	"net/netip"
	"os"
	"path/filepath"
	"testing"

	"adaptiveqos/contracts"
)

// ipv4 builds a packet whose header says it is l3Len long, with payload
// bytes that would give themselves away if anything read them.
func ipv4(proto uint8, src, dst string, l3Len int, flags uint8) []byte {
	s, d := netip.MustParseAddrPort(src), netip.MustParseAddrPort(dst)
	b := make([]byte, 20+20+40)
	b[0] = 0x45
	binary.BigEndian.PutUint16(b[2:], uint16(l3Len))
	b[9] = proto
	copy(b[12:], s.Addr().AsSlice())
	copy(b[16:], d.Addr().AsSlice())
	binary.BigEndian.PutUint16(b[20:], s.Port())
	binary.BigEndian.PutUint16(b[22:], d.Port())
	b[33] = flags
	for i := 40; i < len(b); i++ {
		b[i] = 0xEE
	}
	return b
}

func ipv6(proto uint8, src, dst string, l3Len int) []byte {
	s, d := netip.MustParseAddrPort(src), netip.MustParseAddrPort(dst)
	b := make([]byte, 40+20+40)
	b[0] = 0x60
	binary.BigEndian.PutUint16(b[4:], uint16(l3Len-40))
	b[6] = proto
	copy(b[8:], s.Addr().AsSlice())
	copy(b[24:], d.Addr().AsSlice())
	binary.BigEndian.PutUint16(b[40:], s.Port())
	binary.BigEndian.PutUint16(b[42:], d.Port())
	return b
}

func TestParsePacket(t *testing.T) {
	p, ok := ParsePacket(ipv4(ProtoUDP, "10.77.0.10:50000", "10.78.0.10:3478", 1100, 0), 42)
	if !ok || p.L3Len != 1100 || p.Protocol != ProtoUDP || p.TsNs != 42 ||
		p.Src.String() != "10.77.0.10:50000" || p.Dst.String() != "10.78.0.10:3478" {
		t.Errorf("ipv4 udp: %+v %v", p, ok)
	}
	p, ok = ParsePacket(ipv4(ProtoTCP, "10.77.0.10:50000", "10.78.0.10:443", 60, FlagSYN), 0)
	if !ok || p.TCPFlags != FlagSYN {
		t.Errorf("tcp flags: %+v %v", p, ok)
	}
	p, ok = ParsePacket(ipv6(ProtoTCP, "[fd77:0:0:1::10]:50000", "[fd77:0:0:4::2]:443", 1280), 0)
	if !ok || p.L3Len != 1280 || p.Src.Port() != 50000 || !p.Src.Addr().Is6() {
		t.Errorf("ipv6: %+v %v", p, ok)
	}

	icmp := ipv4(1, "10.77.0.10:0", "10.78.0.10:0", 84, 0)
	fragment := ipv4(ProtoUDP, "10.77.0.10:1", "10.78.0.10:2", 1500, 0)
	binary.BigEndian.PutUint16(fragment[6:], 185) // fragment offset: no ports in this one
	for name, b := range map[string][]byte{"icmp": icmp, "fragment": fragment, "short": {0x45, 0}, "arp": make([]byte, 28)} {
		if _, ok := ParsePacket(b, 0); ok {
			t.Errorf("%s was accepted", name)
		}
	}
}

// The capture rule keeps HeaderBytes of each packet. Whatever lies beyond
// must not change the answer, or something is reading payload.
func TestNothingPastTheHeadersIsRead(t *testing.T) {
	for _, full := range [][]byte{
		ipv4(ProtoTCP, "10.77.0.10:50000", "10.78.0.10:443", 1500, FlagACK),
		ipv4(ProtoUDP, "10.77.0.10:50000", "10.78.0.10:3478", 1100, 0),
		ipv6(ProtoTCP, "[fd77:0:0:1::10]:50000", "[fd77:0:0:4::2]:443", 1280),
		ipv6(ProtoUDP, "[fd77:0:0:1::10]:50000", "[fd77:0:0:4::2]:443", 1280),
	} {
		whole, ok1 := ParsePacket(full, 7)
		cut, ok2 := ParsePacket(full[:HeaderBytes], 7)
		if !ok1 || !ok2 || whole != cut {
			t.Errorf("truncating to %d bytes changed the result: %+v vs %+v", HeaderBytes, whole, cut)
		}
	}
	if HeaderBytes != 64 {
		t.Errorf("HeaderBytes is %d; the capture rule's snaplen and the specification say 64", HeaderBytes)
	}
}

func TestWindowMatchesTheContract(t *testing.T) {
	if Window(nil) != nil {
		t.Error("a window without samples")
	}
	// Two seconds: 100 packets of 1000 bytes up and 50 of 200 down, then
	// 300 up of 1000 and 50 of 200 down.
	w := Window([]Sample{
		{PktsUp: 100, PktsDown: 50, BytesUp: 100000, BytesDown: 10000},
		{PktsUp: 300, PktsDown: 50, BytesUp: 300000, BytesDown: 10000},
	})
	near := func(name string, got, want float64) {
		if math.Abs(got-want) > 1e-9 {
			t.Errorf("%s = %v, want %v", name, got, want)
		}
	}
	near("samples", float64(w.Samples), 2)
	near("pkts_up_per_s", w.PktsUpPerS, 200)
	near("pkts_down_per_s", w.PktsDownPerS, 50)
	near("bytes_up_per_s", w.BytesUpPerS, 200000)
	near("bytes_down_per_s", w.BytesDownPerS, 10000)
	near("mean_pkt_size_up", w.MeanPktSizeUp, 1000)
	near("mean_pkt_size_down", w.MeanPktSizeDown, 200)
	// Per-second mean size: 110000/150 and 310000/350; standard deviation of the two.
	a, b := 110000.0/150, 310000.0/350
	near("pkt_size_spread", w.PktSizeSpread, math.Abs(a-b)/2)
	// Per-second packets 150 and 350: std 100, mean 250.
	near("rate_cv", w.RateCV, 0.4)
	// Per-second bytes 110000 and 310000: peak over mean.
	near("burstiness", w.Burstiness, 310000.0/210000)

	steady := Window([]Sample{{PktsUp: 50, BytesUp: 5000}, {PktsUp: 50, BytesUp: 5000}, {PktsUp: 50, BytesUp: 5000}})
	near("steady rate_cv", steady.RateCV, 0)
	near("steady burstiness", steady.Burstiness, 1)
	near("steady spread", steady.PktSizeSpread, 0)
	near("no downstream size", steady.MeanPktSizeDown, 0)

	idle := Window([]Sample{{}, {}})
	near("idle burstiness", idle.Burstiness, 0)
	near("idle rate_cv", idle.RateCV, 0)

	long := make([]Sample, 9)
	for i := range long {
		long[i].PktsUp = uint64(i)
	}
	if w := Window(long); w.Samples != WindowSeconds || w.PktsUpPerS != 6 { // seconds 4..8
		t.Errorf("only the last %d samples count: %+v", WindowSeconds, w)
	}
}

var lan = mustPrefixes("10.77.0.0/24", "fd77:0:0:1::/64")

const host, server = "10.77.0.10:50000", "10.78.0.10:3478"

func feed(t *testing.T, table *Table, steps []struct {
	fromLAN bool
	size    int
	atUs    int64
}) (*Flow, *contracts.FeatureRecord) {
	t.Helper()
	var flow *Flow
	var record *contracts.FeatureRecord
	for i, s := range steps {
		src, dst := host, server
		if !s.fromLAN {
			src, dst = server, host
		}
		p, _ := ParsePacket(ipv4(ProtoUDP, src, dst, s.size, 0), s.atUs*1000)
		f, rec := table.Packet(p)
		if rec != nil && i != FirstPackets-1 {
			t.Fatalf("a record after packet %d", i+1)
		}
		if rec != nil {
			record = rec
		}
		flow = f
	}
	return flow, record
}

// The contract's own example record, rebuilt packet by packet.
func TestTableEmitsTheContractExample(t *testing.T) {
	var want contracts.FeatureRecord
	data, err := os.ReadFile(filepath.Join("..", "..", "contracts", "examples", "feature_record.initial.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(data, &want); err != nil {
		t.Fatal(err)
	}
	var steps []struct {
		fromLAN bool
		size    int
		atUs    int64
	}
	at := int64(1000)
	for i, size := range want.PktSizes {
		if i > 0 {
			at += int64(want.PktGapsUs[i-1])
		}
		steps = append(steps, struct {
			fromLAN bool
			size    int
			atUs    int64
		}{size > 0, int(math.Abs(float64(size))), at})
	}
	// More packets after the eighth must change nothing and emit nothing.
	steps = append(steps, steps[0], steps[1])
	steps[8].atUs, steps[9].atUs = at+50, at+60

	table := NewTable(lan, []byte("k"), 0)
	hostAddr, serverAddr := netip.MustParseAddrPort(host), netip.MustParseAddrPort(server)
	flow, got := feed(t, table, steps)
	if got == nil {
		t.Fatal("no record after eight packets")
	}
	if got.PktSizes != want.PktSizes || got.PktGapsUs != want.PktGapsUs {
		t.Errorf("sizes %v gaps %v\nwant  %v gaps %v", got.PktSizes, got.PktGapsUs, want.PktSizes, want.PktGapsUs)
	}
	if got.Kind != "initial" || got.Window != nil || got.IPVersion != 4 || got.Protocol != ProtoUDP ||
		got.SrcPort != int(hostAddr.Port()) || got.DstPort != int(serverAddr.Port()) {
		t.Errorf("header fields: %+v", got)
	}
	if got.TsNs != at*1000 {
		t.Errorf("ts_ns %d, want the eighth packet's time %d", got.TsNs, at*1000)
	}
	if len(got.FlowID) != 16 || len(got.HostID) != 16 || got.FlowID == got.HostID {
		t.Errorf("ids: %q %q", got.FlowID, got.HostID)
	}
	if !flow.LANIsA || table.Len() != 1 {
		t.Errorf("flow: %+v, table %d", flow, table.Len())
	}
	// Strict encoding: exactly the contract's fields.
	encoded, _ := json.Marshal(got)
	dec := json.NewDecoder(bytes.NewReader(encoded))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&contracts.FeatureRecord{}); err != nil {
		t.Error(err)
	}
}

func TestDirectionFollowsTheLANNotTheFirstSender(t *testing.T) {
	// The first packet seen comes from the WAN side, as when a capture
	// starts mid-flow. Up must still mean LAN to WAN.
	table := NewTable(lan, []byte("k"), 0)
	var rec *contracts.FeatureRecord
	for i := 0; i < FirstPackets; i++ {
		src, dst := server, host
		if i%2 == 1 {
			src, dst = host, server
		}
		p, _ := ParsePacket(ipv4(ProtoUDP, src, dst, 100+i, 0), int64(i)*1e6)
		_, rec = table.Packet(p)
	}
	if rec.PktSizes != [8]int{-100, 101, -102, 103, -104, 105, -106, 107} {
		t.Errorf("signs: %v", rec.PktSizes)
	}
	// Ports are the first sender's, as the contract says, even though that
	// side is the WAN here.
	if rec.SrcPort != 3478 || rec.DstPort != 50000 {
		t.Errorf("ports: %d %d", rec.SrcPort, rec.DstPort)
	}
}

func TestIDsDependOnTheKeyAndHideAddresses(t *testing.T) {
	record := func(key string) *contracts.FeatureRecord {
		table := NewTable(lan, []byte(key), 0)
		var rec *contracts.FeatureRecord
		for i := 0; i < FirstPackets; i++ {
			p, _ := ParsePacket(ipv4(ProtoUDP, host, server, 100, 0), int64(i))
			_, rec = table.Packet(p)
		}
		return rec
	}
	a, again, b := record("key one"), record("key one"), record("key two")
	if a.FlowID != again.FlowID || a.HostID != again.HostID {
		t.Error("the same key gave different ids")
	}
	if a.FlowID == b.FlowID || a.HostID == b.HostID {
		t.Error("ids do not depend on the key")
	}
	encoded, _ := json.Marshal(a)
	if bytes.Contains(encoded, []byte("10.77.0.10")) || bytes.Contains(encoded, []byte("10.78")) {
		t.Errorf("an address in a feature record: %s", encoded)
	}
}

func TestFullTableLeavesNewFlowsUntracked(t *testing.T) {
	table := NewTable(lan, []byte("k"), 2)
	for port := 1; port <= 5; port++ {
		p, _ := ParsePacket(ipv4(ProtoUDP, host, server, 100, 0), 0)
		p.Src = netip.AddrPortFrom(p.Src.Addr(), uint16(1000+port))
		table.Packet(p)
	}
	if table.Len() != 2 || table.Untracked != 3 {
		t.Errorf("len %d untracked %d", table.Len(), table.Untracked)
	}
}

func TestTCPTupleReusedAfterCloseIsANewFlow(t *testing.T) {
	table := NewTable(lan, []byte("k"), 0)
	send := func(fromLAN bool, flags uint8, at int64) (*Flow, *contracts.FeatureRecord) {
		src, dst := "10.77.0.10:50000", "10.78.0.10:443"
		if !fromLAN {
			src, dst = dst, src
		}
		p, _ := ParsePacket(ipv4(ProtoTCP, src, dst, 60, flags), at)
		return table.Packet(p)
	}
	first, _ := send(true, FlagSYN, 0)
	send(false, FlagSYN|FlagACK, 1)
	send(true, FlagRST, 2)
	second, _ := send(true, FlagSYN, 3)
	if first == second || second.seen != 1 || table.Len() != 1 {
		t.Errorf("a SYN after a reset must start a new flow: %p %p", first, second)
	}
	// A retransmitted SYN on a live connection must not.
	third, _ := send(true, FlagSYN, 4)
	if third != second {
		t.Error("a repeated SYN split a live flow")
	}
}

func TestCountersBecomeWindowRecords(t *testing.T) {
	table := NewTable(lan, []byte("k"), 0)
	var flow *Flow
	for i := 0; i < FirstPackets; i++ {
		p, _ := ParsePacket(ipv4(ProtoUDP, host, server, 1000, 0), int64(i))
		flow, _ = table.Packet(p)
	}
	if table.Record(flow, "window", 0) != nil {
		t.Error("a window record before any one-second sample")
	}
	// The first reading is only a baseline; each later one is a sample.
	flow.Counters(Counters{PktsUp: 8, BytesUp: 8000})
	if table.Record(flow, "window", 0) != nil {
		t.Error("the baseline reading counted as a sample")
	}
	flow.Counters(Counters{PktsUp: 58, PktsDown: 50, BytesUp: 58000, BytesDown: 5000})
	rec := table.Record(flow, "window", 99)
	if rec == nil || rec.Kind != "window" || rec.Window.Samples != 1 || rec.Window.PktsUpPerS != 50 ||
		rec.Window.BytesDownPerS != 5000 || rec.TsNs != 99 {
		t.Fatalf("window record: %+v", rec)
	}
	if rec.PktSizes != [8]int{1000, 1000, 1000, 1000, 1000, 1000, 1000, 1000} {
		t.Error("a window record must repeat the first-packet fields")
	}
	// Counters that go backwards mean a new conntrack entry: no bogus sample.
	flow.Counters(Counters{PktsUp: 3})
	if got := table.Record(flow, "window", 0).Window.Samples; got != 1 {
		t.Errorf("%d samples after a counter reset, want 1", got)
	}
}
