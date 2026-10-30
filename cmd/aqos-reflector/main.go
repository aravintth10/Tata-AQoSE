// Command aqos-reflector answers the capacity estimator's delay probes: it
// sends every UDP datagram straight back to where it came from. It runs in
// the testbed's server namespace, one socket per reflector address.
//
//	aqos-reflector 10.79.0.1:7077 10.79.0.2:7077 10.79.0.3:7077
package main

import (
	"fmt"
	"net"
	"os"
	"os/signal"
	"syscall"
)

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "usage: aqos-reflector <address:port>...")
		os.Exit(2)
	}
	for _, address := range os.Args[1:] {
		conn, err := net.ListenPacket("udp", address)
		if err != nil {
			fmt.Fprintln(os.Stderr, "aqos-reflector:", err)
			os.Exit(1)
		}
		go func() {
			buf := make([]byte, 2048)
			for {
				n, from, err := conn.ReadFrom(buf)
				if err != nil {
					return
				}
				conn.WriteTo(buf[:n], from)
			}
		}()
	}
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGTERM, syscall.SIGINT)
	<-sig
}
