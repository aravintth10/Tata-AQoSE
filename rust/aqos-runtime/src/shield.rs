//! Pure policy checking; committed state follows verified kernel writes.
use crate::{
    Result, Tin,
    contracts::{Applied, Proposal, Verdict},
    is_id,
};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub min_rate_kbit: HashMap<String, i64>,
    pub max_rate_kbit: HashMap<String, i64>,
    pub max_increase: f64,
    pub max_decrease: f64,
    pub increase_interval_ms: i64,
    pub flip_limit: usize,
    pub flip_window_ms: i64,
    pub remark_budget: usize,
}
impl Config {
    pub fn load(path: impl AsRef<std::path::Path>) -> Result<Self> {
        let c: Self = serde_saphyr::from_str(&std::fs::read_to_string(path)?)?;
        c.validate()?;
        Ok(c)
    }
    pub fn validate(&self) -> Result<()> {
        if self.min_rate_kbit.len() != 2 || self.max_rate_kbit.len() != 2 {
            return Err("rates require exactly up and down".into());
        }
        for dir in ["up", "down"] {
            let lo = self.min_rate_kbit.get(dir).copied().unwrap_or(0);
            let hi = self.max_rate_kbit.get(dir).copied().unwrap_or(0);
            if lo < 1 || hi < lo || hi > (1_i64 << 53) {
                return Err("invalid shaper bounds".into());
            }
        }
        if !self.max_increase.is_finite()
            || self.max_increase <= 0.0
            || self.max_increase > 1.0
            || !self.max_decrease.is_finite()
            || self.max_decrease <= 0.0
            || self.max_decrease >= 1.0
            || self.increase_interval_ms < 0
            || self.flip_window_ms <= 0
            || self.remark_budget < 1
            || self.increase_interval_ms.checked_mul(1_000_000).is_none()
            || self.flip_window_ms.checked_mul(1_000_000).is_none()
        {
            return Err("invalid shield limits".into());
        }
        Ok(())
    }
    pub fn allowed(&self, dir: &str, current: i64) -> Option<(i64, i64)> {
        let floor = *self.min_rate_kbit.get(dir)?;
        let ceiling = *self.max_rate_kbit.get(dir)?;
        if floor > ceiling {
            return None;
        }
        let down = (current as f64 * (1.0 - self.max_decrease)).ceil() as i64;
        let up = (ceiling as f64).min((current as f64 * (1.0 + self.max_increase)).floor()) as i64;
        Some((down.clamp(floor, ceiling), up.clamp(floor, ceiling)))
    }
}
#[derive(Default, Clone, Debug)]
pub struct Flow {
    pub host_id: String,
    pub tin: Option<Tin>,
    pub changes: Vec<i64>,
    pub pinned: bool,
    pub pending_clear: bool,
}
#[derive(Default, Clone, Debug)]
pub struct Rate {
    pub kbit: i64,
    pub last_increase: Option<i64>,
}
#[derive(Default, Debug)]
pub struct State {
    pub flows: HashMap<String, Flow>,
    pub rates: HashMap<String, Rate>,
    pub remarks: Vec<i64>,
    pub starvation: bool,
}
pub struct Outcome {
    pub verdict: Verdict,
    pub code: &'static str,
    pub unlabel: bool,
}
fn recent(times: &[i64], now: i64, window: i64) -> usize {
    times
        .iter()
        .filter(|&&t| t <= now && i128::from(now) - i128::from(t) < i128::from(window))
        .count()
}
pub fn check(cfg: &Config, state: &State, p: Option<&Proposal>, now: i64) -> Outcome {
    // The pure checker never mutates state across an unwind boundary.
    std::panic::catch_unwind(|| check_inner(cfg, state, p, now)).unwrap_or_else(|_| {
        reject(
            p,
            now,
            "internal",
            "The safety shield failed; nothing was changed.".into(),
        )
    })
}
fn reject(p: Option<&Proposal>, now: i64, code: &'static str, reason: String) -> Outcome {
    Outcome {
        verdict: Verdict {
            schema_version: 1,
            proposal_id: p
                .filter(|p| is_id(&p.proposal_id))
                .map_or("0000000000000000", |p| p.proposal_id.as_str())
                .into(),
            ts_ns: now.max(0),
            verdict: "reject".into(),
            reason,
            applied: None,
        },
        code,
        unlabel: code == "flip_cap",
    }
}
fn check_inner(cfg: &Config, st: &State, p: Option<&Proposal>, now: i64) -> Outcome {
    let no = |code, reason: &str| reject(p, now, code, reason.into());
    if cfg.validate().is_err() {
        return no(
            "internal",
            "The safety shield is misconfigured; nothing was changed.",
        );
    }
    let Some(p) = p else {
        return no("invalid", "Not a valid proposal: it could not be read.");
    };
    if let Err(e) = p.validate() {
        return no("invalid", &format!("Not a valid proposal: {e}."));
    }
    let pass = |code, reason: &str, applied: Applied, clamped: bool| Outcome {
        verdict: Verdict {
            schema_version: 1,
            proposal_id: p.proposal_id.clone(),
            ts_ns: now.max(0),
            verdict: if clamped { "clamp" } else { "accept" }.into(),
            reason: reason.into(),
            applied: Some(applied),
        },
        code,
        unlabel: false,
    };
    if let Some(l) = &p.flow_label {
        let Some(f) = st.flows.get(&l.flow_id) else {
            return no(
                "unknown_flow",
                "The flow is not in the flow table; it may have ended.",
            );
        };
        if f.host_id != l.host_id {
            return no(
                "unknown_flow",
                "The flow belongs to a different device than the proposal says.",
            );
        }
        if f.pinned {
            return no(
                "pinned",
                "The flow changed label too often and is pinned to unlabelled.",
            );
        }
        if l.previous_tin != f.tin {
            return no(
                "stale",
                "The flow's label changed since this proposal was made.",
            );
        }
        let applied = Applied {
            tin: Some(l.tin),
            dscp: Some(l.dscp.clone()),
            rate_kbit: None,
        };
        if Some(l.tin) == f.tin {
            return pass("ok", "The flow is already in that tin.", applied, false);
        }
        if st.starvation && [Tin::Voice, Tin::Video].contains(&l.tin) {
            return no(
                "starvation",
                "A device is being starved; promotions to Voice and Video are suspended.",
            );
        }
        if f.tin.is_some() {
            if recent(&f.changes, now, cfg.flip_window_ms * 1_000_000) >= cfg.flip_limit {
                return no(
                    "flip_cap",
                    &format!(
                        "The flow's label has already changed {} times in {} s; it is pinned to unlabelled.",
                        cfg.flip_limit,
                        cfg.flip_window_ms / 1000
                    ),
                );
            }
            if recent(&st.remarks, now, 1_000_000_000) >= cfg.remark_budget {
                return no(
                    "remark_budget",
                    &format!(
                        "More than {} label changes in the last second; this one waits.",
                        cfg.remark_budget
                    ),
                );
            }
        }
        return pass("ok", "Within every bound.", applied, false);
    }
    let Some(r) = &p.shaper_rate else {
        return no("invalid", "No rate operation.");
    };
    let current = st.rates.get(&r.direction).cloned().unwrap_or_default();
    if current.kbit < 1 {
        return no(
            "stale",
            &format!("The current {}load rate is not known yet.", r.direction),
        );
    }
    if r.previous_rate_kbit.is_some_and(|v| v != current.kbit) {
        return no(
            "stale",
            &format!(
                "The {}load rate changed since this proposal was made.",
                r.direction
            ),
        );
    }
    let Some((lo, hi)) = cfg.allowed(&r.direction, current.kbit) else {
        return no("internal", "Invalid direction.");
    };
    let target = r.rate_kbit.clamp(lo, hi);
    let mut code = "ok";
    let mut reason = "Within every bound.".to_owned();
    if target != r.rate_kbit {
        if target == cfg.min_rate_kbit[&r.direction] || target == cfg.max_rate_kbit[&r.direction] {
            code = "rate_bounds";
            reason = format!(
                "Rate limited to the configured range, {} to {} kbit/s.",
                cfg.min_rate_kbit[&r.direction], cfg.max_rate_kbit[&r.direction]
            );
        } else {
            code = "rate_step";
            reason = if target > current.kbit {
                "Rate increase limited to the maximum upward step."
            } else {
                "Rate decrease limited to the maximum downward step."
            }
            .into();
        }
    }
    if target == current.kbit && r.rate_kbit != current.kbit {
        return no(
            "no_change",
            &format!(
                "The {}load rate is already at the limit in that direction.",
                r.direction
            ),
        );
    }
    if target > current.kbit
        && current
            .last_increase
            .is_some_and(|at| now.saturating_sub(at) < cfg.increase_interval_ms * 1_000_000)
    {
        return no(
            "rate_interval",
            &format!(
                "The {}load rate was raised less than {} s ago.",
                r.direction,
                cfg.increase_interval_ms / 1000
            ),
        );
    }
    pass(
        code,
        &reason,
        Applied {
            rate_kbit: Some(target),
            ..Applied::default()
        },
        target != r.rate_kbit,
    )
}
pub struct Tracker {
    pub config: Config,
    pub state: State,
    pub counts: HashMap<String, u64>,
}
impl Tracker {
    pub fn new(config: Config) -> Result<Self> {
        config.validate()?;
        Ok(Self {
            config,
            state: State::default(),
            counts: HashMap::new(),
        })
    }
    pub fn seen(&mut self, id: &str, host: &str) {
        self.state.flows.entry(id.into()).or_insert_with(|| Flow {
            host_id: host.into(),
            ..Flow::default()
        });
    }
    pub fn decide(&mut self, p: &Proposal, now: i64) -> Outcome {
        self.state
            .remarks
            .retain(|&t| t <= now && now.saturating_sub(t) < 1_000_000_000);
        let out = check(&self.config, &self.state, Some(p), now);
        *self.counts.entry(out.code.into()).or_default() += 1;
        if out.unlabel
            && let Some(f) = p
                .flow_label
                .as_ref()
                .and_then(|l| self.state.flows.get_mut(&l.flow_id))
        {
            f.pinned = true;
            f.pending_clear = true;
        }
        out
    }
    /// Call only after the kernel confirmed mark zero. A failed clear remains pending.
    pub fn cleared(&mut self, id: &str) {
        if let Some(f) = self.state.flows.get_mut(id) {
            f.tin = None;
            f.changes.clear();
            f.pending_clear = false;
        }
    }
    pub fn applied(&mut self, p: &Proposal, v: &Verdict, now: i64) {
        let Some(a) = &v.applied else {
            return;
        };
        if let Some(l) = &p.flow_label {
            let Some(f) = self.state.flows.get_mut(&l.flow_id) else {
                return;
            };
            if f.pinned || f.tin == a.tin {
                return;
            }
            if f.tin.is_some() {
                f.changes.retain(|&t| {
                    t <= now && now.saturating_sub(t) < self.config.flip_window_ms * 1_000_000
                });
                f.changes.push(now);
                self.state.remarks.push(now);
            }
            f.tin = a.tin;
        } else if let Some(r) = &p.shaper_rate
            && let Some(kbit) = a.rate_kbit
        {
            let state = self.state.rates.entry(r.direction.clone()).or_default();
            if kbit > state.kbit {
                state.last_increase = Some(now);
            }
            state.kbit = kbit;
        }
    }
}
