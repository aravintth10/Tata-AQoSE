package testbed

import (
	"bytes"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
)

// HoldCommand is the hidden subcommand a holder process runs. A namespace
// lives exactly as long as its holder, so killing every holder is a
// complete teardown.
const HoldCommand = "_hold"

// holders scans /proc for this user's holder processes. It does not use the
// state file, so it also finds what a killed `up` left behind.
func holders() map[string]int {
	found := map[string]int{}
	entries, _ := os.ReadDir("/proc")
	for _, e := range entries {
		pid, err := strconv.Atoi(e.Name())
		if err != nil {
			continue
		}
		raw, err := os.ReadFile(filepath.Join("/proc", e.Name(), "cmdline"))
		if err != nil {
			continue
		}
		argv := strings.Split(string(raw), "\x00")
		if len(argv) >= 3 && argv[1] == HoldCommand && strings.HasPrefix(argv[2], Prefix) && ownedByUs(pid) {
			found[argv[2]] = pid
		}
	}
	return found
}

func ownedByUs(pid int) bool {
	info, err := os.Stat(filepath.Join("/proc", strconv.Itoa(pid)))
	if err != nil {
		return false
	}
	st, ok := info.Sys().(*syscall.Stat_t)
	return ok && int(st.Uid) == os.Getuid()
}

// startHolder starts one holder and waits until it sits in its new
// namespace. userPID is 0 for the first holder, which creates the user
// namespace all the network namespaces belong to.
func startHolder(self, name string, userPID int) (int, error) {
	var cmd *exec.Cmd
	if userPID == 0 {
		cmd = exec.Command(self, HoldCommand, name)
		cmd.SysProcAttr = &syscall.SysProcAttr{
			Cloneflags:  syscall.CLONE_NEWUSER,
			UidMappings: []syscall.SysProcIDMap{{ContainerID: 0, HostID: os.Getuid(), Size: 1}},
			GidMappings: []syscall.SysProcIDMap{{ContainerID: 0, HostID: os.Getgid(), Size: 1}},
			Setsid:      true,
		}
	} else {
		cmd = exec.Command("nsenter", "-t", strconv.Itoa(userPID), "-U", "--preserve-credentials",
			"unshare", "-n", self, HoldCommand, name)
		cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	}
	if err := cmd.Start(); err != nil {
		return 0, fmt.Errorf("start holder %s: %w", name, err)
	}
	pid := cmd.Process.Pid
	exited := make(chan struct{})
	go func() { cmd.Wait(); close(exited) }()

	// The pid is known before nsenter and unshare have exec'd into the
	// holder; using it earlier would act on the wrong namespace.
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		select {
		case <-exited:
			return 0, fmt.Errorf("holder %s exited at once; are user namespaces allowed for this user?", name)
		default:
		}
		raw, _ := os.ReadFile(fmt.Sprintf("/proc/%d/cmdline", pid))
		argv := strings.Split(string(raw), "\x00")
		if len(argv) >= 3 && argv[1] == HoldCommand && argv[2] == name {
			return pid, nil
		}
		time.Sleep(2 * time.Millisecond)
	}
	return 0, fmt.Errorf("holder %s did not start", name)
}

// nsenterArgv is the command line that runs argv inside a holder's
// namespaces.
func nsenterArgv(pid int, argv []string) []string {
	return append([]string{"nsenter", "-t", strconv.Itoa(pid), "-U", "-n", "--preserve-credentials", "--"}, argv...)
}

func run(pid int, argv ...string) ([]byte, error) {
	full := nsenterArgv(pid, argv)
	cmd := exec.Command(full[0], full[1:]...)
	// nft takes its ruleset on stdin when the argument is a whole table.
	if argv[0] == "nft" && len(argv) == 2 {
		cmd.Args = append(full[:len(full)-1], "-f", "-")
		cmd.Stdin = strings.NewReader(argv[1])
	}
	var out, errb bytes.Buffer
	cmd.Stdout, cmd.Stderr = &out, &errb
	if err := cmd.Run(); err != nil {
		shown := strings.Join(argv, " ")
		if len(shown) > 120 {
			shown = shown[:120] + "..."
		}
		return nil, fmt.Errorf("%s: %w: %s", shown, err, strings.TrimSpace(errb.String()))
	}
	return out.Bytes(), nil
}

func netNS(pid int) string {
	link, _ := os.Readlink(fmt.Sprintf("/proc/%d/ns/net", pid))
	return link
}

// killAll removes every holder and every process still running inside one
// of their network namespaces. It reports how many holders it found.
func killAll() int {
	found := holders()
	if len(found) == 0 {
		return 0
	}
	spaces := map[string]bool{}
	holder := map[int]bool{}
	for name, pid := range found {
		holder[pid] = true
		if name != NSUser {
			spaces[netNS(pid)] = true
		}
	}
	// Never the namespace this command runs in: that would be every process
	// the user owns.
	delete(spaces, "")
	delete(spaces, netNS(os.Getpid()))

	inside := func() []int {
		var pids []int
		entries, _ := os.ReadDir("/proc")
		for _, e := range entries {
			pid, err := strconv.Atoi(e.Name())
			if err != nil || holder[pid] || pid == os.Getpid() {
				continue
			}
			if spaces[netNS(pid)] {
				pids = append(pids, pid)
			}
		}
		return pids
	}
	for _, pid := range inside() {
		syscall.Kill(pid, syscall.SIGTERM)
	}
	for wait := 0; wait < 50 && len(inside()) > 0; wait++ {
		time.Sleep(20 * time.Millisecond)
	}
	for _, pid := range inside() {
		syscall.Kill(pid, syscall.SIGKILL)
	}
	for pid := range holder {
		syscall.Kill(pid, syscall.SIGKILL)
	}
	for wait := 0; wait < 250 && len(holders()) > 0; wait++ {
		time.Sleep(20 * time.Millisecond)
	}
	return len(found)
}
