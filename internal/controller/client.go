// Package controller joins the parts into one pipeline: the extractor's
// feature records go to the ML service, its proposals to the safety
// shield, and the shield's verdicts to the enforcer. A failure at any step
// leaves the flow unlabelled, which is plain CAKE.
package controller

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"sync"
	"time"

	"adaptiveqos/contracts"
)

var (
	// ErrBusy means too many requests are already waiting for the service.
	ErrBusy = errors.New("ml service: too many requests pending")
	// ErrDown means there is no connection to the service right now.
	ErrDown = errors.New("ml service: not connected")
	// ErrTimeout means the service did not answer within the deadline.
	ErrTimeout = errors.New("ml service: no answer within the deadline")
)

// Result is the outcome of one classify request: a response, or the
// reason there is none.
type Result struct {
	FlowID string
	Resp   *contracts.ServiceResponse
	Err    error
	// TookNs is the time from sending the request to the result.
	TookNs int64
}

type pending struct {
	flowID string
	sent   time.Time
	health chan *contracts.ServiceResponse // set for a health request
}

// Client talks to aqos_ml.serve over its Unix socket. Requests are sent
// without waiting; answers and timeouts arrive on Results.
type Client struct {
	Path     string
	Deadline time.Duration // per request (starting value 50 ms)
	Max      int           // requests allowed to be pending (starting value 1,024)

	mu       sync.Mutex
	conn     net.Conn
	lastDial time.Time
	pending  map[uint64]pending
	next     uint64
	results  chan Result
	stop     chan struct{}
}

func NewClient(path string, deadline time.Duration, max int) *Client {
	c := &Client{
		Path: path, Deadline: deadline, Max: max,
		pending: map[uint64]pending{}, results: make(chan Result, 4*max), stop: make(chan struct{}),
	}
	go c.sweep()
	return c
}

// Results delivers one Result for every request Classify accepted.
func (c *Client) Results() <-chan Result { return c.results }

func (c *Client) Close() {
	close(c.stop)
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.conn != nil {
		c.conn.Close()
	}
}

// connect returns the connection, dialling at most once a second. Called
// with mu held.
func (c *Client) connect() net.Conn {
	if c.conn != nil || time.Since(c.lastDial) < time.Second {
		return c.conn
	}
	c.lastDial = time.Now()
	conn, err := net.DialTimeout("unix", c.Path, 200*time.Millisecond)
	if err != nil {
		return nil
	}
	c.conn = conn
	go c.read(conn)
	return conn
}

// send writes the request build returns for the id it is given.
func (c *Client) send(build func(id uint64) any, p pending) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	conn := c.connect()
	if conn == nil {
		return ErrDown
	}
	if len(c.pending) >= c.Max {
		return ErrBusy
	}
	c.next++
	id := c.next
	line, err := json.Marshal(build(id))
	if err != nil {
		return err
	}
	conn.SetWriteDeadline(time.Now().Add(c.Deadline))
	if _, err := conn.Write(append(line, '\n')); err != nil {
		c.drop(conn)
		return ErrDown
	}
	p.sent = time.Now()
	c.pending[id] = p
	return nil
}

// Classify asks for a classification and a proposal for one feature
// record. An error means nothing was sent and no Result will come.
func (c *Client) Classify(rec *contracts.FeatureRecord, previous *contracts.Tin, leased []contracts.Class) error {
	if leased == nil {
		leased = []contracts.Class{}
	}
	return c.send(func(id uint64) any {
		return classifyRequest{Op: "classify", ID: id, Record: rec, PreviousTin: previous, LeasedClasses: leased}
	}, pending{flowID: rec.FlowID})
}

// Health asks the service what it is running and waits for the answer.
func (c *Client) Health(timeout time.Duration) (*contracts.ServiceResponse, error) {
	answer := make(chan *contracts.ServiceResponse, 1)
	deadline := time.Now().Add(timeout)
	for {
		err := c.send(func(id uint64) any { return contracts.ServiceRequest{Op: "health", ID: id} }, pending{health: answer})
		if err == nil {
			break
		}
		if time.Now().After(deadline) {
			return nil, err
		}
		time.Sleep(100 * time.Millisecond)
		c.mu.Lock()
		c.lastDial = time.Time{} // starting up: do not wait a second between tries
		c.mu.Unlock()
	}
	select {
	case resp := <-answer:
		if resp == nil || !resp.OK {
			return nil, fmt.Errorf("ml service: health check failed")
		}
		return resp, nil
	case <-time.After(time.Until(deadline)):
		return nil, ErrTimeout
	}
}

// drop closes a connection and fails everything waiting on it. Called
// with mu held.
func (c *Client) drop(conn net.Conn) {
	if c.conn != conn {
		return
	}
	conn.Close()
	c.conn = nil
	for id, p := range c.pending {
		delete(c.pending, id)
		c.deliver(p, nil, ErrDown)
	}
}

func (c *Client) deliver(p pending, resp *contracts.ServiceResponse, err error) {
	if p.health != nil {
		p.health <- resp
		return
	}
	select {
	case c.results <- Result{FlowID: p.flowID, Resp: resp, Err: err, TookNs: int64(time.Since(p.sent))}:
	default:
		// Nobody is reading results. The flow stays unlabelled, which is
		// what a lost result means anyway.
	}
}

func (c *Client) read(conn net.Conn) {
	scanner := bufio.NewScanner(conn)
	scanner.Buffer(make([]byte, 64<<10), 1<<20)
	for scanner.Scan() {
		var resp contracts.ServiceResponse
		if json.Unmarshal(scanner.Bytes(), &resp) != nil || resp.ID == nil {
			continue
		}
		c.mu.Lock()
		p, ok := c.pending[*resp.ID]
		delete(c.pending, *resp.ID)
		if ok {
			c.deliver(p, &resp, nil)
		}
		c.mu.Unlock()
	}
	c.mu.Lock()
	c.drop(conn)
	c.mu.Unlock()
}

// sweep gives up on requests the service has not answered in time. A late
// answer then finds nothing pending and is ignored.
func (c *Client) sweep() {
	tick := time.NewTicker(5 * time.Millisecond)
	defer tick.Stop()
	for {
		select {
		case <-c.stop:
			return
		case <-tick.C:
		}
		c.mu.Lock()
		for id, p := range c.pending {
			limit := c.Deadline
			if p.health != nil {
				limit = 5 * time.Second
			}
			if time.Since(p.sent) > limit {
				delete(c.pending, id)
				c.deliver(p, nil, ErrTimeout)
			}
		}
		c.mu.Unlock()
	}
}

// classifyRequest is contracts.ServiceRequest for op "classify". The
// contract requires previous_tin and leased_classes to be present even
// when null or empty, which the shared type's omitempty would drop.
type classifyRequest struct {
	Op            string                   `json:"op"`
	ID            uint64                   `json:"id"`
	Record        *contracts.FeatureRecord `json:"record"`
	PreviousTin   *contracts.Tin           `json:"previous_tin"`
	LeasedClasses []contracts.Class        `json:"leased_classes"`
}
