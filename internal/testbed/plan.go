// Package testbed builds the emulated home network of chunk 2A out of
// network namespaces, from a scenario file.
package testbed

import (
	"fmt"
	"strconv"

	"adaptiveqos/contracts"
)

// Prefix is carried by every namespace holder so teardown can find
// leftovers from a crashed run.
const Prefix = "aqos-"

// Fixed namespaces. LAN hosts are HostNS(name).
const (
	NSUser   = Prefix + "userns"
	NSRouter = Prefix + "router"
	NSISP    = Prefix + "isp"
	NSCore   = Prefix + "core"
	NSServer = Prefix + "server"
)

// Interface names. The bottleneck is IfISPUp and IfISPDown, both in isp.
const (
	IfBridge    = "br-lan"
	IfWAN       = "wan0"  // router, toward isp
	IfISPDown   = "rtr0"  // isp, toward router: download egress
	IfISPUp     = "core0" // isp, toward core: upload egress
	IfCoreDown  = "isp0"  // core, toward isp
	IfCoreUp    = "srv0"  // core, toward server
	IfHost      = "eth0"
	IfServer    = "eth0"
	maxHosts    = 200
	bufferMs    = 500   // bottleneck FIFO depth at the link rate; starting value
	netemLimit  = 20000 // packets
	minBurst    = 3028  // two full frames
	minTBFLimit = 16384
)

const (
	RouterLAN4 = "10.77.0.1"
	RouterLAN6 = "fd77:0:0:1::1"
	RouterWAN4 = "10.78.0.1"
	RouterWAN6 = "fd77:0:0:2::1"
	ISPDown4   = "10.78.0.2"
	ISPDown6   = "fd77:0:0:2::2"
	ISPUp4     = "10.78.0.5"
	ISPUp6     = "fd77:0:0:3::1"
	CoreDown4  = "10.78.0.6"
	CoreDown6  = "fd77:0:0:3::2"
	CoreUp4    = "10.78.0.9"
	CoreUp6    = "fd77:0:0:4::1"
	Server4    = "10.78.0.10"
	Server6    = "fd77:0:0:4::2"
)

// Reflectors4 are the addresses delay probes are sent to, and
// ReflectorNet4 the prefix that holds them.
var Reflectors4 = []string{"10.79.0.1", "10.79.0.2", "10.79.0.3"}

const ReflectorNet4 = "10.79.0.0/29"

func HostNS(name string) string { return Prefix + "host-" + name }

// Host is one LAN host and its addresses.
type Host struct {
	Name string `json:"name"`
	NS   string `json:"ns"`
	IP4  string `json:"ip4"`
	IP6  string `json:"ip6"`
}

// HostAddrs gives host i (from 0) its LAN addresses.
func HostAddrs(i int) (ip4, ip6 string) {
	return fmt.Sprintf("10.77.0.%d", 10+i), fmt.Sprintf("fd77:0:0:1::%x", 0x10+i)
}

func planHosts(s *contracts.Scenario) ([]Host, error) {
	if len(s.Hosts) == 0 {
		return nil, fmt.Errorf("scenario has no hosts")
	}
	if len(s.Hosts) > maxHosts {
		return nil, fmt.Errorf("scenario has %d hosts, the LAN holds %d", len(s.Hosts), maxHosts)
	}
	seen := map[string]bool{}
	hosts := make([]Host, len(s.Hosts))
	for i, name := range s.Hosts {
		if seen[name] {
			return nil, fmt.Errorf("host %q listed twice", name)
		}
		seen[name] = true
		ip4, ip6 := HostAddrs(i)
		hosts[i] = Host{Name: name, NS: HostNS(name), IP4: ip4, IP6: ip6}
	}
	return hosts, nil
}

func checkLink(l contracts.Link) error {
	switch {
	case l.DownKbit < 1 || l.UpKbit < 1:
		return fmt.Errorf("link rates must be at least 1 kbit")
	case l.DelayMs < 0 || l.JitterMs < 0:
		return fmt.Errorf("link delay and jitter cannot be negative")
	case l.LossPct < 0 || l.LossPct > 100:
		return fmt.Errorf("link loss must be between 0 and 100 percent")
	}
	return nil
}

// tbfArgs is the bottleneck token bucket for one direction: a small burst
// so the rate is honest, and a FIFO worth bufferMs at the link rate.
func tbfArgs(rateKbit int) []string {
	bytesPerS := rateKbit * 1000 / 8
	burst := max(bytesPerS/1000, minBurst)
	limit := max(bytesPerS*bufferMs/1000, minTBFLimit)
	return []string{"tbf", "rate", strconv.Itoa(rateKbit) + "kbit",
		"burst", strconv.Itoa(burst), "limit", strconv.Itoa(limit)}
}

// netemArgs is the delay, jitter and loss applied in each direction.
func netemArgs(l contracts.Link) []string {
	args := []string{"netem", "limit", strconv.Itoa(netemLimit), "delay", ms(l.DelayMs)}
	if l.JitterMs > 0 {
		args = append(args, ms(l.JitterMs))
	}
	if l.LossPct > 0 {
		args = append(args, "loss", strconv.FormatFloat(l.LossPct, 'f', -1, 64)+"%")
	}
	return args
}

func ms(v float64) string { return strconv.FormatFloat(v, 'f', -1, 64) + "ms" }

// step is one command run inside one namespace.
type step struct {
	ns   string
	argv []string
}

func in(ns string, argv ...string) step { return step{ns, argv} }

// buildSteps is everything `up` runs once the namespaces exist. pid maps a
// namespace name to its holder, which is how veth peers are moved.
func buildSteps(hosts []Host, link contracts.Link, pid map[string]int) []step {
	var st []step
	ip := func(ns string, args ...string) { st = append(st, in(ns, append([]string{"ip"}, args...)...)) }
	addr := func(ns, dev, a4, a6 string) {
		ip(ns, "addr", "add", a4, "dev", dev)
		ip(ns, "-6", "addr", "add", a6, "dev", dev, "nodad")
		ip(ns, "link", "set", dev, "up")
	}
	veth := func(ns, dev, peerNS, peerDev string) {
		ip(ns, "link", "add", dev, "type", "veth", "peer", "name", peerDev, "netns", strconv.Itoa(pid[peerNS]))
		// Segmentation offload would hand the router packets larger than
		// anything a real link carries, and distort first-packet sizes.
		st = append(st, in(ns, "ethtool", "-K", dev, "tso", "off", "gso", "off", "gro", "off"))
		st = append(st, in(peerNS, "ethtool", "-K", peerDev, "tso", "off", "gso", "off", "gro", "off"))
	}

	all := []string{NSRouter, NSISP, NSCore, NSServer}
	for _, h := range hosts {
		all = append(all, h.NS)
	}
	for _, ns := range all {
		ip(ns, "link", "set", "lo", "up")
	}

	// LAN: one bridge in the router, one veth per host.
	ip(NSRouter, "link", "add", IfBridge, "type", "bridge", "forward_delay", "0", "mcast_snooping", "0")
	addr(NSRouter, IfBridge, RouterLAN4+"/24", RouterLAN6+"/64")
	for i, h := range hosts {
		port := "lan-" + strconv.Itoa(i)
		veth(NSRouter, port, h.NS, IfHost)
		ip(NSRouter, "link", "set", port, "master", IfBridge, "up")
		addr(h.NS, IfHost, h.IP4+"/24", h.IP6+"/64")
		ip(h.NS, "route", "add", "default", "via", RouterLAN4)
		ip(h.NS, "-6", "route", "add", "default", "via", RouterLAN6)
	}

	// WAN path: router, isp (rate), core (delay), server.
	veth(NSRouter, IfWAN, NSISP, IfISPDown)
	addr(NSRouter, IfWAN, RouterWAN4+"/30", RouterWAN6+"/64")
	addr(NSISP, IfISPDown, ISPDown4+"/30", ISPDown6+"/64")
	veth(NSISP, IfISPUp, NSCore, IfCoreDown)
	addr(NSISP, IfISPUp, ISPUp4+"/30", ISPUp6+"/64")
	addr(NSCore, IfCoreDown, CoreDown4+"/30", CoreDown6+"/64")
	veth(NSCore, IfCoreUp, NSServer, IfServer)
	addr(NSCore, IfCoreUp, CoreUp4+"/30", CoreUp6+"/64")
	addr(NSServer, IfServer, Server4+"/30", Server6+"/64")

	for _, ns := range []string{NSRouter, NSISP, NSCore} {
		st = append(st, in(ns, "sysctl", "-qw", "net.ipv4.ip_forward=1", "net.ipv6.conf.all.forwarding=1"))
	}

	// IPv4 leaves the router masqueraded, so only IPv6 needs routes back to
	// the LAN prefix.
	ip(NSRouter, "route", "add", "default", "via", ISPDown4)
	ip(NSRouter, "-6", "route", "add", "default", "via", ISPDown6)
	ip(NSISP, "route", "add", "10.78.0.8/30", "via", CoreDown4)
	ip(NSISP, "-6", "route", "add", "fd77:0:0:4::/64", "via", CoreDown6)
	ip(NSISP, "-6", "route", "add", "fd77:0:0:1::/64", "via", RouterWAN6)
	ip(NSCore, "route", "add", "10.78.0.0/30", "via", ISPUp4)
	ip(NSCore, "-6", "route", "add", "fd77:0:0:1::/64", "via", ISPUp6)
	ip(NSCore, "-6", "route", "add", "fd77:0:0:2::/64", "via", ISPUp6)
	ip(NSServer, "route", "add", "default", "via", CoreUp4)
	// Reflectors for the capacity estimator's delay probes: three more
	// addresses on the server, reached over the same path as everything.
	for _, reflector := range Reflectors4 {
		ip(NSServer, "addr", "add", reflector+"/32", "dev", "lo")
	}
	ip(NSISP, "route", "add", ReflectorNet4, "via", CoreDown4)
	ip(NSCore, "route", "add", ReflectorNet4, "via", Server4)
	ip(NSServer, "-6", "route", "add", "default", "via", CoreUp6)

	// The inet chain exists so IPv6 flows get conntrack entries too; the
	// nat table alone only tracks IPv4.
	st = append(st, in(NSRouter, "nft", routerRules))
	st = append(st, in(NSRouter, "sysctl", "-qw", "net.netfilter.nf_conntrack_acct=1"))

	st = append(st, linkSteps(link, "add")...)
	return st
}

const routerRules = `
table ip aqos_nat {
	chain postrouting {
		type nat hook postrouting priority srcnat;
		oifname "` + IfWAN + `" masquerade
	}
}
table inet aqos_track {
	chain forward {
		type filter hook forward priority filter;
		ct state new counter
	}
}`

// linkSteps sets rate in isp and impairment in core. Rate and delay sit in
// separate namespaces so a token bucket and netem never share an interface.
func linkSteps(l contracts.Link, verb string) []step {
	qdisc := func(ns, dev string, spec []string) step {
		return in(ns, append([]string{"tc", "qdisc", verb, "dev", dev, "root"}, spec...)...)
	}
	return []step{
		qdisc(NSISP, IfISPUp, tbfArgs(l.UpKbit)),
		qdisc(NSISP, IfISPDown, tbfArgs(l.DownKbit)),
		qdisc(NSCore, IfCoreUp, netemArgs(l)),
		qdisc(NSCore, IfCoreDown, netemArgs(l)),
	}
}
