// Package intent holds what the user has asked for: leases that say
// "treat this device's calls and games as more important for a while".
//
// A lease changes one thing. The decision engine counts the damage of
// leaving that device's interactive traffic in Best Effort or Bulk as
// larger (decide.py), so it promotes sooner. Nothing is guaranteed and
// nobody else is starved: a promoted flow lands in CAKE's Voice or Video
// tin, which have priority only up to a share of the link.
package intent

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/netip"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"time"

	"adaptiveqos/contracts"
)

const (
	// MaxLeases is how many may be active at once. Priority given to
	// everyone is priority given to no one.
	MaxLeases = 2
	// MaxDuration is the longest a lease may run.
	MaxDuration = 2 * time.Hour
)

// Classes are what a lease protects: the traffic a person notices within
// a second.
var Classes = []contracts.Class{contracts.Telephony, contracts.RealtimeInteractive, contracts.MultimediaConferencing}

// Lease is one device's priority, until a time.
type Lease struct {
	ID   string `json:"id"`
	Host string `json:"host"` // the device's address on the LAN
	// Expires is wall-clock time, so that it means the same after a
	// restart. While running, expiry is decided on the monotonic clock.
	Expires time.Time `json:"expires"`
	// SecondsLeft is filled in when a lease is listed.
	SecondsLeft int `json:"seconds_left"`

	deadline time.Time // carries the monotonic reading
}

var (
	ErrTooMany     = errors.New("two leases are already active; end one first")
	ErrTooLong     = errors.New("a lease can run for at most two hours")
	ErrDuration    = errors.New("the duration must be more than zero")
	ErrUnknownHost = errors.New("no device with that address has been seen on the network")
	ErrHasLease    = errors.New("that device already has a lease")
	ErrNoLease     = errors.New("no such lease")
)

// Store holds the active leases. It is safe for concurrent use.
type Store struct {
	// Known says whether a device with this address has been seen. A
	// lease for a mistyped address would otherwise sit there doing
	// nothing while the user waits for it to help.
	Known func(netip.Addr) bool
	// HostID turns an address into the identifier feature records carry.
	HostID func(netip.Addr) string
	// Path, if set, is where leases are kept across restarts.
	Path string

	mu     sync.Mutex
	leases map[string]*Lease
	now    func() time.Time
}

// NewStore loads whatever leases a previous run left that have not expired.
func NewStore(path string, known func(netip.Addr) bool, hostID func(netip.Addr) string) *Store {
	s := &Store{Known: known, HostID: hostID, Path: path, leases: map[string]*Lease{}, now: time.Now}
	if path == "" {
		return s
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return s
	}
	var saved []Lease
	if json.Unmarshal(data, &saved) != nil {
		return s
	}
	for _, lease := range saved {
		left := time.Until(lease.Expires)
		if left <= 0 || left > MaxDuration {
			continue // expired while we were down, or not something we wrote
		}
		kept := lease
		kept.deadline = s.now().Add(left)
		s.leases[kept.ID] = &kept
	}
	return s
}

// expire drops leases whose time has come. Called with mu held.
func (s *Store) expire() (changed bool) {
	now := s.now()
	for id, lease := range s.leases {
		if !now.Before(lease.deadline) {
			delete(s.leases, id)
			changed = true
		}
	}
	return changed
}

// save writes the leases to disk. Called with mu held.
func (s *Store) save() {
	if s.Path == "" {
		return
	}
	list := make([]Lease, 0, len(s.leases))
	for _, lease := range s.leases {
		list = append(list, *lease)
	}
	data, err := json.Marshal(list)
	if err != nil {
		return
	}
	os.MkdirAll(filepath.Dir(s.Path), 0o755)
	tmp := s.Path + ".tmp"
	if os.WriteFile(tmp, data, 0o600) == nil {
		os.Rename(tmp, s.Path)
	}
}

// Create starts a lease for a device.
func (s *Store) Create(host netip.Addr, duration time.Duration) (Lease, error) {
	switch {
	case duration <= 0:
		return Lease{}, ErrDuration
	case duration > MaxDuration:
		return Lease{}, ErrTooLong
	case s.Known != nil && !s.Known(host):
		return Lease{}, ErrUnknownHost
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.expire()
	for _, lease := range s.leases {
		if lease.Host == host.String() {
			return Lease{}, ErrHasLease
		}
	}
	if len(s.leases) >= MaxLeases {
		return Lease{}, ErrTooMany
	}
	raw := make([]byte, 8)
	rand.Read(raw)
	now := s.now()
	lease := &Lease{ID: hex.EncodeToString(raw), Host: host.String(), Expires: now.Add(duration).Round(0),
		deadline: now.Add(duration), SecondsLeft: int(duration.Seconds())}
	s.leases[lease.ID] = lease
	s.save()
	return *lease, nil
}

// Delete ends a lease early.
func (s *Store) Delete(id string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.expire()
	if _, ok := s.leases[id]; !ok {
		return ErrNoLease
	}
	delete(s.leases, id)
	s.save()
	return nil
}

// List returns the active leases, the one ending soonest first.
func (s *Store) List() []Lease {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.expire() {
		s.save()
	}
	now := s.now()
	list := make([]Lease, 0, len(s.leases))
	for _, lease := range s.leases {
		shown := *lease
		shown.SecondsLeft = int(lease.deadline.Sub(now).Seconds() + 0.5)
		list = append(list, shown)
	}
	sort.Slice(list, func(i, j int) bool { return list[i].SecondsLeft < list[j].SecondsLeft })
	return list
}

// Leased returns the classes a lease protects for the device a feature
// record names, or nil when it has none.
func (s *Store) Leased(hostID string) []contracts.Class {
	if s == nil || s.HostID == nil {
		return nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.expire() {
		s.save()
	}
	for _, lease := range s.leases {
		addr, err := netip.ParseAddr(lease.Host)
		if err == nil && s.HostID(addr) == hostID {
			return Classes
		}
	}
	return nil
}

func (l Lease) String() string {
	return fmt.Sprintf("lease %s for %s, %d s left", l.ID, l.Host, l.SecondsLeft)
}
