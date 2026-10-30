// Package heartbeat is the controller's side of the watchdog: it touches a
// file from the controller's own main loop, so a loop that hangs stops the
// heartbeat as surely as a crash does.
package heartbeat

import (
	"os"
	"path/filepath"
	"time"
)

// Interval is how often the controller beats. The watchdog allows two
// seconds of silence.
const Interval = 250 * time.Millisecond

// DefaultPath is per user, so the rootless testbed can write it.
func DefaultPath() string {
	dir := os.Getenv("XDG_RUNTIME_DIR")
	if dir == "" {
		dir = "/run"
	}
	return filepath.Join(dir, "aqos", "heartbeat")
}

// Beat records that the controller is alive now.
func Beat(path string) error {
	now := time.Now()
	if err := os.Chtimes(path, now, now); err == nil {
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	file, err := os.Create(path)
	if err != nil {
		return err
	}
	return file.Close()
}
