// Command aqos-testbed builds and tears down the emulated home network.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"adaptiveqos/internal/testbed"
)

const usage = `usage:
  aqos-testbed up <scenario.yaml>             tear down any earlier testbed, build this one, check ping4 and ping6
  aqos-testbed down                           remove everything; safe to run twice
  aqos-testbed status                         print the running testbed as JSON
  aqos-testbed exec [-cpus LIST] <namespace> -- <command>...
  aqos-testbed event <type> [key=value]...    link_rate direction=up|down rate_kbit=N
                                              link_impair [delay_ms=N] [jitter_ms=N] [loss_pct=N]
  aqos-testbed check-rate [-seconds N]        measure the bottleneck in both directions

namespaces: router, isp, core, server, host-<name>`

func main() {
	if len(os.Args) < 2 {
		die(usage)
	}
	args := os.Args[2:]
	switch os.Args[1] {
	case testbed.HoldCommand:
		hold := make(chan os.Signal, 1)
		signal.Notify(hold, syscall.SIGTERM, syscall.SIGINT)
		<-hold
	case testbed.ServeCommand:
		check(testbed.Serve(args[0]))
	case testbed.LoadCommand:
		check(testbed.LoadLink(args[0], args[1]))
	case "up":
		if len(args) != 1 {
			die(usage)
		}
		st, err := testbed.Up(self(), args[0])
		check(err)
		fmt.Printf("testbed up (%s): scenario %s, %d hosts, ping4 and ping6 pass\n", st.Mode, st.ScenarioID, len(st.Hosts))
		fmt.Println(st.LinkLine())
	case "down":
		if n := testbed.Down(); n == 0 {
			fmt.Println("nothing to remove")
		} else {
			fmt.Printf("removed %d namespaces\n", n-1)
		}
	case "status":
		st, err := testbed.Load()
		check(err)
		out, _ := json.MarshalIndent(st, "", "  ")
		fmt.Println(string(out))
	case "exec":
		fs := flag.NewFlagSet("exec", flag.ExitOnError)
		cpus := fs.String("cpus", "", "pin the command to these CPUs, as taskset -c takes them")
		fs.Parse(args)
		rest := fs.Args()
		if len(rest) >= 2 && rest[1] == "--" {
			rest = append(rest[:1], rest[2:]...)
		}
		if len(rest) < 2 {
			die(usage)
		}
		st, err := testbed.Load()
		check(err)
		argv, err := st.ExecArgv(rest[0], *cpus, rest[1:])
		check(err)
		path, err := exec.LookPath(argv[0])
		check(err)
		check(syscall.Exec(path, argv, os.Environ()))
	case "event":
		if len(args) < 1 {
			die(usage)
		}
		kv := map[string]string{}
		for _, a := range args[1:] {
			key, value, ok := strings.Cut(a, "=")
			if !ok {
				die("event arguments are key=value, got " + a)
			}
			kv[key] = value
		}
		st, err := testbed.Load()
		check(err)
		line, err := st.Event(args[0], kv)
		check(err)
		fmt.Println(line)
	case "check-rate":
		fs := flag.NewFlagSet("check-rate", flag.ExitOnError)
		seconds := fs.Float64("seconds", 5, "measuring time per direction")
		fs.Parse(args)
		st, err := testbed.Load()
		check(err)
		results, err := st.CheckRate(self(), time.Duration(*seconds*float64(time.Second)))
		check(err)
		ok := true
		for _, r := range results {
			verdict := "ok"
			if !r.OK() {
				verdict, ok = "FAIL", false
			}
			fmt.Printf("%-4s configured %7d kbit  measured %9.0f kbit  error %+.2f%%  %s\n",
				r.Direction, r.ConfiguredKbit, r.MeasuredKbit, r.ErrorPct, verdict)
		}
		if !ok {
			die(fmt.Sprintf("bottleneck is more than %g%% from the configured rate", testbed.RateTolerancePct))
		}
	default:
		die(usage)
	}
}

func self() string {
	path, err := os.Executable()
	check(err)
	return path
}

func check(err error) {
	if err != nil {
		die("aqos-testbed: " + err.Error())
	}
}

func die(msg string) {
	fmt.Fprintln(os.Stderr, msg)
	os.Exit(1)
}
