package testbed

import (
	"reflect"
	"strings"
	"testing"

	"adaptiveqos/contracts"
)

func TestTBFArgs(t *testing.T) {
	cases := []struct {
		kbit int
		want string
	}{
		// 100 Mbit: 1 ms of burst, 500 ms of FIFO.
		{100000, "tbf rate 100000kbit burst 12500 limit 6250000"},
		{20000, "tbf rate 20000kbit burst 3028 limit 1250000"},
		// Slow links keep room for whole frames.
		{64, "tbf rate 64kbit burst 3028 limit 16384"},
	}
	for _, c := range cases {
		if got := strings.Join(tbfArgs(c.kbit), " "); got != c.want {
			t.Errorf("tbfArgs(%d) = %q, want %q", c.kbit, got, c.want)
		}
	}
}

func TestNetemArgs(t *testing.T) {
	cases := []struct {
		link contracts.Link
		want string
	}{
		{contracts.Link{DelayMs: 10}, "netem limit 20000 delay 10ms"},
		{contracts.Link{DelayMs: 2.5, JitterMs: 1, LossPct: 0.5}, "netem limit 20000 delay 2.5ms 1ms loss 0.5%"},
		{contracts.Link{DelayMs: 10, LossPct: 2}, "netem limit 20000 delay 10ms loss 2%"},
	}
	for _, c := range cases {
		if got := strings.Join(netemArgs(c.link), " "); got != c.want {
			t.Errorf("netemArgs(%+v) = %q, want %q", c.link, got, c.want)
		}
	}
}

func TestPlanHosts(t *testing.T) {
	hosts, err := planHosts(&contracts.Scenario{Hosts: []string{"laptop", "tv1"}})
	if err != nil {
		t.Fatal(err)
	}
	want := []Host{
		{"laptop", "aqos-host-laptop", "10.77.0.10", "fd77:0:0:1::10"},
		{"tv1", "aqos-host-tv1", "10.77.0.11", "fd77:0:0:1::11"},
	}
	if !reflect.DeepEqual(hosts, want) {
		t.Errorf("got %+v, want %+v", hosts, want)
	}
	if _, err := planHosts(&contracts.Scenario{Hosts: []string{"a", "a"}}); err == nil {
		t.Error("duplicate host accepted")
	}
	if _, err := planHosts(&contracts.Scenario{}); err == nil {
		t.Error("empty host list accepted")
	}
}

func TestCheckLink(t *testing.T) {
	good := contracts.Link{DownKbit: 100000, UpKbit: 20000, DelayMs: 10}
	if err := checkLink(good); err != nil {
		t.Error(err)
	}
	for _, bad := range []contracts.Link{
		{DownKbit: 0, UpKbit: 1},
		{DownKbit: 1, UpKbit: 1, DelayMs: -1},
		{DownKbit: 1, UpKbit: 1, LossPct: 101},
	} {
		if checkLink(bad) == nil {
			t.Errorf("accepted %+v", bad)
		}
	}
}

// Every step must name a namespace that has a holder, and every interface
// name must fit the kernel's 15-character limit.
func TestBuildSteps(t *testing.T) {
	names := make([]string, maxHosts)
	for i := range names {
		names[i] = "h" + strings.Repeat("x", 20) + string(rune('a'+i%26)) + string(rune('a'+i/26))
	}
	hosts, err := planHosts(&contracts.Scenario{Hosts: names})
	if err != nil {
		t.Fatal(err)
	}
	pid := map[string]int{NSRouter: 1, NSISP: 2, NSCore: 3, NSServer: 4}
	for i, h := range hosts {
		pid[h.NS] = 10 + i
	}
	for _, s := range buildSteps(hosts, contracts.Link{DownKbit: 1000, UpKbit: 1000}, pid) {
		if pid[s.ns] == 0 {
			t.Fatalf("step in unknown namespace %q: %v", s.ns, s.argv)
		}
		for i, arg := range s.argv {
			if (arg == "dev" || arg == "name") && len(s.argv[i+1]) > 15 {
				t.Errorf("interface name too long: %v", s.argv)
			}
		}
	}
}
