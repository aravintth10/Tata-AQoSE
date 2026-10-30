// Package netfilter speaks the two netfilter netlink protocols the
// controller needs, with the standard library only: nflog, which delivers
// the first packets of each flow, and conntrack, which holds each flow's
// counters and mark.
package netfilter

import (
	"encoding/binary"
	"fmt"
	"syscall"
)

const (
	subsysConntrack = 1
	subsysULog      = 4
	nlaNested       = 0x8000
	nlaTypeMask     = 0x3fff
)

var native = binary.NativeEndian

// attr encodes one netlink attribute, padded to four bytes.
func attr(kind uint16, payload []byte) []byte {
	out := make([]byte, 4, 4+(len(payload)+3)&^3)
	native.PutUint16(out[0:], uint16(4+len(payload)))
	native.PutUint16(out[2:], kind)
	out = append(out, payload...)
	for len(out)%4 != 0 {
		out = append(out, 0)
	}
	return out
}

func nested(kind uint16, parts ...[]byte) []byte {
	var body []byte
	for _, p := range parts {
		body = append(body, p...)
	}
	return attr(kind|nlaNested, body)
}

// attrs calls fn for each attribute in b.
func attrs(b []byte, fn func(kind uint16, payload []byte)) {
	for len(b) >= 4 {
		length := int(native.Uint16(b[0:]))
		if length < 4 || length > len(b) {
			return
		}
		fn(native.Uint16(b[2:])&nlaTypeMask, b[4:length])
		b = b[min((length+3)&^3, len(b)):]
	}
}

// message builds a netfilter netlink message: header, nfgenmsg, attributes.
func message(subsys, msg uint8, flags uint16, family uint8, resID uint16, body ...[]byte) []byte {
	out := make([]byte, 20)
	native.PutUint16(out[4:], uint16(subsys)<<8|uint16(msg))
	native.PutUint16(out[6:], flags)
	out[16] = family
	binary.BigEndian.PutUint16(out[18:], resID)
	for _, b := range body {
		out = append(out, b...)
	}
	native.PutUint32(out[0:], uint32(len(out)))
	return out
}

type socket struct{ fd int }

func open(rcvbuf int) (*socket, error) {
	fd, err := syscall.Socket(syscall.AF_NETLINK, syscall.SOCK_RAW|syscall.SOCK_CLOEXEC, syscall.NETLINK_NETFILTER)
	if err != nil {
		return nil, fmt.Errorf("netlink socket: %w", err)
	}
	if err := syscall.Bind(fd, &syscall.SockaddrNetlink{Family: syscall.AF_NETLINK}); err != nil {
		syscall.Close(fd)
		return nil, fmt.Errorf("netlink bind: %w", err)
	}
	if rcvbuf > 0 {
		syscall.SetsockoptInt(fd, syscall.SOL_SOCKET, syscall.SO_RCVBUF, rcvbuf)
	}
	return &socket{fd}, nil
}

func (s *socket) Close() error { return syscall.Close(s.fd) }

func (s *socket) send(msg []byte) error {
	return syscall.Sendto(s.fd, msg, 0, &syscall.SockaddrNetlink{Family: syscall.AF_NETLINK})
}

// each calls fn for every netlink message in one datagram. fn returns false
// to stop.
func each(datagram []byte, fn func(kind uint16, body []byte) bool) {
	for len(datagram) >= 16 {
		length := int(native.Uint32(datagram[0:]))
		if length < 16 || length > len(datagram) {
			return
		}
		if !fn(native.Uint16(datagram[4:]), datagram[16:length]) {
			return
		}
		datagram = datagram[min((length+3)&^3, len(datagram)):]
	}
}

// ack sends a request and waits for the kernel's verdict on it.
func (s *socket) ack(msg []byte) error {
	if err := s.send(msg); err != nil {
		return err
	}
	buf := make([]byte, 8192)
	for {
		n, _, err := syscall.Recvfrom(s.fd, buf, 0)
		if err != nil {
			return err
		}
		var verdict error
		done := false
		each(buf[:n], func(kind uint16, body []byte) bool {
			if kind == syscall.NLMSG_ERROR && len(body) >= 4 {
				if errno := int32(native.Uint32(body[0:])); errno != 0 {
					verdict = syscall.Errno(-errno)
				}
				done = true
				return false
			}
			return true
		})
		if done {
			return verdict
		}
	}
}
