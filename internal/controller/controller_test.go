package controller

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/netip"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"adaptiveqos/contracts"
	"adaptiveqos/internal/cake"
	"adaptiveqos/internal/capacity"
	"adaptiveqos/internal/enforce"
	"adaptiveqos/internal/extract"
	"adaptiveqos/internal/intent"
	"adaptiveqos/internal/runclock"
	"adaptiveqos/internal/shield"
)

const hostID = "a1b2c3d4e5f60718"

// fakeService stands in for aqos_ml.serve on a real Unix socket. answer
// decides what comes back for each classify request; nil means no answer.
type fakeService struct {
	path     string
	listener net.Listener
	mu       sync.Mutex
	requests []map[string]any
	answer   func(id uint64, rec *contracts.FeatureRecord) *contracts.ServiceResponse
	delay    time.Duration
}

func startService(t *testing.T) *fakeService {
	t.Helper()
	s := &fakeService{path: filepath.Join(t.TempDir(), "ml.sock")}
	s.answer = func(id uint64, rec *contracts.FeatureRecord) *contracts.ServiceResponse {
		return proposalFor(id, rec.FlowID, contracts.TinVideo, "AF41", nil)
	}
	s.listen(t)
	t.Cleanup(func() { s.listener.Close() })
	return s
}

func (s *fakeService) listen(t *testing.T) {
	os.Remove(s.path)
	listener, err := net.Listen("unix", s.path)
	if err != nil {
		t.Fatal(err)
	}
	s.listener = listener
	go func() {
		for {
			conn, err := listener.Accept()
			if err != nil {
				return
			}
			go s.serve(conn)
		}
	}()
}

func (s *fakeService) serve(conn net.Conn) {
	defer conn.Close()
	scanner := bufio.NewScanner(conn)
	var write sync.Mutex
	for scanner.Scan() {
		var raw map[string]any
		var req contracts.ServiceRequest
		json.Unmarshal(scanner.Bytes(), &raw)
		json.Unmarshal(scanner.Bytes(), &req)
		s.mu.Lock()
		s.requests = append(s.requests, raw)
		answer, delay := s.answer, s.delay
		s.mu.Unlock()
		var resp *contracts.ServiceResponse
		if req.Op == "health" {
			id := req.ID
			resp = &contracts.ServiceResponse{ID: &id, OK: true, ModelVersion: "fake", DecisionRule: "expected_damage", Ready: true}
		} else {
			resp = answer(req.ID, req.Record)
		}
		if resp == nil {
			continue
		}
		go func() {
			time.Sleep(delay)
			line, _ := json.Marshal(resp)
			write.Lock()
			conn.Write(append(line, '\n'))
			write.Unlock()
		}()
	}
}

func proposalFor(id uint64, flowID string, tin contracts.Tin, dscp string, previous *contracts.Tin) *contracts.ServiceResponse {
	top := contracts.MultimediaConferencing
	damage := 0.6
	return &contracts.ServiceResponse{
		ID: &id, OK: true,
		Classification: &contracts.Classification{SchemaVersion: 1, FlowID: flowID, ModelVersion: "fake", InferenceUs: 140},
		Proposal: &contracts.PolicyProposal{
			SchemaVersion: 1, ProposalID: fmt.Sprintf("%016x", id), TsNs: 1, Source: "decision_engine",
			Kind: "flow_label", Reason: "Looks like a video call.",
			FlowLabel: &contracts.FlowLabel{
				FlowID: flowID, HostID: hostID, Tin: tin, DSCP: dscp, PreviousTin: previous,
				TopClass: &top, ExpectedDamage: &damage, DecisionRule: "expected_damage",
			},
		},
	}
}

// fakeLabels records what the enforcer was asked to do.
type fakeLabels struct {
	mu   sync.Mutex
	set  map[enforce.Flow]contracts.Tin
	fail error
}

func (l *fakeLabels) SetLabel(f enforce.Flow, tin contracts.Tin) error {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.fail != nil {
		return l.fail
	}
	l.set[f] = tin
	return nil
}

func (l *fakeLabels) get(f enforce.Flow) (contracts.Tin, bool) {
	l.mu.Lock()
	defer l.mu.Unlock()
	tin, ok := l.set[f]
	return tin, ok
}

// syncBuffer is a buffer the controller writes while a test reads.
type syncBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *syncBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *syncBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

func (l *fakeLabels) failWith(err error) {
	l.mu.Lock()
	l.fail = err
	l.mu.Unlock()
}

func (s *fakeService) set(fn func(*fakeService)) {
	s.mu.Lock()
	fn(s)
	s.mu.Unlock()
}

type rig struct {
	t       *testing.T
	service *fakeService
	client  *Client
	labels  *fakeLabels
	c       *Controller
	actions *syncBuffer
	metrics *syncBuffer
	stop    chan struct{}
}

func newRig(t *testing.T, cfg Config) *rig {
	t.Helper()
	r := &rig{t: t, service: startService(t), labels: &fakeLabels{set: map[enforce.Flow]contracts.Tin{}},
		actions: &syncBuffer{}, metrics: &syncBuffer{}, stop: make(chan struct{})}
	r.client = NewClient(r.service.path, 100*time.Millisecond, 64)
	tracker, err := shield.NewTracker(shield.DefaultConfig(500, 200_000, 1_000, 1_000_000))
	if err != nil {
		t.Fatal(err)
	}
	cfg.Actions, cfg.Metrics, cfg.RunID, cfg.Clock = r.actions, r.metrics, "test", runclock.Clock{T0Ns: runclock.NowNs()}
	r.c = New(cfg, r.client, tracker, r.labels)
	go r.c.Run(r.stop)
	t.Cleanup(func() { close(r.stop); r.client.Close() })
	return r
}

func flow(n int) (*extract.Flow, *contracts.FeatureRecord) {
	key := extract.Key{
		Protocol: 17,
		A:        netip.AddrPortFrom(netip.MustParseAddr("10.77.0.10"), uint16(40000+n)),
		B:        netip.AddrPortFrom(netip.MustParseAddr("10.78.0.10"), 3478),
	}
	id := fmt.Sprintf("%016x", n+1)
	f := &extract.Flow{Key: key, FlowID: id, HostID: hostID, LANIsA: true}
	rec := &contracts.FeatureRecord{
		SchemaVersion: 1, FlowID: id, HostID: hostID, Kind: "initial", TsNs: 1, IPVersion: 4, Protocol: 17,
		SrcPort: 40000 + n, DstPort: 3478, PktSizes: [8]int{128, -112, 1100, 1100, -980, 1100, -1012, 1100},
		PktGapsUs: [7]int{18400, 2100, 310, 19800, 420, 20100, 380},
	}
	return f, rec
}

func tuple(f *extract.Flow) enforce.Flow {
	return enforce.Flow{Protocol: f.Key.Protocol, Src: f.Key.A, Dst: f.Key.B}
}

func eventually(t *testing.T, what string, ok func() bool) {
	t.Helper()
	for deadline := time.Now().Add(3 * time.Second); time.Now().Before(deadline); time.Sleep(2 * time.Millisecond) {
		if ok() {
			return
		}
	}
	t.Fatalf("never happened: %s", what)
}

func TestFlowIsClassifiedCheckedAndLabelled(t *testing.T) {
	r := newRig(t, Config{})
	f, rec := flow(0)
	r.c.OnRecord(f, rec)
	eventually(t, "label written", func() bool { _, ok := r.labels.get(tuple(f)); return ok })
	if tin, _ := r.labels.get(tuple(f)); tin != contracts.TinVideo {
		t.Fatalf("labelled %q", tin)
	}
	eventually(t, "counted", func() bool { return r.c.Counts().Labelled == 1 })

	// The request was what the contract asks for, nulls and empties included.
	r.service.mu.Lock()
	request := r.service.requests[0]
	r.service.mu.Unlock()
	for _, key := range []string{"op", "id", "record", "previous_tin", "leased_classes"} {
		if _, ok := request[key]; !ok {
			t.Errorf("request lacks %q: %v", key, request)
		}
	}
	if request["previous_tin"] != nil || len(request["leased_classes"].([]any)) != 0 {
		t.Errorf("request: %v", request)
	}

	// The action log has the proposal, the verdict and the outcome.
	var action Action
	if err := json.Unmarshal([]byte(r.actions.String()), &action); err != nil {
		t.Fatal(err)
	}
	if action.Outcome != "applied" || action.Verdict.Verdict != "accept" || action.Proposal.Reason == "" {
		t.Fatalf("action: %+v", action)
	}
	if !strings.Contains(r.metrics.String(), `"metric":"inference_us"`) {
		t.Errorf("no inference sample: %s", r.metrics.String())
	}
}

func TestFlowThatEndedIsForgotten(t *testing.T) {
	r := newRig(t, Config{})
	f, rec := flow(0)
	r.c.OnRecord(f, rec)
	eventually(t, "labelled", func() bool { return r.c.Counts().Labelled == 1 })
	r.c.OnGone(f)
	second := proposalFor(9, f.FlowID, contracts.TinVoice, "EF", nil).Proposal
	if action := r.c.Propose(second); action.Outcome != "rejected" || action.Code != shield.UnknownFlow {
		t.Fatalf("proposal for a flow that ended: %+v", action)
	}
}

func TestShieldRejectionStopsAtTheShield(t *testing.T) {
	r := newRig(t, Config{})
	r.service.set(func(s *fakeService) {
		s.answer = func(id uint64, rec *contracts.FeatureRecord) *contracts.ServiceResponse {
			return proposalFor(id, rec.FlowID, contracts.TinVoice, "CS1", nil) // a DSCP of another tin
		}
	})
	f, rec := flow(0)
	r.c.OnRecord(f, rec)
	eventually(t, "rejected", func() bool { return r.c.Counts().Rejected == 1 })
	if _, ok := r.labels.get(tuple(f)); ok {
		t.Fatal("a rejected proposal reached the enforcer")
	}
}

func TestAnswerAboutAnotherFlowIsNotTrusted(t *testing.T) {
	r := newRig(t, Config{})
	other, otherRec := flow(1)
	r.c.OnRecord(other, otherRec)
	eventually(t, "other labelled", func() bool { return r.c.Counts().Labelled == 1 })
	r.service.set(func(s *fakeService) {
		s.answer = func(id uint64, rec *contracts.FeatureRecord) *contracts.ServiceResponse {
			return proposalFor(id, other.FlowID, contracts.TinVoice, "EF", nil)
		}
	})
	f, rec := flow(0)
	r.c.OnRecord(f, rec)
	eventually(t, "refused", func() bool { return r.c.Counts().Refused == 1 })
	if tin, _ := r.labels.get(tuple(other)); tin != contracts.TinVideo {
		t.Fatalf("the other flow was moved to %q", tin)
	}
}

func TestEveryFailureLeavesTheFlowUnlabelled(t *testing.T) {
	cases := []struct {
		name    string
		prepare func(*rig)
		counted func(Counts) uint64
	}{
		{"service refuses", func(r *rig) {
			r.service.set(func(s *fakeService) {
				s.answer = func(id uint64, rec *contracts.FeatureRecord) *contracts.ServiceResponse {
					return &contracts.ServiceResponse{ID: &id, OK: false, Error: "no trees"}
				}
			})
		}, func(n Counts) uint64 { return n.Refused }},
		{"service is slow", func(r *rig) { r.service.set(func(s *fakeService) { s.delay = 400 * time.Millisecond }) },
			func(n Counts) uint64 { return n.Timeouts }},
		{"service never answers", func(r *rig) {
			r.service.set(func(s *fakeService) {
				s.answer = func(uint64, *contracts.FeatureRecord) *contracts.ServiceResponse { return nil }
			})
		}, func(n Counts) uint64 { return n.Timeouts }},
		{"service is down", func(r *rig) { r.service.listener.Close(); os.Remove(r.service.path) },
			func(n Counts) uint64 { return n.ServiceDown }},
		{"flow ended first", func(r *rig) { r.labels.failWith(enforce.ErrFlowGone) },
			func(n Counts) uint64 { return n.FlowGone }},
		{"read-back mismatch", func(r *rig) { r.labels.failWith(fmt.Errorf("label: %w", enforce.ErrMismatch)) },
			func(n Counts) uint64 { return n.Mismatch }},
		{"enforcer error", func(r *rig) { r.labels.failWith(errors.New("netlink: permission denied")) },
			func(n Counts) uint64 { return n.Errors }},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			r := newRig(t, Config{})
			c.prepare(r)
			f, rec := flow(0)
			r.c.OnRecord(f, rec)
			eventually(t, "counted", func() bool { return c.counted(r.c.Counts()) == 1 })
			time.Sleep(450 * time.Millisecond) // a late answer must change nothing
			if _, ok := r.labels.get(tuple(f)); ok {
				t.Fatal("the flow was labelled")
			}
			if n := r.c.Counts(); n.Labelled != 0 {
				t.Fatalf("counts: %+v", n)
			}
		})
	}
}

func TestClassificationsPerSecondAreCapped(t *testing.T) {
	r := newRig(t, Config{MaxClassifyPerS: 5})
	// Start just after a second begins, so that all twelve fall in one.
	for runclock.NowNs()%int64(time.Second) > int64(200*time.Millisecond) {
		time.Sleep(10 * time.Millisecond)
	}
	for i := 0; i < 12; i++ {
		f, rec := flow(i)
		r.c.OnRecord(f, rec)
	}
	eventually(t, "five labelled", func() bool { return r.c.Counts().Labelled == 5 })
	if n := r.c.Counts(); n.OverCap != 7 || n.Records != 12 {
		t.Fatalf("counts: %+v", n)
	}
}

func TestTooManyPendingRequestsAreNotQueued(t *testing.T) {
	r := newRig(t, Config{})
	r.service.set(func(s *fakeService) {
		s.answer = func(uint64, *contracts.FeatureRecord) *contracts.ServiceResponse { return nil }
	})
	for i := 0; i < 80; i++ { // the client allows 64 pending
		f, rec := flow(i)
		r.c.OnRecord(f, rec)
	}
	if n := r.c.Counts(); n.ServiceBusy != 16 {
		t.Fatalf("counts: %+v", n)
	}
	eventually(t, "the rest time out", func() bool { return r.c.Counts().Timeouts == 64 })
}

func TestServiceComingBackIsUsedAgain(t *testing.T) {
	r := newRig(t, Config{})
	r.service.listener.Close()
	f, rec := flow(0)
	r.c.OnRecord(f, rec)
	if n := r.c.Counts(); n.ServiceDown != 1 {
		t.Fatalf("counts: %+v", n)
	}
	r.service.listen(t)
	time.Sleep(1100 * time.Millisecond) // the client dials at most once a second
	g, rec2 := flow(1)
	r.c.OnRecord(g, rec2)
	eventually(t, "labelled after reconnect", func() bool { _, ok := r.labels.get(tuple(g)); return ok })
}

func TestFlipCapClearsTheLabel(t *testing.T) {
	r := newRig(t, Config{})
	f, rec := flow(0)
	r.c.OnRecord(f, rec)
	eventually(t, "labelled", func() bool { return r.c.Counts().Labelled == 1 })
	video, voice := contracts.TinVideo, contracts.TinVoice
	steps := []struct {
		tin      contracts.Tin
		dscp     string
		previous *contracts.Tin
		outcome  string
	}{
		{voice, "EF", &video, "applied"},
		{video, "AF41", &voice, "applied"},
		{voice, "EF", &video, "rejected"}, // the third change in a minute
	}
	for i, s := range steps {
		p := proposalFor(uint64(100+i), f.FlowID, s.tin, s.dscp, s.previous).Proposal
		p.Source = "reclassifier"
		if action := r.c.Propose(p); action.Outcome != s.outcome {
			t.Fatalf("step %d: %+v", i, action)
		}
	}
	if tin, _ := r.labels.get(tuple(f)); tin != "" {
		t.Fatalf("a pinned flow keeps label %q; it should be cleared", tin)
	}
	if n := r.c.Counts(); n.Relabelled != 2 {
		t.Fatalf("counts: %+v", n)
	}
}

func TestHealth(t *testing.T) {
	r := newRig(t, Config{})
	resp, err := r.client.Health(2 * time.Second)
	if err != nil || resp.ModelVersion != "fake" || !resp.Ready {
		t.Fatalf("health: %+v %v", resp, err)
	}
	down := NewClient(filepath.Join(t.TempDir(), "nobody.sock"), 50*time.Millisecond, 8)
	defer down.Close()
	if _, err := down.Health(300 * time.Millisecond); err == nil {
		t.Fatal("health of a service that is not there succeeded")
	}
}

func TestPerSecondMetricsAndHeartbeat(t *testing.T) {
	beat := filepath.Join(t.TempDir(), "heartbeat")
	r := newRig(t, Config{Heartbeat: beat})
	f, rec := flow(0)
	r.c.OnRecord(f, rec)
	g, _ := flow(1)
	r.c.mu.Lock()
	r.c.flows[g.FlowID] = tuple(g) // seen, never classified
	r.c.mu.Unlock()
	eventually(t, "per-second samples", func() bool {
		text := r.metrics.String()
		return strings.Contains(text, `"metric":"flows_labelled","value":1`) &&
			strings.Contains(text, `"metric":"flows_unlabelled","value":1`)
	})
	if _, err := os.Stat(beat); err != nil {
		t.Fatalf("no heartbeat: %v", err)
	}
	// An extractor that has stopped must stop the heartbeat too.
	r.c.lastExtract.Store(runclock.NowNs() - int64(ExtractorStall) - 1)
	time.Sleep(600 * time.Millisecond)
	info, _ := os.Stat(beat)
	if age := time.Since(info.ModTime()); age < 500*time.Millisecond {
		t.Fatalf("heartbeat %v old although the extractor stalled", age)
	}
}

func window(rec *contracts.FeatureRecord) *contracts.FeatureRecord {
	again := *rec
	again.Kind = "window"
	again.Window = &contracts.Window{Samples: 5, PktsUpPerS: 50, PktsDownPerS: 50, BytesUpPerS: 5e4, BytesDownPerS: 5e4}
	return &again
}

func TestWindowAnswersMoveAFlowOnlyOnceConfirmed(t *testing.T) {
	r := newRig(t, Config{})
	f, rec := flow(0)
	r.c.OnRecord(f, rec)
	eventually(t, "first label", func() bool { return r.c.Counts().Labelled == 1 })

	// The same tin again changes nothing and writes nothing.
	r.c.OnWindow(f, window(rec))
	eventually(t, "unchanged", func() bool { return r.c.Counts().Unchanged == 1 })

	// From now on the service says Voice. Once is not enough.
	r.service.set(func(s *fakeService) {
		s.answer = func(id uint64, rec *contracts.FeatureRecord) *contracts.ServiceResponse {
			video := contracts.TinVideo
			return proposalFor(id, rec.FlowID, contracts.TinVoice, "EF", &video)
		}
	})
	r.c.OnWindow(f, window(rec))
	eventually(t, "held", func() bool { return r.c.Counts().Held == 1 })
	if tin, _ := r.labels.get(tuple(f)); tin != contracts.TinVideo {
		t.Fatalf("moved to %q on one window", tin)
	}
	r.c.OnWindow(f, window(rec))
	eventually(t, "moved", func() bool { tin, _ := r.labels.get(tuple(f)); return tin == contracts.TinVoice })
	if n := r.c.Counts(); n.Relabelled != 1 || n.Windows != 3 {
		t.Fatalf("counts: %+v", n)
	}

	// The request for a labelled flow says which tin it is in.
	r.service.mu.Lock()
	last := r.service.requests[len(r.service.requests)-1]
	r.service.mu.Unlock()
	if last["previous_tin"] != "video" || last["record"].(map[string]any)["kind"] != "window" {
		t.Fatalf("request: %v", last)
	}
}

func TestAnOddWindowDoesNotMoveAFlow(t *testing.T) {
	r := newRig(t, Config{})
	f, rec := flow(0)
	r.c.OnRecord(f, rec)
	eventually(t, "first label", func() bool { return r.c.Counts().Labelled == 1 })
	video := contracts.TinVideo
	voice := func(id uint64, rec *contracts.FeatureRecord) *contracts.ServiceResponse {
		return proposalFor(id, rec.FlowID, contracts.TinVoice, "EF", &video)
	}
	same := func(id uint64, rec *contracts.FeatureRecord) *contracts.ServiceResponse {
		return proposalFor(id, rec.FlowID, contracts.TinVideo, "AF41", &video)
	}
	for i, answer := range []func(uint64, *contracts.FeatureRecord) *contracts.ServiceResponse{voice, same, voice, same} {
		r.service.set(func(s *fakeService) { s.answer = answer })
		r.c.OnWindow(f, window(rec))
		eventually(t, "answered", func() bool { n := r.c.Counts(); return n.Held+n.Unchanged == uint64(i+1) })
	}
	if n := r.c.Counts(); n.Relabelled != 0 || n.Held != 2 {
		t.Fatalf("counts: %+v", n)
	}
}

func TestFlowWithoutALabelTakesOneFromItsFirstWindow(t *testing.T) {
	r := newRig(t, Config{})
	r.service.set(func(s *fakeService) {
		s.answer = func(uint64, *contracts.FeatureRecord) *contracts.ServiceResponse { return nil }
	})
	f, rec := flow(0)
	r.c.OnRecord(f, rec)
	eventually(t, "timed out", func() bool { return r.c.Counts().Timeouts == 1 })
	r.service.set(func(s *fakeService) {
		s.answer = func(id uint64, rec *contracts.FeatureRecord) *contracts.ServiceResponse {
			return proposalFor(id, rec.FlowID, contracts.TinVideo, "AF41", nil)
		}
	})
	r.c.OnWindow(f, window(rec))
	eventually(t, "labelled", func() bool { tin, _ := r.labels.get(tuple(f)); return tin == contracts.TinVideo })
}

// fakeShaper is CAKE's two rates, as the enforcer would set them.
type fakeShaper struct {
	mu    sync.Mutex
	rates map[string]int
	fail  error
}

func (s *fakeShaper) SetRate(direction string, kbit int) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.fail != nil {
		return s.fail
	}
	s.rates[direction] = kbit
	return nil
}

func (s *fakeShaper) rate(direction string) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.rates[direction]
}

func TestEstimatorCutsTheRateThroughTheShield(t *testing.T) {
	shaper := &fakeShaper{rates: map[string]int{"up": 19_000, "down": 95_000}}
	rounds := make(chan capacity.Round, 16)
	var sent float64
	var cakeMu sync.Mutex
	began := time.Now()
	estimator := capacity.New(capacity.DefaultConfig(19_000, 95_000, 500, 1_000))
	r := newRig(t, Config{
		Estimator: estimator, Rounds: rounds, Shaper: shaper,
		Devices: map[string]string{"down": "ifb0"},
		ReadCake: func(dev string) (*cake.Stats, error) {
			cakeMu.Lock()
			defer cakeMu.Unlock()
			// The link carries 20 Mbit/s whatever the shaper says.
			sent = 20_000 * 1000 / 8 * time.Since(began).Seconds()
			return &cake.Stats{RateKbit: float64(shaper.rate("down")), Tins: []cake.TinStats{{SentBytes: sent}}}, nil
		},
	})
	r.c.shield.RateRead("down", 95_000)
	r.c.shield.RateRead("up", 19_000)

	round := func(rtt float64) capacity.Round {
		return capacity.Round{RTTMs: []float64{rtt, rtt, rtt}, Answered: []bool{true, true, true},
			WaitingMs: []float64{0, 0, 0}, SilentMs: []float64{0, 0, 0}}
	}
	for i := 0; i < 25; i++ { // calm, long enough for two readings of CAKE
		rounds <- round(20)
		time.Sleep(100 * time.Millisecond)
	}
	if got := shaper.rate("down"); got != 95_000 {
		t.Fatalf("rate moved to %d on a calm link", got)
	}
	// Delay up: the queue is beyond the router. It stays up while the
	// shaper is above the link, and falls once it is below, as a buffer
	// that is being emptied does.
	delay := 300.0
	feed := func(rounds_ int) {
		for i := 0; i < rounds_; i++ {
			if shaper.rate("down") < 20_000 {
				delay = max(20, delay-30)
			}
			rounds <- round(delay)
			time.Sleep(100 * time.Millisecond)
		}
	}
	feed(15)
	// 95,000 to 47,500 to 23,750 (the shield's half per step), then nine
	// tenths of the 20,000 or so that got through.
	under := func() bool { rate := shaper.rate("down"); return rate > 17_000 && rate < 19_000 }
	eventually(t, "rate under the link", under)
	if n := r.c.Counts(); n.RateChanges != 3 {
		t.Fatalf("counts: %+v", n)
	}
	settled := shaper.rate("down")
	var first Action
	json.Unmarshal([]byte(strings.SplitN(r.actions.String(), "\n", 2)[0]), &first)
	if first.Outcome != "applied" || first.Verdict.Verdict != "clamp" || first.Code != shield.RateStep ||
		first.Proposal.Source != "capacity_estimator" || len(first.Proposal.ProposalID) != 16 {
		t.Fatalf("first action: %+v %+v", first, first.Proposal)
	}
	// The buffer takes a while to empty and the delay is high all that
	// time. It is falling, so the rate must not be pushed further down.
	feed(10)
	if got := shaper.rate("down"); got != settled {
		t.Fatalf("rate chased down from %d to %d", settled, got)
	}
}

func TestRateThatDoesNotReadBackIsNotBelieved(t *testing.T) {
	shaper := &fakeShaper{rates: map[string]int{"down": 95_000}, fail: fmt.Errorf("rate: %w", enforce.ErrMismatch)}
	r := newRig(t, Config{Shaper: shaper})
	r.c.shield.RateRead("down", 95_000)
	previous := 95_000
	proposal := &contracts.PolicyProposal{
		SchemaVersion: 1, TsNs: 1, Source: "capacity_estimator", Kind: "shaper_rate", Reason: "test",
		ShaperRate: &contracts.ShaperRate{Direction: "down", RateKbit: 80_000, PreviousRateKbit: &previous},
	}
	if action := r.c.ProposeRate(proposal); action.Outcome != "mismatch" {
		t.Fatalf("action: %+v", action)
	}
	// The shield still holds the old rate, so the same proposal is not stale.
	shaper.mu.Lock()
	shaper.fail = nil
	shaper.mu.Unlock()
	proposal.ProposalID = ""
	if action := r.c.ProposeRate(proposal); action.Outcome != "applied" || shaper.rate("down") != 80_000 {
		t.Fatalf("second try: %+v", action)
	}
}

func TestLeaseTravelsWithTheRequestAndSkipsTheWait(t *testing.T) {
	hostAddr := netip.MustParseAddr("10.77.0.10")
	leases := intent.NewStore("", nil, func(a netip.Addr) string {
		if a == hostAddr {
			return hostID
		}
		return "ffffffffffffffff"
	})
	r := newRig(t, Config{Leases: leases})
	// Without a lease the service is told of none, and the flow lands in
	// Best Effort.
	r.service.set(func(s *fakeService) {
		s.answer = func(id uint64, rec *contracts.FeatureRecord) *contracts.ServiceResponse {
			return proposalFor(id, rec.FlowID, contracts.TinBestEffort, "CS0", nil)
		}
	})
	f, rec := flow(0)
	r.c.OnRecord(f, rec)
	eventually(t, "first label", func() bool { return r.c.Counts().Labelled == 1 })
	if !r.c.HostSeen(hostID) || r.c.HostSeen("ffffffffffffffff") {
		t.Fatal("hosts seen are not tracked")
	}
	leasedIn := func(i int) []any {
		r.service.mu.Lock()
		defer r.service.mu.Unlock()
		return r.service.requests[i]["leased_classes"].([]any)
	}
	if len(leasedIn(0)) != 0 {
		t.Fatalf("leased classes without a lease: %v", leasedIn(0))
	}

	if _, err := leases.Create(hostAddr, 45*time.Minute); err != nil {
		t.Fatal(err)
	}
	best := contracts.TinBestEffort
	r.service.set(func(s *fakeService) {
		s.answer = func(id uint64, rec *contracts.FeatureRecord) *contracts.ServiceResponse {
			return proposalFor(id, rec.FlowID, contracts.TinVideo, "AF41", &best)
		}
	})
	r.c.OnWindow(f, window(rec))
	// One window is enough: the user asked for this.
	eventually(t, "promoted at once", func() bool { tin, _ := r.labels.get(tuple(f)); return tin == contracts.TinVideo })
	if got := leasedIn(1); len(got) != 3 {
		t.Fatalf("leased classes with a lease: %v", got)
	}
	if n := r.c.Counts(); n.Held != 0 {
		t.Fatalf("counts: %+v", n)
	}
}
