package extract

import (
	"bufio"
	"encoding/binary"
	"fmt"
	"io"
	"os"
)

// Link types whose frames carry an IP packet at a fixed offset.
var linkIPOffset = map[uint32]int{101: 0, 12: 0, 228: 0, 229: 0, 113: 16, 276: 20}

// ipOffset finds the IP header in a link-layer frame, or -1.
func ipOffset(link uint32, frame []byte) int {
	if link == 1 {
		if len(frame) < 14 {
			return -1
		}
		offset, ethertype := 14, binary.BigEndian.Uint16(frame[12:14])
		for (ethertype == 0x8100 || ethertype == 0x88a8) && len(frame) >= offset+4 { // VLAN tags
			ethertype, offset = binary.BigEndian.Uint16(frame[offset+2:offset+4]), offset+4
		}
		if ethertype != 0x0800 && ethertype != 0x86dd {
			return -1
		}
		return offset
	}
	if offset, ok := linkIPOffset[link]; ok {
		return offset
	}
	return -1
}

// ReadPcap calls fn with every IP packet of a pcap or pcapng file: its
// timestamp in nanoseconds and its bytes from the IP header on, as captured.
func ReadPcap(path string, fn func(tsNs int64, ip []byte)) error {
	file, err := os.Open(path)
	if err != nil {
		return err
	}
	defer file.Close()
	r := bufio.NewReaderSize(file, 1<<20)
	magic, err := r.Peek(4)
	if err != nil {
		return fmt.Errorf("%s: %w", path, err)
	}
	emit := func(tsNs int64, link uint32, frame []byte) {
		if offset := ipOffset(link, frame); offset >= 0 && len(frame) > offset {
			fn(tsNs, frame[offset:])
		}
	}
	switch string(magic) {
	case "\x0a\x0d\x0d\x0a":
		return readPcapNG(r, emit)
	case "\xd4\xc3\xb2\xa1":
		return readClassic(r, binary.LittleEndian, 1000, emit)
	case "\x4d\x3c\xb2\xa1":
		return readClassic(r, binary.LittleEndian, 1, emit)
	case "\xa1\xb2\xc3\xd4":
		return readClassic(r, binary.BigEndian, 1000, emit)
	case "\xa1\xb2\x3c\x4d":
		return readClassic(r, binary.BigEndian, 1, emit)
	}
	return fmt.Errorf("%s: not a pcap or pcapng file", path)
}

func readClassic(r io.Reader, order binary.ByteOrder, nsPerUnit int64, emit func(int64, uint32, []byte)) error {
	head := make([]byte, 24)
	if _, err := io.ReadFull(r, head); err != nil {
		return err
	}
	link := order.Uint32(head[20:24]) & 0xffff
	record := make([]byte, 16)
	var frame []byte
	for {
		if _, err := io.ReadFull(r, record); err != nil {
			return nil // end of file, or a capture cut short
		}
		caplen := int(order.Uint32(record[8:12]))
		if caplen > 1<<20 {
			return fmt.Errorf("corrupt capture: packet of %d bytes", caplen)
		}
		if cap(frame) < caplen {
			frame = make([]byte, caplen)
		}
		frame = frame[:caplen]
		if _, err := io.ReadFull(r, frame); err != nil {
			return nil
		}
		ts := int64(order.Uint32(record[0:4]))*1e9 + int64(order.Uint32(record[4:8]))*nsPerUnit
		emit(ts, link, frame)
	}
}

func readPcapNG(r io.Reader, emit func(int64, uint32, []byte)) error {
	type iface struct {
		link      uint32
		unitsPerS uint64
	}
	var order binary.ByteOrder = binary.LittleEndian
	var ifaces []iface
	head := make([]byte, 8)
	for {
		if _, err := io.ReadFull(r, head); err != nil {
			return nil
		}
		if string(head[:4]) == "\x0a\x0d\x0d\x0a" { // section header: sets the byte order
			magic := make([]byte, 4)
			if _, err := io.ReadFull(r, magic); err != nil {
				return nil
			}
			order = binary.LittleEndian
			if string(magic) == "\x1a\x2b\x3c\x4d" {
				order = binary.BigEndian
			}
			length := int(order.Uint32(head[4:8]))
			if length < 12 || length > 1<<24 {
				return fmt.Errorf("corrupt pcapng section header")
			}
			if _, err := io.CopyN(io.Discard, r, int64(length-12)); err != nil {
				return nil
			}
			ifaces = nil
			continue
		}
		kind, length := order.Uint32(head[0:4]), int(order.Uint32(head[4:8]))
		if length < 12 || length > 1<<24 {
			return fmt.Errorf("corrupt pcapng block of %d bytes", length)
		}
		body := make([]byte, length-8)
		if _, err := io.ReadFull(r, body); err != nil {
			return nil
		}
		switch {
		case kind == 1 && len(body) >= 8: // interface description
			it := iface{link: uint32(order.Uint16(body[0:2])), unitsPerS: 1e6}
			for pos := 8; pos+4 <= len(body)-4; {
				code, size := order.Uint16(body[pos:pos+2]), int(order.Uint16(body[pos+2:pos+4]))
				if code == 0 {
					break
				}
				if code == 9 && size == 1 && pos+4 < len(body) { // if_tsresol
					v := body[pos+4]
					it.unitsPerS = 1
					for i := 0; i < int(v&0x7f); i++ {
						if v&0x80 != 0 {
							it.unitsPerS *= 2
						} else {
							it.unitsPerS *= 10
						}
					}
				}
				pos += 4 + (size+3)/4*4
			}
			ifaces = append(ifaces, it)
		case kind == 6 && len(body) >= 20 && len(ifaces) > 0: // enhanced packet
			id := int(order.Uint32(body[0:4]))
			if id >= len(ifaces) {
				id = 0
			}
			caplen := int(order.Uint32(body[12:16]))
			if 20+caplen > len(body) {
				continue
			}
			units := uint64(order.Uint32(body[4:8]))<<32 | uint64(order.Uint32(body[8:12]))
			per := ifaces[id].unitsPerS
			ts := int64(units/per)*1e9 + int64(units%per)*1e9/int64(per)
			emit(ts, ifaces[id].link, body[20:20+caplen])
		}
	}
}
