package reclass

import (
	"encoding/json"
	"os"
	"strings"
	"testing"

	"adaptiveqos/contracts"
)

// The cases come from the ML side (python -m aqos_ml.envelope cases), with
// the answer its definition of the check gives for every window.
type caseFile struct {
	Envelopes Envelopes `json:"envelopes"`
	Cases     []struct {
		Name     string         `json:"name"`
		Class    string         `json:"class"`
		Tin      string         `json:"tin"`
		Settings map[string]int `json:"settings"`
		Windows  []contracts.Window
		Expect   []*string
	} `json:"cases"`
}

func load(t *testing.T) caseFile {
	t.Helper()
	raw, err := os.ReadFile("testdata/cases.json")
	if err != nil {
		t.Fatal(err)
	}
	var file caseFile
	if err := json.Unmarshal(raw, &file); err != nil {
		t.Fatal(err)
	}
	if len(file.Cases) == 0 {
		t.Fatal("no cases")
	}
	return file
}

func TestTheCheckAgreesWithTheMLCases(t *testing.T) {
	file := load(t)
	for _, c := range file.Cases {
		t.Run(c.Name, func(t *testing.T) {
			s := c.Settings
			w := NewWatcher(Config{K: s["k"], Windows: s["windows"], Interactive: s["interactive"],
				SlowEvery: s["slow_every"], Backoff: s["backoff"], BackoffMax: s["backoff_max"]}, file.Envelopes, 1)
			w.Answered("f", contracts.Class(c.Class))
			for i := range c.Windows {
				ask := w.Window("f", contracts.Tin(c.Tin), &c.Windows[i])
				switch want := c.Expect[i]; {
				case want == nil && ask != nil:
					t.Fatalf("window %d: sent back (%s), the cases say not", i, ask.Reason)
				case want != nil && ask == nil:
					t.Fatalf("window %d: not sent back, the cases say it is, for %s", i, *want)
				case want != nil && !strings.HasPrefix(ask.Reason, units[*want]):
					t.Fatalf("window %d: reason %q does not name %s", i, ask.Reason, *want)
				case ask != nil && ask.Recheck:
					t.Fatalf("window %d: a trigger was marked as a routine recheck", i)
				}
			}
		})
	}
}

func TestLoadReadsTheModelsEnvelopes(t *testing.T) {
	file := load(t)
	path := t.TempDir() + "/envelopes.json"
	raw, _ := json.Marshal(file.Envelopes)
	os.WriteFile(path, raw, 0o644)
	env, err := Load(path)
	if err != nil || len(env) != len(file.Envelopes) {
		t.Fatalf("Load: %d classes, %v", len(env), err)
	}
	os.WriteFile(path, []byte(`{"telephony": {"rate_cv": [0, 1]}}`), 0o644)
	if _, err := Load(path); err == nil {
		t.Fatal("an envelope missing statistics was accepted")
	}
}

func TestRoutineRecheckComesBetweenItsBounds(t *testing.T) {
	file := load(t)
	inside := file.Cases[0].Windows[0]
	for seed := int64(0); seed < 20; seed++ {
		w := NewWatcher(DefaultConfig(), file.Envelopes, seed)
		w.Answered("f", contracts.MultimediaConferencing)
		at := 0
		for second := 1; second <= 70 && at == 0; second++ {
			if ask := w.Window("f", contracts.TinVideo, &inside); ask != nil {
				if !ask.Recheck {
					t.Fatalf("a flow inside its range was triggered: %s", ask.Reason)
				}
				at = second
			}
		}
		if at < 30 || at > 60 {
			t.Fatalf("seed %d: routine recheck after %d s, want 30 to 60", seed, at)
		}
	}
}

func TestAFlowNobodyAnsweredForIsNotWatched(t *testing.T) {
	file := load(t)
	w := NewWatcher(DefaultConfig(), file.Envelopes, 1)
	if ask := w.Window("unknown", contracts.TinVideo, &file.Cases[0].Windows[0]); ask != nil {
		t.Fatal("a flow with no class was sent back")
	}
	w.Answered("f", contracts.Telephony)
	w.Gone("f")
	if ask := w.Window("f", contracts.TinVoice, nil); ask != nil {
		t.Fatal("a flow that ended was sent back")
	}
}

func TestReasonReadsAsASentence(t *testing.T) {
	got := reason(contracts.MultimediaConferencing, Excursion{Feature: "bytes_down_per_s", Value: 812.4, Low: 6235.58, High: 268008.3}, 2)
	want := "Bytes per second received was 812 for 2 s, outside the 6236 to 268008 normal for a video call"
	if got != want {
		t.Fatalf("reason:\n got %q\nwant %q", got, want)
	}
}
