// Package enforce is the only code that changes the network: it writes
// flow labels into conntrack marks and sets CAKE's shaper rates. Every
// change is a transaction: snapshot, apply, read back, compare, and restore
// the snapshot if what was read is not what was asked for.
package enforce

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/netip"
	"os/exec"
	"strings"

	"adaptiveqos/contracts"
	"adaptiveqos/internal/netfilter"
)

// MarkMask is the part of a conntrack mark that holds the label. The other
// 29 bits belong to whoever else uses marks and are never touched.
const MarkMask = 0x7

// TinMark is the label value for each tin. CAKE's fwmark option sends a
// packet whose masked mark is n to tin n, counting from 1 in the order
// Bulk, Best Effort, Video, Voice. Zero means unlabelled: CAKE then picks
// the tin from the packet's own DSCP, which is plain CAKE.
var TinMark = map[contracts.Tin]uint32{
	contracts.TinBulk: 1, contracts.TinBestEffort: 2, contracts.TinVideo: 3, contracts.TinVoice: 4,
}

// MarkTin is the reverse of TinMark. A mark of zero has no tin.
func MarkTin(mark uint32) (contracts.Tin, bool) {
	for tin, value := range TinMark {
		if value == mark&MarkMask {
			return tin, true
		}
	}
	return "", false
}

// Flow names a flow the way conntrack does: by its first packet.
type Flow struct {
	Protocol uint8
	Src, Dst netip.AddrPort
}

// ErrMismatch means a change was applied, read back different, and undone.
var ErrMismatch = errors.New("read-back did not match; previous state restored")

// ErrFlowGone means the flow ended before it could be labelled. That is
// normal and nothing was changed.
var ErrFlowGone = netfilter.ErrNoEntry

// Enforcer acts on one router namespace: the one the process runs in.
type Enforcer struct {
	WANDev string // shapes the upload
	IFBDev string // shapes the download
	ct     *netfilter.Conntrack
	// afterApply runs between apply and read-back. Tests use it to disturb
	// the state, standing in for anything that could make an apply go wrong.
	afterApply func()
}

func New() (*Enforcer, error) {
	ct, err := netfilter.OpenConntrack()
	if err != nil {
		return nil, err
	}
	return &Enforcer{WANDev: "wan0", IFBDev: "ifb0", ct: ct}, nil
}

func (e *Enforcer) Close() error { return e.ct.Close() }

// DisturbAfterApply makes the next changes go wrong on purpose. Test only.
func (e *Enforcer) DisturbAfterApply(fn func()) { e.afterApply = fn }

func (e *Enforcer) dev(direction string) (string, error) {
	switch direction {
	case "up":
		return e.WANDev, nil
	case "down":
		return e.IFBDev, nil
	}
	return "", fmt.Errorf("direction %q is neither up nor down", direction)
}

func run(name string, args ...string) ([]byte, error) {
	out, err := exec.Command(name, args...).CombinedOutput()
	if err != nil {
		return out, fmt.Errorf("%s %s: %w: %s", name, strings.Join(args, " "), err, strings.TrimSpace(string(out)))
	}
	return out, nil
}

type cakeOptions struct {
	Kind    string `json:"kind"`
	Root    bool   `json:"root"`
	Options struct {
		Bandwidth float64 `json:"bandwidth"` // bytes per second
		Fwmark    string  `json:"fwmark"`
	} `json:"options"`
}

func (e *Enforcer) cake(dev string) (*cakeOptions, error) {
	out, err := run("tc", "-j", "qdisc", "show", "dev", dev)
	if err != nil {
		return nil, err
	}
	var qdiscs []cakeOptions
	if err := json.Unmarshal(out, &qdiscs); err != nil {
		return nil, fmt.Errorf("tc output for %s: %w", dev, err)
	}
	for i := range qdiscs {
		if qdiscs[i].Kind == "cake" && qdiscs[i].Root {
			return &qdiscs[i], nil
		}
	}
	return nil, fmt.Errorf("%s has no CAKE qdisc", dev)
}

// Rate reads a direction's shaper rate back from tc, in kbit/s.
func (e *Enforcer) Rate(direction string) (int, error) {
	dev, err := e.dev(direction)
	if err != nil {
		return 0, err
	}
	q, err := e.cake(dev)
	if err != nil {
		return 0, err
	}
	// tc reports bytes per second whatever unit the rate was set in.
	return int(q.Options.Bandwidth*8/1000 + 0.5), nil
}

func (e *Enforcer) writeRate(direction string, kbit int) error {
	dev, err := e.dev(direction)
	if err != nil {
		return err
	}
	_, err = run("tc", "qdisc", "change", "dev", dev, "root", "cake", "bandwidth", fmt.Sprintf("%dkbit", kbit))
	return err
}

// SetRate changes a direction's shaper rate in place. If the rate read back
// is not the rate asked for, the previous rate is put back and ErrMismatch
// returned.
func (e *Enforcer) SetRate(direction string, kbit int) error {
	if kbit < 1 {
		return fmt.Errorf("rate %d kbit is not positive", kbit)
	}
	before, err := e.Rate(direction)
	if err != nil {
		return err
	}
	if err := e.writeRate(direction, kbit); err != nil {
		return err
	}
	if e.afterApply != nil {
		e.afterApply()
	}
	after, err := e.Rate(direction)
	if err == nil && after == kbit {
		return nil
	}
	if restoreErr := e.writeRate(direction, before); restoreErr != nil {
		return fmt.Errorf("%w, and restoring %d kbit failed: %v", ErrMismatch, before, restoreErr)
	}
	return fmt.Errorf("%s rate: asked %d kbit, read %d: %w", direction, kbit, after, ErrMismatch)
}

// Label reads a flow's label. ok is false for an unlabelled flow.
func (e *Enforcer) Label(f Flow) (tin contracts.Tin, ok bool, err error) {
	entry, err := e.ct.Get(f.Protocol, f.Src, f.Dst)
	if err != nil {
		return "", false, err
	}
	tin, ok = MarkTin(entry.Mark)
	return tin, ok, nil
}

// SetLabel puts a flow in a tin by writing its conntrack mark; CAKE reads
// the mark for every later packet. An empty tin removes the label. For a
// flow that has ended it returns ErrFlowGone and changes nothing.
func (e *Enforcer) SetLabel(f Flow, tin contracts.Tin) error {
	want := uint32(0)
	if tin != "" {
		value, ok := TinMark[tin]
		if !ok {
			return fmt.Errorf("unknown tin %q", tin)
		}
		want = value
	}
	before, err := e.ct.Get(f.Protocol, f.Src, f.Dst)
	if err != nil {
		return err
	}
	if err := e.ct.SetMark(f.Protocol, f.Src, f.Dst, want, MarkMask); err != nil {
		return err
	}
	if e.afterApply != nil {
		e.afterApply()
	}
	after, err := e.ct.Get(f.Protocol, f.Src, f.Dst)
	if errors.Is(err, ErrFlowGone) {
		return ErrFlowGone // it ended while we worked: nothing left to label
	}
	if err == nil && after.Mark&MarkMask == want && after.Mark&^MarkMask == before.Mark&^MarkMask {
		return nil
	}
	if restoreErr := e.ct.SetMark(f.Protocol, f.Src, f.Dst, before.Mark, ^uint32(0)); restoreErr != nil && !errors.Is(restoreErr, ErrFlowGone) {
		return fmt.Errorf("%w, and restoring mark %#x failed: %v", ErrMismatch, before.Mark, restoreErr)
	}
	return fmt.Errorf("label: asked mark %d, read %#x: %w", want, after.Mark, ErrMismatch)
}

// Snapshot is the state a set of changes is about to touch.
type Snapshot struct {
	Rates map[string]int  // by direction
	Marks map[Flow]uint32 // whole marks; flows already gone are left out
}

// Snapshot reads both shaper rates and the marks of the given flows.
func (e *Enforcer) Snapshot(flows []Flow) (Snapshot, error) {
	snap := Snapshot{Rates: map[string]int{}, Marks: map[Flow]uint32{}}
	for _, direction := range []string{"up", "down"} {
		rate, err := e.Rate(direction)
		if err != nil {
			return snap, err
		}
		snap.Rates[direction] = rate
	}
	for _, f := range flows {
		entry, err := e.ct.Get(f.Protocol, f.Src, f.Dst)
		if errors.Is(err, ErrFlowGone) {
			continue
		}
		if err != nil {
			return snap, err
		}
		snap.Marks[f] = entry.Mark
	}
	return snap, nil
}

// Restore writes a snapshot back. Flows that have ended since are skipped.
func (e *Enforcer) Restore(snap Snapshot) error {
	var first error
	for direction, rate := range snap.Rates {
		if err := e.writeRate(direction, rate); err != nil && first == nil {
			first = err
		}
	}
	for f, mark := range snap.Marks {
		err := e.ct.SetMark(f.Protocol, f.Src, f.Dst, mark, ^uint32(0))
		if err != nil && !errors.Is(err, ErrFlowGone) && first == nil {
			first = err
		}
	}
	return first
}

// markRules copies the label from the conntrack entry to each packet about
// to leave on the WAN interface, where CAKE reads it.
const markTable = "aqos_mark"

func (e *Enforcer) markRules() string {
	return fmt.Sprintf(`
table inet %s {
	chain postrouting {
		type filter hook postrouting priority mangle;
		oifname "%s" meta mark set ct mark and %#x
	}
}`, markTable, e.WANDev, MarkMask)
}

func (e *Enforcer) ingressFilter(withMark bool) error {
	// Packets arriving on the WAN interface are redirected to the IFB
	// device to be shaped. With marking, the label is restored from the
	// conntrack entry first.
	run("tc", "filter", "del", "dev", e.WANDev, "parent", "ffff:", "prio", "10")
	args := []string{"filter", "add", "dev", e.WANDev, "parent", "ffff:", "protocol", "all", "prio", "10", "matchall"}
	if withMark {
		args = append(args, "action", "ctinfo", "cpmark", fmt.Sprintf("%#x", MarkMask))
	}
	args = append(args, "action", "mirred", "egress", "redirect", "dev", e.IFBDev)
	_, err := run("tc", args...)
	return err
}

func (e *Enforcer) fwmark(mask int) error {
	for _, dev := range []string{e.WANDev, e.IFBDev} {
		if _, err := run("tc", "qdisc", "change", "dev", dev, "root", "cake", "fwmark", fmt.Sprintf("%#x", mask)); err != nil {
			return err
		}
	}
	return nil
}

// InstallMarking makes CAKE obey the labels: until it is installed, marks
// can be written but nothing reads them. It needs CAKE on both devices, as
// configuration B2 sets up.
func (e *Enforcer) InstallMarking() error {
	run("nft", "delete", "table", "inet", markTable)
	cmd := exec.Command("nft", "-f", "-")
	cmd.Stdin = strings.NewReader(e.markRules())
	if out, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("nft: %w: %s", err, strings.TrimSpace(string(out)))
	}
	if err := e.ingressFilter(true); err != nil {
		return err
	}
	if err := e.fwmark(MarkMask); err != nil {
		return err
	}
	if ok, err := e.MarkingInstalled(); err != nil || !ok {
		e.RemoveMarking()
		return fmt.Errorf("marking: %w (%v)", ErrMismatch, err)
	}
	return nil
}

// RemoveMarking turns the whole system off: every flow is treated as
// unlabelled again, whatever its mark says. Marks are left in place.
func (e *Enforcer) RemoveMarking() error {
	_, nftErr := run("nft", "delete", "table", "inet", markTable)
	filterErr := e.ingressFilter(false)
	markErr := e.fwmark(0)
	if ok, _ := e.MarkingInstalled(); ok {
		return errors.Join(fmt.Errorf("marking still installed"), nftErr, filterErr, markErr)
	}
	return errors.Join(filterErr, markErr)
}

// MarkingInstalled reads back all three parts of marking: the rule, the
// ingress action, and CAKE's fwmark on both devices.
func (e *Enforcer) MarkingInstalled() (bool, error) {
	if _, err := run("nft", "list", "table", "inet", markTable); err != nil {
		return false, nil
	}
	filter, err := run("tc", "filter", "show", "dev", e.WANDev, "parent", "ffff:")
	if err != nil {
		return false, err
	}
	if !strings.Contains(string(filter), "ctinfo") {
		return false, nil
	}
	for _, dev := range []string{e.WANDev, e.IFBDev} {
		q, err := e.cake(dev)
		if err != nil {
			return false, err
		}
		if q.Options.Fwmark != fmt.Sprintf("%#x", MarkMask) {
			return false, nil
		}
	}
	return true, nil
}
