// Command aqos-classify is the ML service without Python: the exported
// classifier and the decision rule behind the same Unix socket and the
// same one-JSON-object-per-line protocol as aqos_ml.serve, so the
// controller cannot tell them apart.
//
//	aqos-classify -socket ml.sock -model models/lgbm-v2-b3f691d8/trees.json [-mode top_label]
//
// Its answers are aqos_ml.serve's: internal/classify is tested against the
// Python service's probabilities and tins. The threshold rules (B4) stay
// in Python; they stand in for other tools, not for this one.
package main

import (
	"bufio"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"math"
	"net"
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"time"

	"adaptiveqos/contracts"
	"adaptiveqos/internal/classify"
	"adaptiveqos/internal/runclock"
)

var plain = map[contracts.Class]string{
	contracts.Telephony: "a voice call", contracts.RealtimeInteractive: "a game or other real-time traffic",
	contracts.MultimediaConferencing: "a video call", contracts.MultimediaStreaming: "streaming video",
	contracts.Standard: "ordinary web traffic", contracts.HighThroughputData: "a large download",
	contracts.LowPriorityData: "background transfer",
}

var tinName = map[contracts.Tin]string{
	contracts.TinVoice: "Voice", contracts.TinVideo: "Video", contracts.TinBestEffort: "Best Effort", contracts.TinBulk: "Bulk",
}

type service struct {
	model  *classify.Model
	engine *classify.Engine
	tax    *contracts.Taxonomy
}

type request struct {
	Op            string                   `json:"op"`
	ID            *uint64                  `json:"id"`
	Record        *contracts.FeatureRecord `json:"record"`
	PreviousTin   *contracts.Tin           `json:"previous_tin"`
	LeasedClasses []contracts.Class        `json:"leased_classes"`
}

func newID() string {
	raw := make([]byte, 8)
	rand.Read(raw)
	return hex.EncodeToString(raw)
}

func pct(p float64) string { return fmt.Sprintf("%.0f%%", math.Round(p*100)) }

// reason is decide.py's _reason.
func (s *service) reason(probs []float64, ch classify.Choice, leased bool) string {
	top := 0
	for i, c := range classify.Classes {
		if c == ch.Top {
			top = i
		}
	}
	e := s.engine
	text := fmt.Sprintf("Looks like %s (%s confident)", plain[ch.Top], pct(probs[top]))
	topTin := s.tax.Classes[ch.Top].Tin
	switch {
	case ch.Guarded:
		text += "; not demoted to Bulk on its first packets, or while it is UDP, as calls and games are, so it stays in Best Effort."
	case ch.Held:
		text += fmt.Sprintf("; not sure enough to demote it to Bulk (needs over %s), so it stays in Best Effort.", pct(e.DemotionBar))
	case e.TopLabel:
		text += fmt.Sprintf("; %s is the tin for that class.", tinName[ch.Tin])
	case ch.Tin != contracts.TinVoice && ch.Expected[contracts.TinVoice] < ch.Expected[ch.Tin]-e.TieMargin:
		text += fmt.Sprintf("; not sure enough that it is a call or a game to put it in Voice (needs over %s), so %s, the next lowest expected damage.",
			pct(e.VoiceBar), tinName[ch.Tin])
	case ch.Tin == topTin:
		text += fmt.Sprintf("; the %s tin has the lowest expected damage.", tinName[ch.Tin])
	default:
		text += fmt.Sprintf("; %s has the lowest expected damage (%.2f, against %.2f for %s).",
			tinName[ch.Tin], ch.Expected[ch.Tin], ch.Expected[topTin], tinName[topTin])
	}
	if leased {
		text += " A priority lease is active for this device."
	}
	return text
}

func (s *service) answer(line []byte) any {
	var req request
	if err := json.Unmarshal(line, &req); err != nil {
		return map[string]any{"id": nil, "ok": false, "error": "request is not JSON"}
	}
	fail := func(why string) any { return map[string]any{"id": req.ID, "ok": false, "error": why} }
	switch {
	case req.ID == nil:
		return fail("request: 'id' is a required property")
	case req.Op == "health":
		return map[string]any{"id": req.ID, "ok": true, "model_version": s.model.Version,
			"decision_rule": s.engine.RuleName, "ready": true}
	case req.Op != "classify":
		return fail(fmt.Sprintf("request: unknown op %q", req.Op))
	case req.Record == nil:
		return fail("request: 'record' is a required property")
	}
	rec := req.Record
	started := time.Now()
	x := classify.Vector(rec)
	probs := s.model.Probabilities(&x)
	took := float64(time.Since(started).Nanoseconds()) / 1000
	byClass := map[contracts.Class]float64{}
	confidence := 0.0
	for i, c := range classify.Classes {
		byClass[c] = probs[i]
		confidence = math.Max(confidence, probs[i])
	}
	classification := contracts.Classification{SchemaVersion: contracts.SchemaVersion, FlowID: rec.FlowID,
		ModelVersion: s.model.Version, Probs: byClass, Confidence: confidence, InferenceUs: took}
	source := "decision_engine"
	if rec.Kind == "window" {
		source = "reclassifier" // a second look at a flow already seen
	}
	label := map[string]any{"flow_id": rec.FlowID, "host_id": rec.HostID, "previous_tin": req.PreviousTin}
	var reason string
	ch, err := s.engine.Choose(probs, req.LeasedClasses, rec)
	if err != nil {
		label["tin"], label["dscp"] = contracts.TinBestEffort, s.tax.Tins[contracts.TinBestEffort].DSCP
		label["top_class"], label["expected_damage"], label["decision_rule"] = nil, nil, "fallback"
		reason = fmt.Sprintf("Could not decide (%v); left in Best Effort.", err)
	} else {
		leased := false
		for _, c := range req.LeasedClasses {
			leased = leased || classify.Interactive[c]
		}
		label["tin"], label["dscp"], label["top_class"] = ch.Tin, s.engine.DSCP(ch.Top, ch.Tin), ch.Top
		label["expected_damage"] = math.Round(ch.Expected[ch.Tin]*1e6) / 1e6
		label["decision_rule"] = s.engine.RuleName
		reason = s.reason(probs, ch, leased)
	}
	proposal := map[string]any{"schema_version": contracts.SchemaVersion, "proposal_id": newID(),
		"ts_ns": runclock.NowNs(), "source": source, "kind": "flow_label", "reason": reason, "flow_label": label}
	return map[string]any{"id": req.ID, "ok": true, "classification": classification, "proposal": proposal}
}

func (s *service) serve(conn net.Conn) {
	defer conn.Close()
	scanner := bufio.NewScanner(conn)
	scanner.Buffer(make([]byte, 64<<10), 1<<20)
	var write sync.Mutex
	for scanner.Scan() {
		line := scanner.Bytes()
		if len(strings.TrimSpace(string(line))) == 0 {
			continue
		}
		out, _ := json.Marshal(s.answer(line))
		write.Lock()
		conn.Write(append(out, '\n'))
		write.Unlock()
	}
}

func die(err error) {
	fmt.Fprintln(os.Stderr, "aqos-classify:", err)
	os.Exit(1)
}

func main() {
	socket := flag.String("socket", "", "Unix socket to listen on")
	modelPath := flag.String("model", "", "trees.json written by python -m aqos_ml.export")
	mode := flag.String("mode", "expected_damage", "expected_damage or top_label")
	damagePath := flag.String("damage", "configs/damage_table.yaml", "damage table")
	noGuard := flag.Bool("no-bulk-guard", false, "let the model demote UDP flows and first-packet records to Bulk")
	flag.Parse()
	if *socket == "" || *modelPath == "" || (*mode != "expected_damage" && *mode != "top_label") {
		flag.Usage()
		os.Exit(2)
	}
	model, err := classify.Load(*modelPath)
	if err != nil {
		die(err)
	}
	damage, err := classify.LoadDamage(*damagePath)
	if err != nil {
		die(err)
	}
	engine, err := classify.NewEngine(damage, *mode == "top_label")
	if err != nil {
		die(err)
	}
	engine.BulkGuard = !*noGuard
	tax, _ := contracts.LoadTaxonomy()
	s := &service{model: model, engine: engine, tax: tax}

	os.Remove(*socket)
	listener, err := net.Listen("unix", *socket)
	if err != nil {
		die(err)
	}
	os.Chmod(*socket, 0o600)
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGTERM, syscall.SIGINT)
	go func() {
		<-sig
		listener.Close()
	}()
	fmt.Fprintf(os.Stderr, "aqos-classify: %s, deciding by %s, on %s\n", model.Version, engine.RuleName, *socket)
	for {
		conn, err := listener.Accept()
		if err != nil {
			os.Remove(*socket)
			return
		}
		go s.serve(conn)
	}
}
