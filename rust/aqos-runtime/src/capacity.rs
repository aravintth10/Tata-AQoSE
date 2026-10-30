//! Capacity proposals from reflector delay and recently achieved throughput.
use crate::{Result, now_ns};
use std::{
    collections::{HashMap, VecDeque},
    io,
    net::{SocketAddr, UdpSocket},
};
#[derive(Clone)]
pub struct Config {
    pub base: [i64; 2],
    pub minimum: [i64; 2],
    pub reflectors: usize,
    pub deaf_hold: bool,
    pub raise_every_ns: i64,
}
#[derive(Default, Clone)]
struct Reflector {
    base: f64,
    have: bool,
    over: u32,
    delay: f64,
    answering: bool,
    on_time: bool,
    waiting_only: bool,
}
#[derive(Default)]
struct Direction {
    shaper: i64,
    achieved: f64,
    readings: VecDeque<(i64, f64)>,
    have: bool,
    last_cut: i64,
    first_cut: i64,
    last_raise: Option<i64>,
    cut: bool,
}
pub struct Estimator {
    pub config: Config,
    reflectors: Vec<Reflector>,
    dirs: [Direction; 2],
    calm_since: Option<i64>,
    pub deaf: bool,
    recent: VecDeque<f64>,
}
#[derive(Debug)]
pub struct Change {
    pub direction: usize,
    pub rate: i64,
    pub previous: i64,
    pub reason: String,
}
impl Estimator {
    pub fn new(config: Config) -> Result<Self> {
        if config.reflectors < 1
            || config.reflectors > 32
            || config.raise_every_ns < 0
            || (0..2).any(|i| config.minimum[i] < 1 || config.base[i] < config.minimum[i])
        {
            return Err("invalid capacity configuration".into());
        }
        Ok(Self {
            reflectors: vec![Reflector::default(); config.reflectors],
            config,
            dirs: Default::default(),
            calm_since: None,
            deaf: false,
            recent: VecDeque::with_capacity(6),
        })
    }
    pub fn probe(&mut self, index: usize, rtt: f64, answered: bool, waiting: f64, silent: f64) {
        let Some(r) = self.reflectors.get_mut(index) else {
            return;
        };
        r.answering = silent.is_finite() && silent < 3000.0;
        if !r.answering {
            r.over = 0;
            return;
        }
        let timed = answered && rtt.is_finite() && rtt >= 0.0;
        r.on_time = false;
        r.waiting_only = !timed;
        let mut delay = if waiting.is_finite() {
            waiting.max(0.0)
        } else {
            0.0
        };
        if timed {
            if !r.have || rtt < r.base {
                r.base = rtt;
                r.have = true;
            } else {
                r.base += (rtt - r.base) * 0.002;
            }
            delay = delay.max(rtt);
        }
        if !r.have {
            return;
        }
        r.delay = delay - r.base;
        if r.delay > 15.0 {
            r.over = r.over.saturating_add(1);
        } else {
            r.over = 0;
            r.on_time = timed;
        }
    }
    pub fn congested(&self) -> (bool, usize) {
        let (mut over, mut late, mut on_time, mut answering) = (0, 0, 0, 0);
        for r in &self.reflectors {
            if !r.answering {
                continue;
            }
            answering += 1;
            if r.on_time {
                on_time += 1;
            }
            if r.over >= 3 {
                over += 1;
                if !r.waiting_only {
                    late += 1;
                }
            }
        }
        (
            answering > 0 && over * 2 > answering && (on_time == 0 || late * 2 > answering),
            answering,
        )
    }
    pub fn delay(&self) -> (f64, bool) {
        let mut values: Vec<_> = self
            .reflectors
            .iter()
            .filter(|r| r.answering && r.have)
            .map(|r| r.delay)
            .collect();
        values.sort_by(f64::total_cmp);
        (
            values.get(values.len() / 2).copied().unwrap_or(0.0),
            values.len() >= 2 && !self.deaf,
        )
    }
    pub fn load(&mut self, dir: usize, rate: i64, achieved: f64, now: i64) {
        let Some(d) = self.dirs.get_mut(dir) else {
            return;
        };
        if rate < 1 || !achieved.is_finite() || achieved < 0.0 {
            return;
        }
        while d
            .readings
            .front()
            .is_some_and(|(at, _)| now.saturating_sub(*at) >= 600_000_000)
        {
            d.readings.pop_front();
        }
        if d.readings.len() >= 64 {
            d.readings.pop_front();
        }
        d.readings.push_back((now, achieved));
        d.shaper = rate;
        d.have = true;
        d.achieved = d.readings.iter().map(|(_, v)| *v).fold(0.0, f64::max);
    }
    pub fn applied(&mut self, dir: usize, rate: i64) {
        if rate < 1 {
            return;
        }
        if let Some(d) = self.dirs.get_mut(dir) {
            d.shaper = rate;
            d.achieved = d.achieved.min(rate as f64);
            for (_, v) in &mut d.readings {
                *v = v.min(rate as f64);
            }
        }
    }
    pub fn decide(&mut self, now: i64) -> Vec<Change> {
        let calm = *self.calm_since.get_or_insert(now);
        let (congested, answering) = self.congested();
        let mut out = Vec::new();
        let propose = |out: &mut Vec<Change>, i: usize, d: &Direction, rate, reason| {
            out.push(Change {
                direction: i,
                rate,
                previous: d.shaper,
                reason,
            })
        };
        if answering < 2 {
            self.deaf = true;
            self.calm_since = Some(now);
            if !self.config.deaf_hold {
                for i in 0..2 {
                    let d = &self.dirs[i];
                    if d.have && d.shaper > self.config.minimum[i] {
                        propose(
                            &mut out,
                            i,
                            d,
                            self.config.minimum[i],
                            format!(
                                "Only {answering} of {} delay probes are being answered; holding the {}load rate at its minimum.",
                                self.config.reflectors,
                                ["up", "down"][i]
                            ),
                        );
                    }
                }
            }
            return out;
        }
        self.deaf = false;
        if congested {
            self.calm_since = Some(now);
            if self.recent.len() == 6 {
                self.recent.pop_front();
            }
            self.recent.push_back(self.delay().0);
            let draining = self.recent.len() == 6 && self.recent[5] < self.recent[0] - 10.0;
            for i in 0..2 {
                let down = &self.dirs[1];
                let defer_up = down.have
                    && down.achieved >= 0.5 * down.shaper as f64
                    && (!down.cut || now.saturating_sub(down.first_cut) < 2_000_000_000);
                let d = &mut self.dirs[i];
                if !d.have {
                    continue;
                }
                let (target, how) = if i == 1 {
                    if d.achieved < 0.02 * d.shaper as f64
                        || d.cut && (now.saturating_sub(d.last_cut) < 300_000_000 || draining)
                    {
                        continue;
                    }
                    (
                        (0.90 * d.achieved.min(d.shaper as f64)) as i64,
                        format!("{:.0} kbit/s got through a moment ago", d.achieved),
                    )
                } else {
                    if d.achieved < 0.75 * d.shaper as f64 / 2.0
                        || d.cut && (now.saturating_sub(d.last_cut) < 1_000_000_000 || draining)
                        || defer_up
                    {
                        continue;
                    }
                    (
                        (0.75 * d.shaper as f64) as i64,
                        "what gets through cannot be measured from here, so by a fixed share"
                            .into(),
                    )
                };
                let target = target.max(self.config.minimum[i]);
                if target >= d.shaper {
                    continue;
                }
                if !d.cut {
                    d.first_cut = now;
                }
                d.cut = true;
                d.last_cut = now;
                propose(
                    &mut out,
                    i,
                    d,
                    target,
                    format!(
                        "Delay rose on most probes, so a queue is building beyond the router; lowering the {}load rate ({how}).",
                        ["up", "down"][i]
                    ),
                );
            }
            return out;
        }
        self.recent.clear();
        if now.saturating_sub(calm) < 2_000_000_000 {
            return out;
        }
        for d in &mut self.dirs {
            d.cut = false;
        }
        for i in 0..2 {
            let d = &mut self.dirs[i];
            if !d.have
                || d.shaper >= self.config.base[i]
                || d.last_raise
                    .is_some_and(|at| now.saturating_sub(at) < self.config.raise_every_ns)
            {
                continue;
            }
            let target = self.config.base[i].min(
                d.shaper
                    .saturating_add(1)
                    .max((d.shaper as f64 * 1.1) as i64),
            );
            let dir = ["up", "down"][i];
            let reason = if d.achieved >= 0.75 * d.shaper as f64 {
                format!("Delay has stayed low with the {dir}load busy; trying a higher rate.")
            } else {
                format!(
                    "Delay is low and the {dir}load is quiet; returning toward the configured rate."
                )
            };
            d.last_raise = Some(now);
            propose(&mut out, i, d, target, reason);
        }
        out
    }
}
struct Target {
    socket: UdpSocket,
    out: HashMap<u64, i64>,
    last_answer: i64,
    rtt: f64,
    answered: bool,
}
pub struct Prober {
    targets: Vec<Target>,
    next: i64,
    seq: u64,
    nonce: [u8; 8],
}
impl Prober {
    pub fn new(addresses: &[SocketAddr]) -> Result<Self> {
        if addresses.is_empty() || addresses.len() > 32 {
            return Err("one to 32 reflectors required".into());
        }
        let mut targets = Vec::new();
        let now = now_ns();
        for address in addresses {
            let socket = UdpSocket::bind(if address.is_ipv4() {
                "0.0.0.0:0"
            } else {
                "[::]:0"
            })?;
            socket.connect(address)?;
            socket.set_nonblocking(true)?;
            targets.push(Target {
                socket,
                out: HashMap::with_capacity(32),
                last_answer: now,
                rtt: 0.0,
                answered: false,
            });
        }
        let mut nonce = [0; 8];
        getrandom::fill(&mut nonce)?;
        Ok(Self {
            targets,
            next: now,
            seq: 0,
            nonce,
        })
    }
    /// Nonblocking reads run frequently; a new round is published every 100 ms.
    pub fn poll(&mut self, e: &mut Estimator, now: i64) -> bool {
        for t in &mut self.targets {
            for _ in 0..64 {
                let mut b = [0; 64];
                match t.socket.recv(&mut b) {
                    Ok(16) if b[8..16] == self.nonce => {
                        let seq = u64::from_be_bytes(b[..8].try_into().expect("eight bytes"));
                        if let Some(at) = t.out.remove(&seq) {
                            t.rtt = (now - at) as f64 / 1e6;
                            t.last_answer = now;
                            t.answered = true;
                        }
                    }
                    Ok(_) => {}
                    Err(err) if err.kind() == io::ErrorKind::WouldBlock => break,
                    Err(_) => break,
                }
            }
        }
        if now < self.next {
            return false;
        }
        let publish = self.seq > 0;
        self.next = now.saturating_add(100_000_000);
        self.seq = self.seq.wrapping_add(1);
        let mut b = [0; 16];
        b[..8].copy_from_slice(&self.seq.to_be_bytes());
        b[8..].copy_from_slice(&self.nonce);
        for (i, t) in self.targets.iter_mut().enumerate() {
            t.out
                .retain(|_, at| now.saturating_sub(*at) <= 3_000_000_000);
            let oldest = t.out.values().min().copied().unwrap_or(now);
            if publish {
                e.probe(
                    i,
                    t.rtt,
                    t.answered,
                    (now - oldest) as f64 / 1e6,
                    (now - t.last_answer) as f64 / 1e6,
                );
            }
            t.answered = false;
            t.out.insert(self.seq, now);
            let _ = t.socket.send(&b);
        }
        publish
    }
}
