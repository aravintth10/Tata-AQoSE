use serde::Serialize;
#[derive(Clone, Copy, Debug, Default)]
pub struct Sample {
    pub ts: f64,
    pub tin: Option<f64>,
    pub probe: Option<f64>,
}
#[derive(Clone, Copy)]
pub struct Config {
    pub before: f64,
    pub window: f64,
    pub minimum: usize,
    pub band_ms: f64,
    pub band_rel: f64,
}
impl Default for Config {
    fn default() -> Self {
        Self {
            before: 10.0,
            window: 5.0,
            minimum: 3,
            band_ms: 5.0,
            band_rel: 0.5,
        }
    }
}
#[derive(Serialize, Debug)]
pub struct Judgement {
    pub verdict: &'static str,
    pub reason: String,
    pub tin_delay_ms: [f64; 3],
    pub probe_delay_ms: [f64; 3],
}
pub fn judge(cfg: Config, samples: &[Sample], at: f64) -> Judgement {
    let mut out = Judgement {
        verdict: "neutral",
        reason: String::new(),
        tin_delay_ms: [-1.0; 3],
        probe_delay_ms: [-1.0; 3],
    };
    let edges = [at - cfg.before, at, at + cfg.window, at + 2.0 * cfg.window];
    let (mut worse, mut better, mut judged) = (None, None, 0);
    for metric in 0..2 {
        let values = if metric == 0 {
            &mut out.tin_delay_ms
        } else {
            &mut out.probe_delay_ms
        };
        for i in 0..3 {
            let (mut sum, mut count) = (0.0, 0);
            for s in samples {
                if s.ts >= edges[i]
                    && s.ts < edges[i + 1]
                    && let Some(v) = if metric == 0 { s.tin } else { s.probe }
                    && v.is_finite()
                    && v >= 0.0
                {
                    sum += v;
                    count += 1;
                }
            }
            if count >= cfg.minimum && count > 0 {
                values[i] = sum / count as f64;
            }
        }
        let [b, a, c] = *values;
        if b < 0.0 || a < 0.0 || c < 0.0 {
            continue;
        }
        judged += 1;
        let band = cfg.band_ms.max(cfg.band_rel * b);
        let name = if metric == 0 {
            "delay in the Voice and Video tins"
        } else {
            "probe delay"
        };
        if a - b > band && c - b > band {
            worse = Some(format!(
                "{name} rose from {b:.1} ms to {a:.1} and {c:.1} ms"
            ));
        } else if b - a > band && b - c > band {
            better = Some(format!(
                "{name} fell from {b:.1} ms to {a:.1} and {c:.1} ms"
            ));
        }
    }
    if let Some(reason) = worse {
        out.verdict = "regressed";
        out.reason = reason;
    } else if let Some(reason) = better {
        out.verdict = "improved";
        out.reason = reason;
    } else {
        out.reason = if judged == 0 {
            "too few samples to judge"
        } else {
            "no change beyond the noise band"
        }
        .into();
    }
    out
}
