// Package runclock is the clock every process of a run shares: seconds
// since the run's start, on CLOCK_MONOTONIC, which is the same in every
// namespace on one machine.
package runclock

import (
	"syscall"
	"time"
	"unsafe"
)

const clockMonotonic = 1

// NowNs reads CLOCK_MONOTONIC in nanoseconds, the value Python's
// time.monotonic_ns returns.
func NowNs() int64 {
	var ts syscall.Timespec
	syscall.Syscall(syscall.SYS_CLOCK_GETTIME, clockMonotonic, uintptr(unsafe.Pointer(&ts)), 0)
	return ts.Sec*1e9 + ts.Nsec
}

// Clock measures from T0Ns, the monotonic time at which the run reads zero.
type Clock struct{ T0Ns int64 }

func (c Clock) NowS() float64 { return float64(NowNs()-c.T0Ns) / 1e9 }

// SleepUntil waits for run time t and reports false if stop closed first.
func (c Clock) SleepUntil(t float64, stop <-chan struct{}) bool {
	left := time.Duration((t - c.NowS()) * float64(time.Second))
	if left <= 0 {
		return true
	}
	select {
	case <-time.After(left):
		return true
	case <-stop:
		return false
	}
}
