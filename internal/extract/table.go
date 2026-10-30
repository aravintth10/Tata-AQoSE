package extract

import (
	"net/netip"

	"adaptiveqos/contracts"
)

// Key names a flow: its protocol, the side that sent the first packet, and
// the other side.
type Key struct {
	Protocol uint8
	A, B     netip.AddrPort
}

func (k Key) reversed() Key { return Key{k.Protocol, k.B, k.A} }

// Flow is one tracked flow.
type Flow struct {
	Key    Key
	FlowID string
	HostID string
	// LANIsA says which side is the LAN, and so which way is up.
	LANIsA bool

	seen     int // packets seen, up to FirstPackets
	sizes    [FirstPackets]int
	gaps     [FirstPackets - 1]int
	lastTsNs int64
	// Truncated is set when some of the first packets were lost to a full
	// buffer. The flow is then never classified.
	Truncated bool
	closed    uint8 // TCP: which sides have sent FIN; RST closes both

	total    Counters // offline only: counted from packets
	prev     Counters
	havePrev bool
	samples  []Sample
	LastSeen int64
}

// Complete reports whether the first packets have all been seen.
func (f *Flow) Complete() bool { return f.seen == FirstPackets }

// Table holds the flows being watched.
type Table struct {
	// LAN lists the prefixes on the LAN side. A flow with neither or both
	// ends in them is taken to be opened from the LAN.
	LAN    []netip.Prefix
	Hasher Hasher
	// Max bounds the table. When it is full new flows are not tracked, and
	// so are never labelled: plain CAKE behaviour.
	Max int

	flows map[Key]*Flow
	// Untracked counts flows turned away because the table was full.
	Untracked uint64
}

func NewTable(lan []netip.Prefix, key []byte, max int) *Table {
	return &Table{LAN: lan, Hasher: Hasher{key}, Max: max, flows: map[Key]*Flow{}}
}

func (t *Table) Len() int { return len(t.flows) }

func (t *Table) isLAN(addr netip.Addr) bool {
	for _, p := range t.LAN {
		if p.Contains(addr) {
			return true
		}
	}
	return false
}

// Lookup finds a flow whichever way round the endpoints are given. forward
// says whether a is the flow's first sender.
func (t *Table) Lookup(protocol uint8, a, b netip.AddrPort) (flow *Flow, forward bool) {
	if f := t.flows[Key{protocol, a, b}]; f != nil {
		return f, true
	}
	return t.flows[Key{protocol, b, a}], false
}

// Flows calls fn for every tracked flow.
func (t *Table) Flows(fn func(*Flow)) {
	for _, f := range t.flows {
		fn(f)
	}
}

// Remove forgets a flow, as when its conntrack entry is destroyed.
func (t *Table) Remove(f *Flow) { delete(t.flows, f.Key) }

func (t *Table) add(p Packet) *Flow {
	if t.Max > 0 && len(t.flows) >= t.Max {
		t.Untracked++
		return nil
	}
	key := Key{p.Protocol, p.Src, p.Dst}
	lanA, lanB := t.isLAN(p.Src.Addr()), t.isLAN(p.Dst.Addr())
	f := &Flow{Key: key, LANIsA: lanA || !lanB}
	lan := key.A
	if !f.LANIsA {
		lan = key.B
	}
	f.FlowID = t.Hasher.FlowID(key.Protocol, key.A, key.B)
	f.HostID = t.Hasher.HostID(lan.Addr())
	t.flows[key] = f
	return f
}

// Packet takes one packet. It returns the flow's initial feature record
// when this is the flow's eighth packet, and nil otherwise. After that the
// flow's packets no longer matter to the first-packet features; live, the
// kernel stops sending them at all.
func (t *Table) Packet(p Packet) (*Flow, *contracts.FeatureRecord) {
	f, forward := t.Lookup(p.Protocol, p.Src, p.Dst)
	if f != nil && p.Protocol == ProtoTCP && t.reopened(f, p) {
		t.Remove(f)
		f = nil
	}
	if f == nil {
		if f = t.add(p); f == nil {
			return nil, nil
		}
		forward = true
	}
	up := forward == f.LANIsA
	f.LastSeen = p.TsNs
	if up {
		f.total.PktsUp++
		f.total.BytesUp += uint64(p.L3Len)
	} else {
		f.total.PktsDown++
		f.total.BytesDown += uint64(p.L3Len)
	}
	if p.Protocol == ProtoTCP {
		switch {
		case p.TCPFlags&FlagRST != 0:
			f.closed = 3
		case p.TCPFlags&FlagFIN != 0 && forward:
			f.closed |= 1
		case p.TCPFlags&FlagFIN != 0:
			f.closed |= 2
		}
	}
	if f.seen == FirstPackets || f.Truncated {
		return f, nil
	}
	if f.seen > 0 {
		f.gaps[f.seen-1] = int(max(p.TsNs-f.lastTsNs, 0) / 1000)
	}
	f.sizes[f.seen] = p.L3Len
	if !up {
		f.sizes[f.seen] = -p.L3Len
	}
	f.lastTsNs = p.TsNs
	f.seen++
	if f.seen < FirstPackets {
		return f, nil
	}
	return f, t.Record(f, "initial", p.TsNs)
}

// reopened reports whether a TCP packet starts a new connection on a tuple
// whose old connection has ended, which conntrack treats as a new flow.
func (t *Table) reopened(f *Flow, p Packet) bool {
	return p.TCPFlags&(FlagSYN|FlagACK) == FlagSYN && f.closed == 3
}

// Counters takes a flow's running totals as read once per second, live from
// conntrack and offline from the packets themselves, and keeps the last
// WindowSeconds differences. The first reading only sets the baseline.
func (f *Flow) Counters(now Counters) {
	if f.havePrev && now.PktsUp >= f.prev.PktsUp && now.PktsDown >= f.prev.PktsDown {
		f.samples = append(f.samples, Sample{
			PktsUp: now.PktsUp - f.prev.PktsUp, PktsDown: now.PktsDown - f.prev.PktsDown,
			BytesUp: now.BytesUp - f.prev.BytesUp, BytesDown: now.BytesDown - f.prev.BytesDown,
		})
		if len(f.samples) > WindowSeconds {
			f.samples = f.samples[len(f.samples)-WindowSeconds:]
		}
	}
	f.prev, f.havePrev = now, true
}

// Tick is the offline stand-in for the once-per-second conntrack read: it
// feeds every flow the totals counted from its own packets.
func (t *Table) Tick() {
	for _, f := range t.flows {
		f.Counters(f.total)
	}
}

// Record builds a feature record for a flow as it stands. kind is
// "initial" or "window"; a window record needs at least one sample and
// repeats the first-packet fields so the classifier stays stateless.
func (t *Table) Record(f *Flow, kind string, tsNs int64) *contracts.FeatureRecord {
	window := Window(f.samples)
	if kind == "window" && window == nil {
		return nil
	}
	// The contract has no way to say "first packets unknown", so a flow
	// that lost them gets no record and stays unlabelled.
	if !f.Complete() {
		return nil
	}
	version := 4
	if f.Key.A.Addr().Is6() && !f.Key.A.Addr().Is4In6() {
		version = 6
	}
	return &contracts.FeatureRecord{
		SchemaVersion: contracts.SchemaVersion,
		FlowID:        f.FlowID,
		HostID:        f.HostID,
		Kind:          kind,
		TsNs:          tsNs,
		IPVersion:     version,
		Protocol:      int(f.Key.Protocol),
		SrcPort:       int(f.Key.A.Port()),
		DstPort:       int(f.Key.B.Port()),
		PktSizes:      f.sizes,
		PktGapsUs:     f.gaps,
		Window:        window,
	}
}
