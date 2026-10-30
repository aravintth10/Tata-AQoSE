// Package extract turns packets into feature records. The live extractor
// and the offline tool both go through this package and nothing else, so
// training data and live data cannot be computed differently.
//
// Nothing here reads past the transport ports. HeaderBytes is all a capture
// has to keep.
package extract

import (
	"encoding/binary"
	"net/netip"
)

// HeaderBytes is the most this package reads of a packet, counted from the
// IP header: a 40-byte IPv6 header, the two ports, and the TCP flags.
const HeaderBytes = 64

const (
	ProtoTCP = 6
	ProtoUDP = 17
)

// TCP flags, as far as flow boundaries need them.
const (
	FlagFIN = 0x01
	FlagSYN = 0x02
	FlagRST = 0x04
	FlagACK = 0x10
)

// Packet is what is kept of one packet: who, when and how long.
type Packet struct {
	TsNs     int64
	Protocol uint8
	Src, Dst netip.AddrPort
	// L3Len is the IP packet's length including its header, taken from the
	// header itself, so a truncated capture gives the same answer.
	L3Len    int
	TCPFlags uint8
}

// ParsePacket reads an IP packet that may be cut short after HeaderBytes.
// It reports false for anything that is not TCP or UDP over IPv4 or IPv6,
// and for later fragments, which carry no ports.
func ParsePacket(ip []byte, tsNs int64) (Packet, bool) {
	p := Packet{TsNs: tsNs}
	if len(ip) < 20 {
		return p, false
	}
	var l4 int
	var src, dst netip.Addr
	switch ip[0] >> 4 {
	case 4:
		ihl := int(ip[0]&0x0f) * 4
		if ihl < 20 || binary.BigEndian.Uint16(ip[6:8])&0x1fff != 0 {
			return p, false
		}
		p.Protocol, p.L3Len, l4 = ip[9], int(binary.BigEndian.Uint16(ip[2:4])), ihl
		src, dst = netip.AddrFrom4([4]byte(ip[12:16])), netip.AddrFrom4([4]byte(ip[16:20]))
	case 6:
		if len(ip) < 40 {
			return p, false
		}
		// Extension headers are rare on the flows that matter and would
		// need more than HeaderBytes; such packets are skipped.
		p.Protocol, p.L3Len, l4 = ip[6], int(binary.BigEndian.Uint16(ip[4:6]))+40, 40
		src, dst = netip.AddrFrom16([16]byte(ip[8:24])), netip.AddrFrom16([16]byte(ip[24:40]))
	default:
		return p, false
	}
	if p.Protocol != ProtoTCP && p.Protocol != ProtoUDP || len(ip) < l4+4 || p.L3Len < l4 {
		return p, false
	}
	p.Src = netip.AddrPortFrom(src, binary.BigEndian.Uint16(ip[l4:l4+2]))
	p.Dst = netip.AddrPortFrom(dst, binary.BigEndian.Uint16(ip[l4+2:l4+4]))
	if p.Protocol == ProtoTCP && len(ip) > l4+13 {
		p.TCPFlags = ip[l4+13]
	}
	return p, true
}
