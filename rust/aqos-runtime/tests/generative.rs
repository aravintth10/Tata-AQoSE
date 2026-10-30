//! Reproducible, time-bounded generative stress of the production Rust shield.
//! This is not a coverage-guided fuzzer. Keep the oracle independent of allowed().
use aqos_runtime_rs::{
    Tin,
    contracts::{Proposal, default_dscp},
    extract::parse_packet,
    shield::{self, Config, Flow, Rate, State},
};
use std::time::{Duration, Instant};
struct Rng(u64);
impl Rng {
    fn next(&mut self) -> u64 {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 7;
        self.0 ^= self.0 << 17;
        self.0
    }
    fn number(&mut self) -> i64 {
        match self.next() % 8 {
            0 => i64::MIN,
            1 => i64::MAX,
            2 => 0,
            3 => -1,
            _ => (self.next() % 250000) as i64,
        }
    }
}
fn oracle(cfg: &Config, state: &State, p: &Proposal, now: i64) {
    let out = shield::check(cfg, state, Some(p), now);
    if out.verdict.verdict == "reject" {
        assert!(out.verdict.applied.is_none());
        return;
    }
    assert!(p.validate().is_ok(), "invalid proposal accepted: {p:?}");
    let applied = out
        .verdict
        .applied
        .expect("accepted proposal must have applied state");
    if let Some(rate) = &p.shaper_rate {
        let old = &state.rates[&rate.direction];
        let target = applied.rate_kbit.expect("missing rate");
        let lo = cfg.min_rate_kbit[&rate.direction];
        let hi = cfg.max_rate_kbit[&rate.direction];
        assert!((lo..=hi).contains(&target));
        assert!(rate.previous_rate_kbit.is_none_or(|v| v == old.kbit));
        if (lo..=hi).contains(&old.kbit) {
            assert!(target as f64 >= (old.kbit as f64 * (1.0 - cfg.max_decrease)).ceil());
            assert!(target as f64 <= (old.kbit as f64 * (1.0 + cfg.max_increase)).floor());
        }
        if target > old.kbit
            && let Some(at) = old.last_increase
        {
            assert!(
                i128::from(now) - i128::from(at)
                    >= i128::from(cfg.increase_interval_ms) * 1_000_000
            );
        }
    } else {
        let label = p.flow_label.as_ref().unwrap();
        let flow = &state.flows[&label.flow_id];
        assert_eq!(flow.host_id, label.host_id);
        assert!(!flow.pinned);
        assert_eq!(flow.tin, label.previous_tin);
        assert_eq!(applied.tin, Some(label.tin));
        assert_eq!(applied.dscp.as_deref(), Some(label.dscp.as_str()));
        if flow.tin != Some(label.tin) {
            assert!(!state.starvation || ![Tin::Voice, Tin::Video].contains(&label.tin));
            if flow.tin.is_some() {
                assert!(
                    flow.changes
                        .iter()
                        .filter(|&&at| at <= now
                            && i128::from(now) - i128::from(at)
                                < i128::from(cfg.flip_window_ms) * 1_000_000)
                        .count()
                        < cfg.flip_limit
                );
                assert!(state.remarks.iter().filter(|&&at|at<=now&&i128::from(now)-i128::from(at)<1_000_000_000).count()<cfg.remark_budget);
            }
        }
    }
}
#[test]
#[ignore = "time-bounded stress: make fuzz FUZZTIME=150s"]
fn randomized_proposals_json_and_packet_headers() {
    let seconds = std::env::var("AQOS_FUZZ_SECONDS")
        .unwrap_or_else(|_| "150".into())
        .parse::<u64>()
        .expect("seconds");
    assert!((1..=86400).contains(&seconds));
    let seed = std::env::var("AQOS_FUZZ_SEED")
        .unwrap_or_else(|_| "20261022".into())
        .parse()
        .expect("seed");
    assert_ne!(seed, 0);
    let mut rng = Rng(seed);
    let cfg = Config::load(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../configs/shield.yaml"
    ))
    .unwrap();
    let label: Proposal = serde_json::from_str(include_str!(
        "../../../contracts/examples/policy_proposal.flow_label.json"
    ))
    .unwrap();
    let rate: Proposal = serde_json::from_str(include_str!(
        "../../../contracts/examples/policy_proposal.shaper_rate.json"
    ))
    .unwrap();
    let seeds = [
        serde_json::to_vec(&label).unwrap(),
        serde_json::to_vec(&rate).unwrap(),
    ];
    let now = 120_000_000_000_i64;
    let start = Instant::now();
    let mut cases = 0_u64;
    while start.elapsed() < Duration::from_secs(seconds) {
        let mut st = State {
            starvation: rng.next().is_multiple_of(4),
            ..Default::default()
        };
        let mut p = if rng.next().is_multiple_of(2) {
            label.clone()
        } else {
            rate.clone()
        };
        if let Some(l) = &mut p.flow_label {
            l.tin = [Tin::Voice, Tin::Video, Tin::BestEffort, Tin::Bulk][rng.next() as usize % 4];
            l.dscp = default_dscp(l.tin).into();
            let old = [
                None,
                Some(Tin::Voice),
                Some(Tin::Video),
                Some(Tin::BestEffort),
                Some(Tin::Bulk),
            ][rng.next() as usize % 5];
            l.previous_tin = if rng.next().is_multiple_of(4) {
                None
            } else {
                old
            };
            st.flows.insert(
                l.flow_id.clone(),
                Flow {
                    host_id: l.host_id.clone(),
                    tin: old,
                    pinned: rng.next().is_multiple_of(5),
                    changes: vec![now - 1; rng.next() as usize % 5],
                    ..Default::default()
                },
            );
            st.remarks = vec![now - 1; rng.next() as usize % 70];
        } else if let Some(r) = &mut p.shaper_rate {
            let current = rng.number();
            r.rate_kbit = rng.number();
            r.previous_rate_kbit = if rng.next().is_multiple_of(4) {
                Some(rng.number())
            } else {
                Some(current)
            };
            let at = [None, Some(now - 1), Some(0), Some(i64::MAX), Some(i64::MIN)]
                [rng.next() as usize % 5];
            st.rates.insert(
                r.direction.clone(),
                Rate {
                    kbit: current,
                    last_increase: at,
                },
            );
        }
        oracle(&cfg, &st, &p, now);
        let mut bytes = seeds[rng.next() as usize % 2].clone();
        for _ in 0..1 + rng.next() % 8 {
            let i = rng.next() as usize % bytes.len();
            bytes[i] = rng.next() as u8;
        }
        if let Ok(p) = serde_json::from_slice::<Proposal>(&bytes) {
            oracle(&cfg, &st, &p, now);
        }
        let mut header = [0; 256];
        for byte in &mut header {
            *byte = rng.next() as u8;
        }
        let _ = parse_packet(&header[..rng.next() as usize % 257], rng.number());
        cases += 1;
    }
    println!(
        "seed={seed} cases={cases} elapsed_s={:.3}",
        start.elapsed().as_secs_f64()
    );
}
