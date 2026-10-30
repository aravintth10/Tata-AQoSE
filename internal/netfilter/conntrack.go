package netfilter

import (
	"encoding/binary"
	"errors"
	"fmt"
	"net/netip"
	"syscall"
)

const (
	ctNew = 0
	ctGet = 1

	ctaTupleOrig     = 1
	ctaMark          = 8
	ctaCountersOrig  = 9
	ctaCountersReply = 10
	ctaMarkMask      = 21

	ctaTupleIP    = 1
	ctaTupleProto = 2
	ctaIPv4Src    = 1
	ctaIPv4Dst    = 2
	ctaIPv6Src    = 3
	ctaIPv6Dst    = 4
	ctaProtoNum   = 1
	ctaProtoSrc   = 2
	ctaProtoDst   = 3

	ctaCountersPackets = 1
	ctaCountersBytes   = 2
)

// Entry is one conntrack entry: the flow as its first packet named it,
// what each direction has carried, and its mark.
type Entry struct {
	Protocol              uint8
	Src, Dst              netip.AddrPort // original direction
	OrigPkts, OrigBytes   uint64
	ReplyPkts, ReplyBytes uint64
	Mark                  uint32
}

// Conntrack reads and updates the kernel's connection table.
type Conntrack struct {
	sock *socket
	buf  []byte
}

func OpenConntrack() (*Conntrack, error) {
	sock, err := open(4 << 20)
	if err != nil {
		return nil, err
	}
	return &Conntrack{sock: sock, buf: make([]byte, 1<<18)}, nil
}

func (c *Conntrack) Close() error { return c.sock.Close() }

// Dump calls fn for every TCP and UDP entry. Counters are zero unless
// net.netfilter.nf_conntrack_acct was on when the entry was created.
func (c *Conntrack) Dump(fn func(Entry)) error {
	const flags = syscall.NLM_F_REQUEST | syscall.NLM_F_DUMP
	if err := c.sock.send(message(subsysConntrack, ctGet, flags, syscall.AF_UNSPEC, 0)); err != nil {
		return err
	}
	for {
		n, _, err := syscall.Recvfrom(c.sock.fd, c.buf, 0)
		if err != nil {
			return fmt.Errorf("conntrack dump: %w", err)
		}
		done := false
		var failed error
		each(c.buf[:n], func(kind uint16, body []byte) bool {
			switch {
			case kind == syscall.NLMSG_DONE:
				done = true
				return false
			case kind == syscall.NLMSG_ERROR:
				if len(body) >= 4 && native.Uint32(body[0:]) != 0 {
					failed = syscall.Errno(-int32(native.Uint32(body[0:])))
				}
				done = true
				return false
			case kind == subsysConntrack<<8|ctNew && len(body) >= 4:
				if e, ok := parseEntry(body[4:]); ok {
					fn(e)
				}
			}
			return true
		})
		if done {
			return failed
		}
	}
}

func parseEntry(b []byte) (Entry, bool) {
	var e Entry
	counters := func(payload []byte, pkts, bytes *uint64) {
		attrs(payload, func(kind uint16, v []byte) {
			if len(v) < 8 {
				return
			}
			switch kind {
			case ctaCountersPackets:
				*pkts = binary.BigEndian.Uint64(v)
			case ctaCountersBytes:
				*bytes = binary.BigEndian.Uint64(v)
			}
		})
	}
	attrs(b, func(kind uint16, payload []byte) {
		switch kind {
		case ctaTupleOrig:
			e.Protocol, e.Src, e.Dst = parseTuple(payload)
		case ctaCountersOrig:
			counters(payload, &e.OrigPkts, &e.OrigBytes)
		case ctaCountersReply:
			counters(payload, &e.ReplyPkts, &e.ReplyBytes)
		case ctaMark:
			if len(payload) >= 4 {
				e.Mark = binary.BigEndian.Uint32(payload)
			}
		}
	})
	return e, (e.Protocol == syscall.IPPROTO_TCP || e.Protocol == syscall.IPPROTO_UDP) && e.Src.IsValid()
}

func parseTuple(b []byte) (protocol uint8, src, dst netip.AddrPort) {
	var srcAddr, dstAddr netip.Addr
	var srcPort, dstPort uint16
	attrs(b, func(kind uint16, payload []byte) {
		switch kind {
		case ctaTupleIP:
			attrs(payload, func(kind uint16, v []byte) {
				addr, ok := netip.AddrFromSlice(v)
				if !ok {
					return
				}
				switch kind {
				case ctaIPv4Src, ctaIPv6Src:
					srcAddr = addr
				case ctaIPv4Dst, ctaIPv6Dst:
					dstAddr = addr
				}
			})
		case ctaTupleProto:
			attrs(payload, func(kind uint16, v []byte) {
				switch {
				case kind == ctaProtoNum && len(v) >= 1:
					protocol = v[0]
				case kind == ctaProtoSrc && len(v) >= 2:
					srcPort = binary.BigEndian.Uint16(v)
				case kind == ctaProtoDst && len(v) >= 2:
					dstPort = binary.BigEndian.Uint16(v)
				}
			})
		}
	})
	if !srcAddr.IsValid() || !dstAddr.IsValid() {
		return 0, src, dst
	}
	return protocol, netip.AddrPortFrom(srcAddr, srcPort), netip.AddrPortFrom(dstAddr, dstPort)
}

func tuple(protocol uint8, src, dst netip.AddrPort) []byte {
	srcKind, dstKind := uint16(ctaIPv4Src), uint16(ctaIPv4Dst)
	if src.Addr().Is6() {
		srcKind, dstKind = ctaIPv6Src, ctaIPv6Dst
	}
	port := func(p uint16) []byte { return binary.BigEndian.AppendUint16(nil, p) }
	return nested(ctaTupleOrig,
		nested(ctaTupleIP, attr(srcKind, src.Addr().AsSlice()), attr(dstKind, dst.Addr().AsSlice())),
		nested(ctaTupleProto, attr(ctaProtoNum, []byte{protocol}),
			attr(ctaProtoSrc, port(src.Port())), attr(ctaProtoDst, port(dst.Port()))))
}

// SetMark changes the bits of an existing entry's mark that mask selects,
// and leaves the others alone. The entry is named by its original
// direction. It never creates one: for a flow that has ended it returns
// ErrNoEntry.
func (c *Conntrack) SetMark(protocol uint8, src, dst netip.AddrPort, mark, mask uint32) error {
	// The kernel computes (old AND NOT mask) XOR mark.
	msg := message(subsysConntrack, ctNew, syscall.NLM_F_REQUEST|syscall.NLM_F_ACK, family(src), 0,
		tuple(protocol, src, dst),
		attr(ctaMark, binary.BigEndian.AppendUint32(nil, mark&mask)),
		attr(ctaMarkMask, binary.BigEndian.AppendUint32(nil, mask)))
	return noEntry(c.sock.ack(msg))
}

// ErrNoEntry means conntrack has no such flow: it ended, or never was.
var ErrNoEntry = errors.New("no such conntrack entry")

func noEntry(err error) error {
	if errors.Is(err, syscall.ENOENT) {
		return ErrNoEntry
	}
	return err
}

func family(addr netip.AddrPort) uint8 {
	if addr.Addr().Is6() {
		return syscall.AF_INET6
	}
	return syscall.AF_INET
}

// Get reads one entry, named by its original direction.
func (c *Conntrack) Get(protocol uint8, src, dst netip.AddrPort) (Entry, error) {
	msg := message(subsysConntrack, ctGet, syscall.NLM_F_REQUEST|syscall.NLM_F_ACK, family(src), 0,
		tuple(protocol, src, dst))
	if err := c.sock.send(msg); err != nil {
		return Entry{}, err
	}
	var entry Entry
	found := false
	for {
		n, _, err := syscall.Recvfrom(c.sock.fd, c.buf, 0)
		if err != nil {
			return entry, fmt.Errorf("conntrack get: %w", err)
		}
		done := false
		var failed error
		each(c.buf[:n], func(kind uint16, body []byte) bool {
			switch {
			case kind == syscall.NLMSG_ERROR:
				if len(body) >= 4 && native.Uint32(body[0:]) != 0 {
					failed = noEntry(syscall.Errno(-int32(native.Uint32(body[0:]))))
				}
				done = true
				return false
			case kind == subsysConntrack<<8|ctNew && len(body) >= 4:
				entry, found = parseEntry(body[4:])
			}
			return true
		})
		if done {
			if failed == nil && !found {
				failed = ErrNoEntry
			}
			return entry, failed
		}
	}
}
