package testbed

import (
	"encoding/json"
	"fmt"
	"io"
	"net"
	"os/exec"
	"time"
)

const (
	// Helper subcommands the rate check runs inside the namespaces.
	ServeCommand = "_serve"
	LoadCommand  = "_load"

	ratePort = "5999"
	// RateTolerancePct is the build plan's bound on the bottleneck error.
	RateTolerancePct = 3.0
	rampUp           = 1500 * time.Millisecond
	// drain lets the bottleneck FIFO empty before the other direction's
	// handshake has to cross it.
	drain = bufferMs * time.Millisecond
)

// RateResult is one direction of the bottleneck check.
type RateResult struct {
	Direction      string
	ConfiguredKbit int
	MeasuredKbit   float64
	ErrorPct       float64
}

func (r RateResult) OK() bool {
	return r.ErrorPct <= RateTolerancePct && r.ErrorPct >= -RateTolerancePct
}

// CheckRate saturates the link with one greedy TCP flow per direction and
// compares what the bottleneck interface transmitted with the configured
// rate. Both count whole frames, as the token bucket does, so the figure is
// the rate on the wire and not TCP goodput.
func (s *State) CheckRate(self string, measure time.Duration) ([]RateResult, error) {
	if s.Link.LossPct > 0 {
		return nil, fmt.Errorf("check-rate needs a link without loss: TCP cannot fill a lossy link")
	}
	start := func(ns string, argv ...string) (*exec.Cmd, error) {
		full := nsenterArgv(s.Holders[ns], append([]string{self}, argv...))
		cmd := exec.Command(full[0], full[1:]...)
		return cmd, cmd.Start()
	}
	server, err := start(NSServer, ServeCommand, net.JoinHostPort(s.Server4, ratePort))
	if err != nil {
		return nil, err
	}
	defer func() { server.Process.Kill(); server.Wait() }()
	time.Sleep(200 * time.Millisecond)

	host := s.Hosts[0]
	var results []RateResult
	for _, dir := range []struct {
		name, dev string
		kbit      int
	}{{"up", IfISPUp, s.Link.UpKbit}, {"down", IfISPDown, s.Link.DownKbit}} {
		// A sender remembers the last connection's slow-start threshold per
		// destination and would then take many seconds to fill the link.
		for _, ns := range []string{host.NS, NSServer} {
			run(s.Holders[ns], "ip", "tcp_metrics", "flush")
		}
		load, err := start(host.NS, LoadCommand, net.JoinHostPort(s.Server4, ratePort), dir.name)
		if err != nil {
			return nil, err
		}
		time.Sleep(rampUp)
		b0, t0, err0 := s.txBytes(dir.dev)
		time.Sleep(measure)
		b1, t1, err1 := s.txBytes(dir.dev)
		load.Process.Kill()
		load.Wait()
		time.Sleep(drain)
		if err0 != nil || err1 != nil {
			return nil, fmt.Errorf("read %s counters: %v %v", dir.dev, err0, err1)
		}
		kbit := float64(b1-b0) * 8 / 1000 / t1.Sub(t0).Seconds()
		results = append(results, RateResult{
			Direction: dir.name, ConfiguredKbit: dir.kbit, MeasuredKbit: kbit,
			ErrorPct: (kbit/float64(dir.kbit) - 1) * 100,
		})
	}
	return results, nil
}

// txBytes reads an isp interface's transmit counter. The timestamp is the
// middle of the read, which takes a few milliseconds.
func (s *State) txBytes(dev string) (uint64, time.Time, error) {
	before := time.Now()
	out, err := run(s.Holders[NSISP], "ip", "-j", "-s", "link", "show", "dev", dev)
	at := before.Add(time.Since(before) / 2)
	if err != nil {
		return 0, at, err
	}
	var links []struct {
		Stats struct {
			TX struct {
				Bytes uint64 `json:"bytes"`
			} `json:"tx"`
		} `json:"stats64"`
	}
	if err := json.Unmarshal(out, &links); err != nil || len(links) != 1 {
		return 0, at, fmt.Errorf("unexpected output from ip: %v", err)
	}
	return links[0].Stats.TX.Bytes, at, nil
}

// Serve is the far end of the rate check: each connection names a
// direction in its first byte, then data flows that way until it closes.
func Serve(addr string) error {
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		return err
	}
	for {
		conn, err := ln.Accept()
		if err != nil {
			return err
		}
		go func() {
			defer conn.Close()
			mode := make([]byte, 1)
			if _, err := io.ReadFull(conn, mode); err != nil {
				return
			}
			if mode[0] == 'd' {
				flood(conn)
			} else {
				io.Copy(io.Discard, conn)
			}
		}()
	}
}

// LoadLink is the LAN end of the rate check. It runs until killed.
func LoadLink(addr, direction string) error {
	conn, err := net.Dial("tcp", addr)
	if err != nil {
		return err
	}
	defer conn.Close()
	if direction == "down" {
		if _, err := conn.Write([]byte{'d'}); err != nil {
			return err
		}
		_, err = io.Copy(io.Discard, conn)
		return err
	}
	if _, err := conn.Write([]byte{'u'}); err != nil {
		return err
	}
	return flood(conn)
}

func flood(conn net.Conn) error {
	buf := make([]byte, 64<<10)
	for {
		if _, err := conn.Write(buf); err != nil {
			return err
		}
	}
}
