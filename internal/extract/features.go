package extract

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"math"
	"net/netip"

	"adaptiveqos/contracts"
)

const (
	// FirstPackets is how many packets a flow is watched for one by one.
	FirstPackets = 8
	// WindowSeconds is how many one-second samples a window covers at most.
	WindowSeconds = 5
)

// Counters are a flow's packet and byte totals since it began, as conntrack
// accounting keeps them. Up is LAN to WAN.
type Counters struct {
	PktsUp, PktsDown, BytesUp, BytesDown uint64
}

// Sample is what one second added to the counters.
type Sample Counters

// Window computes the nine window features over the last one to five
// one-second samples, as the contract defines them. It returns nil when
// there is no sample yet.
func Window(samples []Sample) *contracts.Window {
	n := len(samples)
	if n == 0 {
		return nil
	}
	if n > WindowSeconds {
		samples, n = samples[n-WindowSeconds:], WindowSeconds
	}
	var total Sample
	var sizes []float64 // per-second mean packet size, seconds with packets only
	pkts := make([]float64, n)
	bytes := make([]float64, n)
	for i, s := range samples {
		total.PktsUp += s.PktsUp
		total.PktsDown += s.PktsDown
		total.BytesUp += s.BytesUp
		total.BytesDown += s.BytesDown
		pkts[i] = float64(s.PktsUp + s.PktsDown)
		bytes[i] = float64(s.BytesUp + s.BytesDown)
		if pkts[i] > 0 {
			sizes = append(sizes, bytes[i]/pkts[i])
		}
	}
	w := &contracts.Window{
		Samples:       n,
		PktsUpPerS:    float64(total.PktsUp) / float64(n),
		PktsDownPerS:  float64(total.PktsDown) / float64(n),
		BytesUpPerS:   float64(total.BytesUp) / float64(n),
		BytesDownPerS: float64(total.BytesDown) / float64(n),
	}
	if total.PktsUp > 0 {
		w.MeanPktSizeUp = float64(total.BytesUp) / float64(total.PktsUp)
	}
	if total.PktsDown > 0 {
		w.MeanPktSizeDown = float64(total.BytesDown) / float64(total.PktsDown)
	}
	_, w.PktSizeSpread = meanStd(sizes)
	if mean, std := meanStd(pkts); mean > 0 {
		w.RateCV = std / mean
	}
	if mean, _ := meanStd(bytes); mean > 0 {
		peak := 0.0
		for _, b := range bytes {
			peak = math.Max(peak, b)
		}
		w.Burstiness = peak / mean
	}
	return w
}

// meanStd is the mean and the population standard deviation.
func meanStd(v []float64) (mean, std float64) {
	if len(v) == 0 {
		return 0, 0
	}
	for _, x := range v {
		mean += x
	}
	mean /= float64(len(v))
	for _, x := range v {
		std += (x - mean) * (x - mean)
	}
	return mean, math.Sqrt(std / float64(len(v)))
}

// Hasher makes the identifiers that stand in for addresses. The key is
// random per boot when live and per capture when offline, so identifiers
// cannot be linked across runs or turned back into addresses.
type Hasher struct{ Key []byte }

func (h Hasher) id(parts ...[]byte) string {
	mac := hmac.New(sha256.New, h.Key)
	for _, p := range parts {
		mac.Write(p)
	}
	return hex.EncodeToString(mac.Sum(nil)[:8])
}

// FlowID identifies a flow by its protocol and both endpoints.
func (h Hasher) FlowID(protocol uint8, a, b netip.AddrPort) string {
	ab, _ := a.MarshalBinary()
	bb, _ := b.MarshalBinary()
	return h.id([]byte{protocol}, ab, bb)
}

// HostID identifies a LAN host by its address.
func (h Hasher) HostID(addr netip.Addr) string {
	return h.id([]byte("host"), addr.AsSlice())
}
