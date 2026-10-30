package testbed

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"adaptiveqos/contracts"
)

// State is what `up` leaves behind for the other commands and the runner.
type State struct {
	// Mode is "rootless" or "root". The runner copies it into the manifest.
	Mode       string         `json:"mode"`
	ScenarioID string         `json:"scenario_id"`
	Scenario   string         `json:"scenario"`
	CreatedAt  string         `json:"created_at"`
	Link       contracts.Link `json:"link"`
	Hosts      []Host         `json:"hosts"`
	Server4    string         `json:"server_ip4"`
	Server6    string         `json:"server_ip6"`
	Reflectors []string       `json:"reflectors"`
	Holders    map[string]int `json:"holders"`
}

// StatePath is per user and does not survive a reboot, like the namespaces.
func StatePath() string {
	dir := os.Getenv("XDG_RUNTIME_DIR")
	if dir == "" {
		dir = filepath.Join(os.TempDir(), "aqos-"+strconv.Itoa(os.Getuid()))
	}
	return filepath.Join(dir, "aqos", "testbed.json")
}

func (s *State) save() error {
	data, err := json.MarshalIndent(s, "", "  ")
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(StatePath()), 0o700); err != nil {
		return err
	}
	tmp := StatePath() + ".tmp"
	if err := os.WriteFile(tmp, append(data, '\n'), 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, StatePath())
}

// Load returns the running testbed, checked against the live holders.
func Load() (*State, error) {
	data, err := os.ReadFile(StatePath())
	if err != nil {
		return nil, fmt.Errorf("no testbed is up (run: aqos-testbed up <scenario.yaml>)")
	}
	var s State
	if err := json.Unmarshal(data, &s); err != nil {
		return nil, fmt.Errorf("%s: %w", StatePath(), err)
	}
	live := holders()
	for name, pid := range s.Holders {
		if live[name] != pid {
			return nil, fmt.Errorf("testbed is broken: namespace %s is gone (run: aqos-testbed down)", name)
		}
	}
	return &s, nil
}

// Down removes every namespace, link and process of any earlier testbed. It
// is safe to run twice and after a killed `up`.
func Down() int {
	n := killAll()
	os.Remove(StatePath())
	return n
}

// Up tears down any earlier testbed and builds the scenario's. self is the
// path of this binary, which the holders run.
func Up(self, scenarioPath string) (*State, error) {
	sc, err := contracts.LoadScenario(scenarioPath)
	if err != nil {
		return nil, err
	}
	hosts, err := planHosts(sc)
	if err != nil {
		return nil, err
	}
	if err := checkLink(sc.Link); err != nil {
		return nil, err
	}
	Down()

	abs, _ := filepath.Abs(scenarioPath)
	st := &State{
		Mode: "rootless", ScenarioID: sc.ID, Scenario: abs,
		CreatedAt: time.Now().Format(time.RFC3339),
		Link:      sc.Link, Hosts: hosts, Server4: Server4, Server6: Server6, Reflectors: Reflectors4,
		Holders: map[string]int{},
	}
	if os.Getuid() == 0 {
		st.Mode = "root"
	}
	fail := func(err error) (*State, error) {
		Down()
		return nil, err
	}

	names := []string{NSRouter, NSISP, NSCore, NSServer}
	for _, h := range hosts {
		names = append(names, h.NS)
	}
	userPID, err := startHolder(self, NSUser, 0)
	if err != nil {
		return fail(err)
	}
	st.Holders[NSUser] = userPID
	for _, name := range names {
		pid, err := startHolder(self, name, userPID)
		if err != nil {
			return fail(err)
		}
		st.Holders[name] = pid
	}
	for _, s := range buildSteps(hosts, sc.Link, st.Holders) {
		if _, err := run(st.Holders[s.ns], s.argv...); err != nil {
			return fail(fmt.Errorf("%s: %w", strings.TrimPrefix(s.ns, Prefix), err))
		}
	}
	if err := st.save(); err != nil {
		return fail(err)
	}
	if err := st.Ping(); err != nil {
		return fail(err)
	}
	return st, nil
}

// Ping checks IPv4 and IPv6 from every host to the server.
func (s *State) Ping() error {
	for _, h := range s.Hosts {
		for _, target := range []string{s.Server4, s.Server6} {
			var err error
			// The first IPv6 packet can be lost to neighbour discovery, and
			// a scenario's link may itself lose packets.
			for try := 0; try < 6; try++ {
				if _, err = run(s.Holders[h.NS], "ping", "-c", "1", "-W", "1", "-q", target); err == nil {
					break
				}
				time.Sleep(300 * time.Millisecond)
			}
			if err != nil {
				return fmt.Errorf("host %s cannot reach %s: %w", h.Name, target, err)
			}
		}
	}
	return nil
}

// Resolve accepts "router", "host-laptop", or the full prefixed name.
func (s *State) Resolve(ns string) (int, error) {
	if pid, ok := s.Holders[ns]; ok && ns != NSUser {
		return pid, nil
	}
	if pid, ok := s.Holders[Prefix+ns]; ok && Prefix+ns != NSUser {
		return pid, nil
	}
	var known []string
	for name := range s.Holders {
		if name != NSUser {
			known = append(known, strings.TrimPrefix(name, Prefix))
		}
	}
	return 0, fmt.Errorf("unknown namespace %q (have: %s)", ns, strings.Join(known, ", "))
}

// ExecArgv is the command line that runs argv inside a namespace,
// optionally pinned to CPUs.
func (s *State) ExecArgv(ns, cpus string, argv []string) ([]string, error) {
	pid, err := s.Resolve(ns)
	if err != nil {
		return nil, err
	}
	if cpus != "" {
		argv = append([]string{"taskset", "-c", cpus}, argv...)
	}
	return nsenterArgv(pid, argv), nil
}

// Event applies one scenario event. Arguments left out keep their current
// value. It returns a line describing the link afterwards.
func (s *State) Event(kind string, args map[string]string) (string, error) {
	link := s.Link
	num := func(key string, into *float64) error {
		v, ok := args[key]
		if !ok {
			return nil
		}
		delete(args, key)
		f, err := strconv.ParseFloat(v, 64)
		if err != nil {
			return fmt.Errorf("%s=%q is not a number", key, v)
		}
		*into = f
		return nil
	}
	switch kind {
	case "link_rate":
		dir, ok := args["direction"]
		delete(args, "direction")
		if !ok || (dir != "up" && dir != "down") {
			return "", fmt.Errorf("link_rate needs direction=up or direction=down")
		}
		var rate float64
		if err := num("rate_kbit", &rate); err != nil {
			return "", err
		}
		if rate < 1 {
			return "", fmt.Errorf("link_rate needs rate_kbit of at least 1")
		}
		if dir == "up" {
			link.UpKbit = int(rate)
		} else {
			link.DownKbit = int(rate)
		}
	case "link_impair":
		for key, into := range map[string]*float64{
			"delay_ms": &link.DelayMs, "jitter_ms": &link.JitterMs, "loss_pct": &link.LossPct,
		} {
			if err := num(key, into); err != nil {
				return "", err
			}
		}
	default:
		return "", fmt.Errorf("event %q is not handled by the testbed", kind)
	}
	for key := range args {
		return "", fmt.Errorf("%s does not take %q", kind, key)
	}
	if err := checkLink(link); err != nil {
		return "", err
	}
	for _, st := range linkSteps(link, "change") {
		if _, err := run(s.Holders[st.ns], st.argv...); err != nil {
			return "", err
		}
	}
	s.Link = link
	if err := s.save(); err != nil {
		return "", err
	}
	return s.LinkLine(), nil
}

func (s *State) LinkLine() string {
	l := s.Link
	return fmt.Sprintf("link: down %d kbit, up %d kbit, delay %g ms, jitter %g ms, loss %g%%",
		l.DownKbit, l.UpKbit, l.DelayMs, l.JitterMs, l.LossPct)
}
