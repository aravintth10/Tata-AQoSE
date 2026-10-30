package intent

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

var (
	laptop  = netip.MustParseAddr("10.77.0.10")
	desktop = netip.MustParseAddr("10.77.0.11")
	tv      = netip.MustParseAddr("10.77.0.12")
)

func store(t *testing.T, path string) (*Store, *time.Time) {
	t.Helper()
	known := func(a netip.Addr) bool { return a != netip.MustParseAddr("10.77.0.99") }
	s := NewStore(path, known, func(a netip.Addr) string { return "host-" + a.String() })
	now := time.Now()
	s.now = func() time.Time { return now }
	return s, &now
}

func TestLimits(t *testing.T) {
	s, _ := store(t, "")
	for _, c := range []struct {
		host netip.Addr
		d    time.Duration
		want error
	}{
		{laptop, 0, ErrDuration},
		{laptop, -time.Minute, ErrDuration},
		{laptop, 2*time.Hour + time.Second, ErrTooLong},
		{netip.MustParseAddr("10.77.0.99"), time.Hour, ErrUnknownHost},
		{laptop, 45 * time.Minute, nil},
		{laptop, 10 * time.Minute, ErrHasLease},
		{desktop, 2 * time.Hour, nil},
		{tv, time.Minute, ErrTooMany}, // the third
	} {
		if _, err := s.Create(c.host, c.d); !errors.Is(err, c.want) {
			t.Errorf("Create(%s, %v) = %v, want %v", c.host, c.d, err, c.want)
		}
	}
	if list := s.List(); len(list) != 2 || list[0].Host != laptop.String() || list[0].SecondsLeft != 45*60 {
		t.Fatalf("list: %+v", list)
	}
}

func TestLeaseProtectsItsDeviceOnlyAndEndsOnTime(t *testing.T) {
	s, now := store(t, "")
	lease, err := s.Create(laptop, 45*time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if got := s.Leased("host-10.77.0.10"); len(got) != 3 {
		t.Fatalf("leased: %v", got)
	}
	if got := s.Leased("host-10.77.0.11"); got != nil {
		t.Fatalf("another device is leased: %v", got)
	}
	*now = now.Add(45*time.Minute - time.Second)
	if s.Leased("host-10.77.0.10") == nil {
		t.Fatal("ended a second early")
	}
	*now = now.Add(time.Second)
	if s.Leased("host-10.77.0.10") != nil || len(s.List()) != 0 {
		t.Fatal("still active at its deadline")
	}
	if err := s.Delete(lease.ID); !errors.Is(err, ErrNoLease) {
		t.Fatalf("deleting an expired lease: %v", err)
	}
	// With the first gone there is room again.
	if _, err := s.Create(desktop, time.Minute); err != nil {
		t.Fatal(err)
	}
	var none *Store
	if none.Leased("x") != nil {
		t.Fatal("a controller without leases must still answer")
	}
}

func TestLeasesSurviveARestart(t *testing.T) {
	path := filepath.Join(t.TempDir(), "leases.json")
	first, _ := store(t, path)
	kept, _ := first.Create(laptop, time.Hour)
	ended, _ := first.Create(desktop, time.Hour)
	first.Delete(ended.ID)

	second, now := store(t, path) // the controller came back
	list := second.List()
	if len(list) != 1 || list[0].ID != kept.ID || list[0].SecondsLeft < 3590 || list[0].SecondsLeft > 3600 {
		t.Fatalf("after a restart: %+v", list)
	}
	*now = now.Add(time.Hour + time.Second)
	if len(second.List()) != 0 {
		t.Fatal("a restored lease did not expire")
	}
	third, _ := store(t, path)
	if len(third.List()) != 0 {
		t.Fatal("an expired lease came back from disk")
	}
}

func TestHTTP(t *testing.T) {
	s, _ := store(t, "")
	server := httptest.NewServer(s.Handler())
	defer server.Close()
	post := func(body string) (int, map[string]any) {
		r, err := http.Post(server.URL+"/leases", "application/json", strings.NewReader(body))
		if err != nil {
			t.Fatal(err)
		}
		defer r.Body.Close()
		var out map[string]any
		json.NewDecoder(r.Body).Decode(&out)
		return r.StatusCode, out
	}
	for _, c := range []struct {
		body string
		want int
	}{
		{`{"host":"10.77.0.10","minutes":45}`, 201},
		{`{"host":"10.77.0.10","minutes":5}`, 409},
		{`{"host":"10.77.0.99","minutes":5}`, 404},
		{`{"host":"the laptop","minutes":5}`, 400},
		{`{"host":"10.77.0.11","minutes":121}`, 400},
		{`{"host":"10.77.0.11","minutes":0}`, 400},
		{`{"host":"10.77.0.11","minutes":5,"class":"all"}`, 400},
		{`not json`, 400},
		{`{"host":"10.77.0.11","minutes":120}`, 201},
		{`{"host":"10.77.0.12","minutes":5}`, 409},
	} {
		status, out := post(c.body)
		if status != c.want {
			t.Errorf("POST %s = %d %v, want %d", c.body, status, out, c.want)
		}
		if status >= 400 && out["error"] == "" {
			t.Errorf("POST %s: refused without a reason", c.body)
		}
	}
	r, _ := http.Get(server.URL + "/leases")
	var list []Lease
	json.NewDecoder(r.Body).Decode(&list)
	r.Body.Close()
	if len(list) != 2 || list[0].Host != "10.77.0.10" {
		t.Fatalf("GET /leases: %+v", list)
	}
	del := func(id string) int {
		request, _ := http.NewRequest("DELETE", server.URL+"/leases/"+id, nil)
		r, err := http.DefaultClient.Do(request)
		if err != nil {
			t.Fatal(err)
		}
		r.Body.Close()
		return r.StatusCode
	}
	if got := del(list[0].ID); got != 204 {
		t.Fatalf("DELETE = %d", got)
	}
	if got := del(list[0].ID); got != 404 {
		t.Fatalf("second DELETE = %d", got)
	}
	page, _ := http.Get(server.URL + "/")
	if page.StatusCode != 200 || !strings.HasPrefix(page.Header.Get("Content-Type"), "text/html") {
		t.Fatalf("GET /: %d %s", page.StatusCode, page.Header.Get("Content-Type"))
	}
	page.Body.Close()
}
