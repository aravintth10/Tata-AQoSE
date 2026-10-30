package testbed

import (
	"path/filepath"
	"testing"

	"adaptiveqos/contracts"
)

// Every scenario in scenarios/ must load and give a testbed plan: hosts
// with addresses and a link the testbed can build.
func TestScenarioFilesPlan(t *testing.T) {
	files, _ := filepath.Glob(filepath.Join("..", "..", "scenarios", "S*.yaml"))
	if len(files) < 6 {
		t.Fatalf("found %d scenario files", len(files))
	}
	for _, file := range files {
		s, err := contracts.LoadScenario(file)
		if err != nil {
			t.Errorf("%s: %v", file, err)
			continue
		}
		if _, err := planHosts(s); err != nil {
			t.Errorf("%s: %v", file, err)
		}
		if err := checkLink(s.Link); err != nil {
			t.Errorf("%s: %v", file, err)
		}
		hosts := map[string]bool{}
		for _, h := range s.Hosts {
			hosts[h] = true
		}
		for _, entry := range s.Traffic {
			if !hosts[entry.Host] {
				t.Errorf("%s: traffic %s is on a host the scenario does not have", file, entry.ID)
			}
		}
	}
}
