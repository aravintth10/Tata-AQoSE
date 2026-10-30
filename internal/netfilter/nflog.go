package netfilter

import (
	"encoding/binary"
	"errors"
	"fmt"
	"syscall"
	"time"
)

const (
	ulogPacket = 0
	ulogConfig = 1

	cfgCmd     = 1
	cfgMode    = 2
	cfgQThresh = 5

	cmdBind   = 1
	cmdPFBind = 3

	ulaTimestamp = 3
	ulaPayload   = 9

	copyPacket = 2
)

// LogPacket is one packet delivered by an nftables `log group` rule: its
// bytes from the IP header on, cut to the capture length.
type LogPacket struct {
	// TsNs is the kernel's receive time when it has one, else the time the
	// packet reached us. Either way it only serves to measure gaps.
	TsNs int64
	IP   []byte
}

// NFLog listens on one nflog group.
type NFLog struct {
	sock *socket
	// stamp keeps kernel packet timestamps switched on for as long as we
	// listen; without some socket asking, the kernel does not take them.
	stamp int
	buf   []byte
	// Overflows counts the times the kernel had to drop packets because we
	// read too slowly. Flows that lost packets then get no record.
	Overflows uint64
}

// ListenNFLog binds to an nflog group and asks for copyRange bytes of each
// packet, delivered at once rather than in batches.
func ListenNFLog(group uint16, copyRange uint32) (*NFLog, error) {
	sock, err := open(8 << 20)
	if err != nil {
		return nil, err
	}
	mode := make([]byte, 6)
	binary.BigEndian.PutUint32(mode[0:], copyRange)
	mode[4] = copyPacket
	one := make([]byte, 4)
	binary.BigEndian.PutUint32(one, 1)
	const flags = syscall.NLM_F_REQUEST | syscall.NLM_F_ACK
	steps := [][]byte{
		message(subsysULog, ulogConfig, flags, syscall.AF_INET, 0, attr(cfgCmd, []byte{cmdPFBind})),
		message(subsysULog, ulogConfig, flags, syscall.AF_INET6, 0, attr(cfgCmd, []byte{cmdPFBind})),
		message(subsysULog, ulogConfig, flags, syscall.AF_UNSPEC, group, attr(cfgCmd, []byte{cmdBind})),
		message(subsysULog, ulogConfig, flags, syscall.AF_UNSPEC, group, attr(cfgMode, mode)),
		message(subsysULog, ulogConfig, flags, syscall.AF_UNSPEC, group, attr(cfgQThresh, one)),
	}
	for i, step := range steps {
		if err := sock.ack(step); err != nil {
			sock.Close()
			return nil, fmt.Errorf("nflog group %d, setup step %d: %w", group, i+1, err)
		}
	}
	l := &NFLog{sock: sock, stamp: -1, buf: make([]byte, 1<<16)}
	if fd, err := syscall.Socket(syscall.AF_INET, syscall.SOCK_DGRAM|syscall.SOCK_CLOEXEC, 0); err == nil {
		syscall.SetsockoptInt(fd, syscall.SOL_SOCKET, syscall.SO_TIMESTAMPNS, 1)
		l.stamp = fd
	}
	syscall.SetsockoptTimeval(sock.fd, syscall.SOL_SOCKET, syscall.SO_RCVTIMEO, &syscall.Timeval{Usec: 200000})
	return l, nil
}

func (l *NFLog) Close() error {
	if l.stamp >= 0 {
		syscall.Close(l.stamp)
	}
	return l.sock.Close()
}

// Read waits up to 200 ms and calls fn for each packet that arrived. The
// IP slice is only valid during the call.
func (l *NFLog) Read(fn func(LogPacket)) error {
	n, _, err := syscall.Recvfrom(l.sock.fd, l.buf, 0)
	switch {
	case errors.Is(err, syscall.EAGAIN), errors.Is(err, syscall.EINTR):
		return nil
	case errors.Is(err, syscall.ENOBUFS):
		l.Overflows++
		return nil
	case err != nil:
		return err
	}
	arrived := time.Now().UnixNano()
	each(l.buf[:n], func(kind uint16, body []byte) bool {
		if kind != subsysULog<<8|ulogPacket || len(body) < 4 {
			return true
		}
		p := LogPacket{TsNs: arrived}
		attrs(body[4:], func(kind uint16, payload []byte) {
			switch {
			case kind == ulaPayload:
				p.IP = payload
			case kind == ulaTimestamp && len(payload) >= 16:
				sec, usec := binary.BigEndian.Uint64(payload[0:]), binary.BigEndian.Uint64(payload[8:])
				p.TsNs = int64(sec)*1e9 + int64(usec)*1e3
			}
		})
		if p.IP != nil {
			fn(p)
		}
		return true
	})
	return nil
}
