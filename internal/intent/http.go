package intent

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/netip"
	"strings"
	"time"
)

// Handler serves the lease API and a one-page form:
//
//	POST   /leases        {"host": "10.77.0.10", "minutes": 45}
//	GET    /leases
//	DELETE /leases/<id>
//	GET    /              the form
func (s *Store) Handler() http.Handler {
	mux := http.NewServeMux()
	reply := func(w http.ResponseWriter, status int, v any) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		json.NewEncoder(w).Encode(v)
	}
	refuse := func(w http.ResponseWriter, status int, err error) {
		reply(w, status, map[string]string{"error": err.Error()})
	}
	mux.HandleFunc("GET /leases", func(w http.ResponseWriter, r *http.Request) { reply(w, http.StatusOK, s.List()) })
	mux.HandleFunc("POST /leases", func(w http.ResponseWriter, r *http.Request) {
		var request struct {
			Host    string  `json:"host"`
			Minutes float64 `json:"minutes"`
		}
		body := http.MaxBytesReader(w, r.Body, 4096)
		decoder := json.NewDecoder(body)
		decoder.DisallowUnknownFields()
		if err := decoder.Decode(&request); err != nil {
			refuse(w, http.StatusBadRequest, errors.New("the request must be JSON with a host and a number of minutes"))
			return
		}
		host, err := netip.ParseAddr(strings.TrimSpace(request.Host))
		if err != nil {
			refuse(w, http.StatusBadRequest, errors.New("host must be an IP address"))
			return
		}
		lease, err := s.Create(host, time.Duration(request.Minutes*float64(time.Minute)))
		switch {
		case err == nil:
			reply(w, http.StatusCreated, lease)
		case errors.Is(err, ErrTooMany), errors.Is(err, ErrHasLease):
			refuse(w, http.StatusConflict, err)
		case errors.Is(err, ErrUnknownHost):
			refuse(w, http.StatusNotFound, err)
		default:
			refuse(w, http.StatusBadRequest, err)
		}
	})
	mux.HandleFunc("DELETE /leases/{id}", func(w http.ResponseWriter, r *http.Request) {
		if err := s.Delete(r.PathValue("id")); err != nil {
			refuse(w, http.StatusNotFound, err)
			return
		}
		w.WriteHeader(http.StatusNoContent)
	})
	mux.HandleFunc("GET /{$}", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Write([]byte(page))
	})
	return mux
}

// page is the whole user interface. It plays no part in expiry: close it
// and the lease still ends on time.
const page = `<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Priority for a device</title>
<style>
 body { font: 16px/1.5 system-ui, sans-serif; max-width: 34rem; margin: 2rem auto; padding: 0 1rem; }
 input, button { font: inherit; padding: .4rem .6rem; } label { display: block; margin: .8rem 0 .2rem; }
 li { margin: .4rem 0; } #said { color: #a40000; min-height: 1.5em; }
</style>
<h1>Priority for a device</h1>
<p>Calls and games on the device are treated as more important until the time runs out.
At most two devices at once, for at most two hours.</p>
<form id="f">
 <label for="host">Device address</label><input id="host" placeholder="10.77.0.10" required>
 <label for="minutes">Minutes</label><input id="minutes" type="number" min="1" max="120" value="45" required>
 <p><button>Give priority</button></p>
</form>
<p id="said" role="alert"></p>
<h2>Active now</h2><ul id="list"></ul>
<script>
const said = document.getElementById('said'), list = document.getElementById('list');
async function show() {
  const leases = await (await fetch('/leases')).json();
  list.replaceChildren(...leases.map(l => {
    const li = document.createElement('li'), end = document.createElement('button');
    li.textContent = l.host + ', ' + Math.ceil(l.seconds_left / 60) + ' min left ';
    end.textContent = 'End now';
    end.onclick = async () => { await fetch('/leases/' + l.id, {method: 'DELETE'}); show(); };
    li.append(end); return li;
  }));
  if (!leases.length) list.innerHTML = '<li>None.</li>';
}
document.getElementById('f').onsubmit = async e => {
  e.preventDefault();
  const r = await fetch('/leases', {method: 'POST', body: JSON.stringify({
    host: document.getElementById('host').value, minutes: Number(document.getElementById('minutes').value)})});
  said.textContent = r.ok ? '' : (await r.json()).error;
  show();
};
show(); setInterval(show, 15000);
</script>
`
