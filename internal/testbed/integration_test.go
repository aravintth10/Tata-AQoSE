package testbed

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// TestTestbed builds the real thing and checks chunk 2A's "done when". It
// replaces any running testbed, so it runs only when asked: make
// testbed-check.
func TestTestbed(t *testing.T) {
	if os.Getenv("AQOS_INTEGRATION") == "" {
		t.Skip("set AQOS_INTEGRATION=1 (make testbed-check)")
	}
	bin := filepath.Join(t.TempDir(), "aqos-testbed")
	if out, err := exec.Command("go", "build", "-o", bin, "adaptiveqos/cmd/aqos-testbed").CombinedOutput(); err != nil {
		t.Fatalf("build: %v\n%s", err, out)
	}
	scenario, _ := filepath.Abs("../../contracts/examples/scenario.S1.yaml")
	tb := func(args ...string) (string, error) {
		out, err := exec.Command(bin, args...).CombinedOutput()
		return string(out), err
	}
	must := func(args ...string) string {
		t.Helper()
		out, err := tb(args...)
		if err != nil {
			t.Fatalf("%v: %v\n%s", args, err, out)
		}
		return out
	}
	t.Cleanup(func() { tb("down") })

	// A killed `up` must leave nothing that `down` cannot remove.
	up := exec.Command(bin, "up", scenario)
	if err := up.Start(); err != nil {
		t.Fatal(err)
	}
	time.Sleep(150 * time.Millisecond)
	up.Process.Kill()
	up.Wait()
	if len(holders()) == 0 {
		t.Log("up was killed before it created a namespace")
	}
	must("down")
	if left := holders(); len(left) != 0 {
		t.Fatalf("down after a killed up left %v", left)
	}
	if out := must("down"); !strings.Contains(out, "nothing to remove") {
		t.Errorf("second down: %s", out)
	}

	// up runs ping4 and ping6 from every host and fails if either does.
	if out := must("up", scenario); !strings.Contains(out, "ping4 and ping6 pass") {
		t.Fatalf("up: %s", out)
	}
	// IPv4 is masqueraded: the server must see the router, not the host.
	must("exec", "server", "--", "nft", "add table ip probe; add chain ip probe in { type filter hook input priority 0; }; add rule ip probe in ip saddr 10.77.0.0/24 counter drop")
	must("exec", "host-laptop", "--", "ping", "-c", "1", "-W", "1", "10.78.0.10")

	out := must("check-rate", "-seconds", "4")
	t.Log("\n" + out)

	// A rate event must move the bottleneck, and up again must replace the
	// running testbed.
	must("event", "link_rate", "direction=down", "rate_kbit=20000")
	out = must("check-rate", "-seconds", "4")
	if !strings.Contains(out, "down configured   20000") {
		t.Errorf("rate event not applied:\n%s", out)
	}
	if _, err := tb("event", "kill_controller"); err == nil {
		t.Error("an event the testbed does not own was accepted")
	}
	before := holders()
	must("up", scenario)
	for name, pid := range holders() {
		if before[name] == pid {
			t.Errorf("up reused namespace %s", name)
		}
	}

	// down must also take a process still running inside a namespace.
	sleeper := exec.Command(bin, "exec", "host-desktop", "--", "sleep", "600")
	if err := sleeper.Start(); err != nil {
		t.Fatal(err)
	}
	time.Sleep(200 * time.Millisecond)
	must("down")
	done := make(chan struct{})
	go func() { sleeper.Wait(); close(done) }()
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		sleeper.Process.Kill()
		t.Error("down left a process running inside a namespace")
	}
	if left := holders(); len(left) != 0 {
		t.Errorf("down left %v", left)
	}
}
