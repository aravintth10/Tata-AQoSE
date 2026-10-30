package capacity

import (
	"net"
	"sync"
	"testing"
	"time"
)

// echo is a reflector on localhost that can be made slow or silent.
func echo(t *testing.T, delay *time.Duration, silent *bool, mu *sync.Mutex) string {
	t.Helper()
	conn, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	go func() {
		buf := make([]byte, 64)
		for {
			n, from, err := conn.ReadFrom(buf)
			if err != nil {
				return
			}
			mu.Lock()
			wait, quiet := *delay, *silent
			mu.Unlock()
			if quiet {
				continue
			}
			packet := append([]byte(nil), buf[:n]...)
			go func() { time.Sleep(wait); conn.WriteTo(packet, from) }()
		}
	}()
	return conn.LocalAddr().String()
}

func TestProberTellsLateFromSilent(t *testing.T) {
	var mu sync.Mutex
	delay, silent, never := 5*time.Millisecond, false, false
	zero := time.Duration(0)
	addresses := []string{echo(t, &delay, &silent, &mu), echo(t, &zero, &never, &mu)}
	rounds := make(chan Round, 1000)
	stop := make(chan struct{})
	go Prober{Reflectors: addresses, Interval: 30 * time.Millisecond, Lost: time.Second}.Run(stop, func(r Round) { rounds <- r })
	defer close(stop)

	take := func() Round {
		select {
		case r := <-rounds:
			return r
		case <-time.After(2 * time.Second):
			t.Fatal("no round")
			return Round{}
		}
	}
	take() // the first round may race the sockets
	r := take()
	if !r.Answered[0] || !r.Answered[1] || r.RTTMs[0] < 5 || r.RTTMs[0] > 20 || r.RTTMs[1] > 5 {
		t.Fatalf("round: %+v", r)
	}
	// Answers four intervals late are still answers, and are timed.
	mu.Lock()
	delay = 120 * time.Millisecond
	mu.Unlock()
	late := false
	for i := 0; i < 12; i++ {
		r = take()
		if r.Answered[0] && r.RTTMs[0] > 110 {
			late = true
		}
		if r.SilentMs[1] > 60 {
			t.Fatalf("the quick reflector looks silent: %+v", r)
		}
	}
	if !late {
		t.Fatalf("a 120 ms answer was never timed: %+v", r)
	}
	if r.WaitingMs[0] < 60 {
		t.Fatalf("probes are out for 120 ms, yet waiting is %v", r.WaitingMs[0])
	}
	// Nothing at all coming back shows as silence growing.
	mu.Lock()
	silent = true
	mu.Unlock()
	for i := 0; i < 15; i++ {
		r = take()
	}
	if r.Answered[0] || r.SilentMs[0] < 250 || !r.Answered[1] {
		t.Fatalf("round with one reflector silent: %+v", r)
	}
}
