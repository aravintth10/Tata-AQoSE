// Package contracts holds the Go side of the interface contracts in this
// directory. The JSON Schemas in schema/ are the source of truth; the types
// here must decode every file in examples/ without unknown fields.
package contracts

import (
	_ "embed"
	"encoding/json"
	"fmt"
	"os"

	"gopkg.in/yaml.v3"
)

const SchemaVersion = 1

type Class string

const (
	Telephony              Class = "telephony"
	RealtimeInteractive    Class = "realtime_interactive"
	MultimediaConferencing Class = "multimedia_conferencing"
	MultimediaStreaming    Class = "multimedia_streaming"
	Standard               Class = "standard"
	HighThroughputData     Class = "high_throughput_data"
	LowPriorityData        Class = "low_priority_data"
)

type Tin string

const (
	TinVoice      Tin = "voice"
	TinVideo      Tin = "video"
	TinBestEffort Tin = "best_effort"
	TinBulk       Tin = "bulk"
)

// FeatureRecord is sent by the feature extractor to the classifier.
type FeatureRecord struct {
	SchemaVersion int     `json:"schema_version"`
	FlowID        string  `json:"flow_id"`
	HostID        string  `json:"host_id"`
	Kind          string  `json:"kind"`
	TsNs          int64   `json:"ts_ns"`
	IPVersion     int     `json:"ip_version"`
	Protocol      int     `json:"protocol"`
	SrcPort       int     `json:"src_port"`
	DstPort       int     `json:"dst_port"`
	PktSizes      [8]int  `json:"pkt_sizes"`
	PktGapsUs     [7]int  `json:"pkt_gaps_us"`
	Window        *Window `json:"window"`
}

type Window struct {
	Samples         int     `json:"samples"`
	PktsUpPerS      float64 `json:"pkts_up_per_s"`
	PktsDownPerS    float64 `json:"pkts_down_per_s"`
	BytesUpPerS     float64 `json:"bytes_up_per_s"`
	BytesDownPerS   float64 `json:"bytes_down_per_s"`
	MeanPktSizeUp   float64 `json:"mean_pkt_size_up"`
	MeanPktSizeDown float64 `json:"mean_pkt_size_down"`
	PktSizeSpread   float64 `json:"pkt_size_spread"`
	RateCV          float64 `json:"rate_cv"`
	Burstiness      float64 `json:"burstiness"`
}

// Classification is the classifier's answer for one feature record.
type Classification struct {
	SchemaVersion int               `json:"schema_version"`
	FlowID        string            `json:"flow_id"`
	ModelVersion  string            `json:"model_version"`
	Probs         map[Class]float64 `json:"probs"`
	Confidence    float64           `json:"confidence"`
	InferenceUs   float64           `json:"inference_us"`
}

// PolicyProposal is sent to the safety shield. Exactly one of FlowLabel and
// ShaperRate is set, matching Kind.
type PolicyProposal struct {
	SchemaVersion int         `json:"schema_version"`
	ProposalID    string      `json:"proposal_id"`
	TsNs          int64       `json:"ts_ns"`
	Source        string      `json:"source"`
	Kind          string      `json:"kind"`
	Reason        string      `json:"reason"`
	FlowLabel     *FlowLabel  `json:"flow_label,omitempty"`
	ShaperRate    *ShaperRate `json:"shaper_rate,omitempty"`
}

type FlowLabel struct {
	FlowID         string   `json:"flow_id"`
	HostID         string   `json:"host_id"`
	Tin            Tin      `json:"tin"`
	DSCP           string   `json:"dscp"`
	PreviousTin    *Tin     `json:"previous_tin"`
	TopClass       *Class   `json:"top_class"`
	ExpectedDamage *float64 `json:"expected_damage"`
	DecisionRule   string   `json:"decision_rule"`
}

type ShaperRate struct {
	Direction        string `json:"direction"`
	RateKbit         int    `json:"rate_kbit"`
	PreviousRateKbit *int   `json:"previous_rate_kbit"`
}

// PolicyVerdict is the shield's answer to one proposal. Applied is nil on
// reject.
type PolicyVerdict struct {
	SchemaVersion int      `json:"schema_version"`
	ProposalID    string   `json:"proposal_id"`
	TsNs          int64    `json:"ts_ns"`
	Verdict       string   `json:"verdict"`
	Reason        string   `json:"reason"`
	Applied       *Applied `json:"applied"`
}

// Applied carries Tin and DSCP for a flow label, or RateKbit for a shaper
// rate.
type Applied struct {
	Tin      Tin    `json:"tin,omitempty"`
	DSCP     string `json:"dscp,omitempty"`
	RateKbit int    `json:"rate_kbit,omitempty"`
}

// ServiceRequest is one line sent to the ML service. Record, PreviousTin
// and LeasedClasses are for op "classify" only.
type ServiceRequest struct {
	Op            string         `json:"op"`
	ID            uint64         `json:"id"`
	Record        *FeatureRecord `json:"record,omitempty"`
	PreviousTin   *Tin           `json:"previous_tin,omitempty"`
	LeasedClasses []Class        `json:"leased_classes,omitempty"`
}

// ServiceResponse is the ML service's answer to one request. With OK
// false, only Error is set and the flow stays unlabelled.
type ServiceResponse struct {
	ID             *uint64         `json:"id"`
	OK             bool            `json:"ok"`
	Error          string          `json:"error,omitempty"`
	Classification *Classification `json:"classification,omitempty"`
	Proposal       *PolicyProposal `json:"proposal,omitempty"`
	ModelVersion   string          `json:"model_version,omitempty"`
	DecisionRule   string          `json:"decision_rule,omitempty"`
	Ready          bool            `json:"ready,omitempty"`
}

// MetricSample is one line of a run's metrics.jsonl.
type MetricSample struct {
	RunID     string  `json:"run_id"`
	TS        float64 `json:"t_s"`
	Source    string  `json:"source"`
	Metric    string  `json:"metric"`
	Value     float64 `json:"value"`
	Host      *string `json:"host,omitempty"`
	TrafficID *string `json:"traffic_id,omitempty"`
	Profile   *string `json:"profile,omitempty"`
	TrueClass *Class  `json:"true_class,omitempty"`
	Tin       *Tin    `json:"tin,omitempty"`
	Direction *string `json:"direction,omitempty"`
	Component *string `json:"component,omitempty"`
}

// RunManifest is written once per run next to metrics.jsonl.
type RunManifest struct {
	SchemaVersion int      `json:"schema_version"`
	RunID         string   `json:"run_id"`
	ScenarioID    string   `json:"scenario_id"`
	Config        string   `json:"config"`
	Seed          int      `json:"seed"`
	Attempt       int      `json:"attempt"`
	Tag           *string  `json:"tag"`
	StartedAt     string   `json:"started_at"`
	DurationS     int      `json:"duration_s"`
	WarmupS       int      `json:"warmup_s"`
	Valid         bool     `json:"valid"`
	InvalidReason *string  `json:"invalid_reason"`
	Versions      Versions `json:"versions"`
}

type Versions struct {
	GitCommit    string  `json:"git_commit"`
	Kernel       string  `json:"kernel"`
	IPRoute2     string  `json:"iproute2"`
	ModelVersion *string `json:"model_version"`
}

// Taxonomy is the shared class, tin, DSCP and profile mapping in
// taxonomy.json.
type Taxonomy struct {
	SchemaVersion int `json:"schema_version"`
	Classes       map[Class]struct {
		DSCP string `json:"dscp"`
		Tin  Tin    `json:"tin"`
	} `json:"classes"`
	Tins map[Tin]struct {
		DSCP string `json:"dscp"`
	} `json:"tins"`
	DSCP map[string]struct {
		Value int `json:"value"`
		Tin   Tin `json:"tin"`
	} `json:"dscp"`
	Profiles     map[string]Class `json:"profiles"`
	DefaultClass Class            `json:"default_class"`
	Configs      []string         `json:"configs"`
}

//go:embed taxonomy.json
var taxonomyJSON []byte

// LoadTaxonomy returns the taxonomy embedded at build time.
func LoadTaxonomy() (*Taxonomy, error) {
	var t Taxonomy
	if err := json.Unmarshal(taxonomyJSON, &t); err != nil {
		return nil, fmt.Errorf("taxonomy.json: %w", err)
	}
	return &t, nil
}

// Scenario is one experiment scenario, written by hand as YAML.
type Scenario struct {
	SchemaVersion  int       `yaml:"schema_version"`
	ID             string    `yaml:"id"`
	Name           string    `yaml:"name"`
	Description    string    `yaml:"description"`
	DurationS      int       `yaml:"duration_s"`
	WarmupS        int       `yaml:"warmup_s"`
	Link           Link      `yaml:"link"`
	Hosts          []string  `yaml:"hosts"`
	Traffic        []Traffic `yaml:"traffic"`
	Events         []Event   `yaml:"events"`
	PrimaryMetrics []string  `yaml:"primary_metrics"`
}

// Link is the emulated WAN bottleneck. DelayMs is one way.
type Link struct {
	DownKbit int     `yaml:"down_kbit" json:"down_kbit"`
	UpKbit   int     `yaml:"up_kbit" json:"up_kbit"`
	DelayMs  float64 `yaml:"delay_ms" json:"delay_ms"`
	JitterMs float64 `yaml:"jitter_ms" json:"jitter_ms"`
	LossPct  float64 `yaml:"loss_pct" json:"loss_pct"`
}

// Traffic is one generator entry. StopS is nil when the entry runs to the
// end of the run.
type Traffic struct {
	ID      string         `yaml:"id"`
	Host    string         `yaml:"host"`
	Profile string         `yaml:"profile"`
	StartS  float64        `yaml:"start_s"`
	StopS   *float64       `yaml:"stop_s"`
	Params  map[string]any `yaml:"params"`
}

type Event struct {
	AtS  float64        `yaml:"at_s"`
	Type string         `yaml:"type"`
	Args map[string]any `yaml:"args"`
}

// LoadScenario reads a scenario file and rejects unknown fields.
func LoadScenario(path string) (*Scenario, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	dec := yaml.NewDecoder(f)
	dec.KnownFields(true)
	var s Scenario
	if err := dec.Decode(&s); err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	return &s, nil
}
