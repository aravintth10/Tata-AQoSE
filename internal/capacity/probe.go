package capacity

import (
	"encoding/binary"
	"net"
	"sync"
	"time"

	"adaptiveqos/internal/runclock"
)

// Port is where reflectors listen.
const Port = 7077

// Round is what the probes showed over one interval, per reflector.
type Round struct {
	// RTTMs is the round-trip time of the newest answer that arrived in
	// the interval, when Answered is set.
	RTTMs    []float64
	Answered []bool
	// WaitingMs is how long the oldest probe still unanswered has been
	// out. An answer that is late is not an answer that is lost: while a
	// queue is building, this is the first sign of it, before any answer
	// comes back to be timed.
	WaitingMs []float64
	// SilentMs is how long it has been since any answer at all.
	SilentMs []float64
}

// Prober sends one small datagram to each reflector every Interval and
// reports once per interval.
type Prober struct {
	Reflectors []string // address:port
	Interval   time.Duration
	// Lost is how long a probe is waited for before it is given up on.
	// It is far longer than Interval: a link with a full buffer answers
	// late, and that lateness is the measurement.
	Lost time.Duration
}

type target struct {
	mu         sync.Mutex
	out        map[uint64]int64 // probes sent and not yet answered: sequence to send time
	rttMs      float64
	answered   bool
	lastAnswer int64
}

// Run probes until stop is closed, calling fn once per interval.
func (p Prober) Run(stop <-chan struct{}, fn func(Round)) error {
	conns := make([]net.Conn, len(p.Reflectors))
	targets := make([]*target, len(p.Reflectors))
	began := runclock.NowNs()
	for i, address := range p.Reflectors {
		conn, err := net.Dial("udp", address)
		if err != nil {
			return err
		}
		defer conn.Close()
		conns[i], targets[i] = conn, &target{out: map[uint64]int64{}, lastAnswer: began}
	}
	for i, conn := range conns {
		go func() {
			buf := make([]byte, 64)
			for {
				n, err := conn.Read(buf)
				if err != nil {
					select {
					case <-stop:
						return
					default:
						// A refused datagram (nothing listening yet) comes
						// back as an error on a connected socket. Keep going.
						time.Sleep(10 * time.Millisecond)
						continue
					}
				}
				now := runclock.NowNs()
				if n < 8 {
					continue
				}
				t := targets[i]
				t.mu.Lock()
				seq := binary.BigEndian.Uint64(buf)
				if sent, ok := t.out[seq]; ok {
					delete(t.out, seq)
					t.rttMs, t.answered, t.lastAnswer = float64(now-sent)/1e6, true, now
				}
				t.mu.Unlock()
			}
		}()
	}

	tick := time.NewTicker(p.Interval)
	defer tick.Stop()
	packet := make([]byte, 16)
	var seq uint64
	for {
		seq++
		binary.BigEndian.PutUint64(packet, seq)
		for i, conn := range conns {
			t := targets[i]
			t.mu.Lock()
			t.out[seq] = runclock.NowNs()
			t.mu.Unlock()
			conn.Write(packet)
		}
		select {
		case <-stop:
			return nil
		case <-tick.C:
		}
		now := runclock.NowNs()
		n := len(targets)
		round := Round{RTTMs: make([]float64, n), Answered: make([]bool, n),
			WaitingMs: make([]float64, n), SilentMs: make([]float64, n)}
		for i, t := range targets {
			t.mu.Lock()
			oldest := now
			for s, sent := range t.out {
				if now-sent > int64(p.Lost) {
					delete(t.out, s) // given up on
				} else if sent < oldest {
					oldest = sent
				}
			}
			round.RTTMs[i], round.Answered[i] = t.rttMs, t.answered
			round.WaitingMs[i] = float64(now-oldest) / 1e6
			round.SilentMs[i] = float64(now-t.lastAnswer) / 1e6
			t.answered = false
			t.mu.Unlock()
		}
		fn(round)
	}
}
