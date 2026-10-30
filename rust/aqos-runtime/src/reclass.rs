use crate::{Class, Result, Tin, Window};
use std::collections::HashMap;
pub const FEATURES: [&str; 9] = [
    "pkts_up_per_s",
    "pkts_down_per_s",
    "bytes_up_per_s",
    "bytes_down_per_s",
    "mean_pkt_size_up",
    "mean_pkt_size_down",
    "pkt_size_spread",
    "rate_cv",
    "burstiness",
];
pub type Envelopes = HashMap<Class, HashMap<String, [f64; 2]>>;
pub fn load(path: &str) -> Result<Envelopes> {
    let env: Envelopes = serde_json::from_slice(&std::fs::read(path)?)?;
    validate(&env)?;
    Ok(env)
}
pub fn validate(env: &Envelopes) -> Result<()> {
    for ranges in env.values() {
        for name in FEATURES {
            let r = ranges
                .get(name)
                .ok_or_else(|| format!("missing envelope for {name}"))?;
            if !r[0].is_finite() || !r[1].is_finite() || r[0] > r[1] {
                return Err("invalid envelope bounds".into());
            }
        }
    }
    Ok(())
}
#[derive(Clone, Copy, Default, Debug)]
pub struct Excursion {
    pub feature: usize,
    pub value: f64,
    pub low: f64,
    pub high: f64,
    pub far: f64,
}
pub fn outside(env: &Envelopes, class: Class, w: &Window) -> Option<(usize, Excursion)> {
    let ranges = env.get(&class)?;
    let mut n = 0;
    let mut worst = Excursion::default();
    for (i, value) in [
        w.pkts_up_per_s,
        w.pkts_down_per_s,
        w.bytes_up_per_s,
        w.bytes_down_per_s,
        w.mean_pkt_size_up,
        w.mean_pkt_size_down,
        w.pkt_size_spread,
        w.rate_cv,
        w.burstiness,
    ]
    .into_iter()
    .enumerate()
    {
        let [low, high] = *ranges.get(FEATURES[i])?;
        let beyond = (low - value).max(value - high);
        if beyond <= 0.0 {
            continue;
        }
        n += 1;
        let far = beyond / (high - low).max(high.abs() * 0.05).max(1e-9);
        if far > worst.far {
            worst = Excursion {
                feature: i,
                value,
                low,
                high,
                far,
            };
        }
    }
    Some((n, worst))
}
#[derive(Clone, Copy)]
pub struct Config {
    pub k: u32,
    pub windows: u32,
    pub interactive: u32,
    pub slow_every: u32,
    pub recheck_min: u32,
    pub recheck_max: u32,
    pub backoff: u32,
    pub backoff_max: u32,
}
impl Default for Config {
    fn default() -> Self {
        Self {
            k: 2,
            windows: 3,
            interactive: 2,
            slow_every: 2,
            recheck_min: 30,
            recheck_max: 60,
            backoff: 5,
            backoff_max: 40,
        }
    }
}
struct State {
    class: Class,
    age: u32,
    outside: u32,
    worst: Excursion,
    cooldown: u32,
    backoff: u32,
    recheck_at: u32,
}
#[derive(Clone)]
pub struct Ask {
    pub recheck: bool,
    pub reason: String,
}
pub struct Watcher {
    pub config: Config,
    env: Envelopes,
    flows: HashMap<String, State>,
    rng: u64,
}
impl Watcher {
    pub fn new(mut config: Config, env: Envelopes, seed: u64) -> Result<Self> {
        validate(&env)?;
        config.k = config.k.max(1);
        config.windows = config.windows.max(1);
        config.interactive = config.interactive.max(1);
        config.slow_every = config.slow_every.max(1);
        Ok(Self {
            config,
            env,
            flows: HashMap::new(),
            rng: seed.max(1),
        })
    }
    fn next(&mut self) -> u32 {
        if self.config.recheck_max == 0 {
            return u32::MAX;
        }
        self.rng ^= self.rng << 13;
        self.rng ^= self.rng >> 7;
        self.rng ^= self.rng << 17;
        self.config.recheck_min
            + (self.rng
                % (u64::from(
                    self.config
                        .recheck_max
                        .saturating_sub(self.config.recheck_min),
                ) + 1)) as u32
    }
    pub fn answered(&mut self, id: &str, class: Class) {
        let next = self.next();
        let st = self.flows.entry(id.into()).or_insert(State {
            class,
            age: 0,
            outside: 0,
            worst: Default::default(),
            cooldown: 0,
            backoff: 0,
            recheck_at: next,
        });
        if st.class != class {
            st.class = class;
            st.cooldown = 0;
            st.backoff = 0;
        }
        st.age = 0;
        st.outside = 0;
        st.worst = Default::default();
        st.recheck_at = next;
    }
    pub fn gone(&mut self, id: &str) {
        self.flows.remove(id);
    }
    pub fn window(&mut self, id: &str, tin: Tin, w: &Window) -> Option<Ask> {
        let st = self.flows.get_mut(id)?;
        st.age = st.age.saturating_add(1);
        st.cooldown = st.cooldown.saturating_sub(1);
        if st.age >= st.recheck_at {
            let age = st.age;
            let next = self.next();
            self.flows.get_mut(id)?.recheck_at = age.saturating_add(next);
            return Some(Ask {
                recheck: true,
                reason: format!("Routine recheck after {age} s"),
            });
        }
        let (every, need) = if matches!(tin, Tin::Voice | Tin::Video) {
            (1, self.config.interactive)
        } else {
            (self.config.slow_every, self.config.windows)
        };
        if !st.age.is_multiple_of(every) {
            return None;
        }
        let (n, worst) = outside(&self.env, st.class, w)?;
        if n < self.config.k as usize {
            st.outside = 0;
            st.worst = Default::default();
            st.backoff = 0;
            return None;
        }
        st.outside = st.outside.saturating_add(1);
        if worst.far > st.worst.far {
            st.worst = worst;
        }
        if st.outside < need || st.cooldown > 0 {
            return None;
        }
        let names = [
            "Packets per second sent",
            "Packets per second received",
            "Bytes per second sent",
            "Bytes per second received",
            "Mean size of packets sent",
            "Mean size of packets received",
            "Spread of packet sizes",
            "Variation of the packet rate",
            "Burstiness",
        ];
        let class = match st.class {
            Class::Telephony => "a voice call",
            Class::RealtimeInteractive => "a game",
            Class::MultimediaConferencing => "a video call",
            Class::MultimediaStreaming => "streaming",
            Class::Standard => "ordinary traffic",
            Class::HighThroughputData => "a download",
            Class::LowPriorityData => "a background transfer",
        };
        let e = st.worst;
        let reason = format!(
            "{} was {} for {} s, outside the {} to {} normal for {class}",
            names[e.feature],
            number(e.value),
            st.outside * every,
            number(e.low),
            number(e.high)
        );
        st.backoff = st
            .backoff
            .saturating_mul(2)
            .max(self.config.backoff)
            .min(self.config.backoff_max.max(self.config.backoff));
        st.cooldown = st.backoff;
        st.outside = 0;
        st.worst = Default::default();
        Some(Ask {
            recheck: false,
            reason,
        })
    }
}
fn number(v: f64) -> String {
    if v.abs() >= 100.0 {
        return format!("{:.0}", v.round());
    }
    if v == 0.0 {
        return "0".into();
    }
    let places = (2.0 - v.abs().log10().floor()).clamp(0.0, 12.0) as usize;
    let s = format!("{v:.places$}");
    if s.contains('.') {
        s.trim_end_matches('0').trim_end_matches('.').into()
    } else {
        s
    }
}
