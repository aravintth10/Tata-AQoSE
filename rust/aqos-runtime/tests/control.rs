use aqos_runtime_rs::{
    Class, Tin, Window,
    capacity::{Config as CapacityConfig, Estimator},
    extract::Hasher,
    intent::Store,
    reclass, verify,
};
use std::collections::HashSet;
fn estimator() -> Estimator {
    Estimator::new(CapacityConfig {
        base: [19000, 95000],
        minimum: [500, 1000],
        reflectors: 3,
        deaf_hold: true,
        raise_every_ns: 10_000_000_000,
    })
    .unwrap()
}
#[test]
fn quiet_and_unreachable_reflectors_do_not_lower_rates() {
    let mut e = estimator();
    for tick in 0..100 {
        let now = 1_000_000_000_000 + tick * 100_000_000;
        e.load(0, 19000, 18000.0, now);
        e.load(1, 95000, 90000.0, now);
        for i in 0..3 {
            e.probe(i, 20.0, true, 0.0, 0.0);
        }
        assert!(e.decide(now).is_empty());
    }
    for i in 0..3 {
        e.probe(i, 0.0, false, 3000.0, 3000.0);
    }
    assert!(e.decide(1_020_000_000_000).is_empty());
    assert!(e.deaf);
    assert!(!e.delay().1);
}
#[test]
fn one_answer_on_time_prevents_two_silent_targets_looking_like_congestion() {
    let mut e = estimator();
    for i in 0..3 {
        e.probe(i, 20.0, true, 0.0, 0.0);
    }
    for _ in 0..10 {
        e.probe(0, 20.0, true, 0.0, 0.0);
        e.probe(1, 0.0, false, 100.0, 100.0);
        e.probe(2, 0.0, false, 100.0, 100.0);
        assert!(!e.congested().0);
    }
}
#[test]
fn download_congestion_is_measured_before_upload_is_cut() {
    let mut e = estimator();
    let start = 1_000_000_000_000;
    for i in 0..3 {
        e.probe(i, 20.0, true, 0.0, 0.0);
    }
    e.load(0, 19000, 18000.0, start);
    e.load(1, 95000, 50000.0, start);
    for tick in 1..=3 {
        for i in 0..3 {
            e.probe(i, 100.0, true, 0.0, 0.0);
        }
        let p = e.decide(start + tick * 100_000_000);
        if tick < 3 {
            assert!(p.is_empty());
        } else {
            assert_eq!(p.len(), 1);
            assert_eq!(p[0].direction, 1);
            assert_eq!(p[0].rate, 45000);
        }
    }
}
#[test]
fn simulated_capacity_drop_drains_and_recovers() {
    let mut e = estimator();
    let mut rate = 95000_i64;
    let mut queue = 0.0_f64;
    let mut last_raise = 0_i64;
    let mut low = 95000;
    for tick in 0..1400 {
        let now = 1_000_000_000_000 + tick * 100_000_000;
        let capacity = if (50..200).contains(&tick) {
            20000.0
        } else {
            100000.0
        };
        let sent = rate as f64;
        let passed = (sent + queue / 0.1).min(capacity);
        queue = (queue + (sent - passed) * 0.1).clamp(0.0, capacity * 0.5);
        let rtt = 20.0 + queue / capacity * 1000.0;
        for i in 0..3 {
            e.probe(i, rtt, true, (rtt - 100.0).max(0.0), 0.0);
        }
        if tick % 2 == 0 {
            e.load(1, rate, passed.min(rate as f64), now);
            e.load(0, 19000, 500.0, now);
        }
        for p in e.decide(now) {
            assert_eq!(p.direction, 1);
            if p.rate < rate {
                rate = p.rate.max(rate / 2);
            } else if now - last_raise >= 10_000_000_000 {
                rate = p.rate.min(rate * 11 / 10);
                last_raise = now;
            }
            e.applied(1, rate);
        }
        if tick == 70 {
            assert!(rate <= 20000, "failed to react within 2 s: {rate}");
        }
        if tick == 199 {
            assert!(
                (15000..=20000).contains(&rate),
                "chased a draining queue too low: {rate}"
            );
            assert!(queue < 100.0);
            low = rate;
        }
    }
    assert!(rate > 2 * low && rate <= 95000);
}
#[test]
fn all_ml_reclassifier_sequences_match() {
    let data: serde_json::Value = serde_json::from_str(include_str!(
        "../../../internal/reclass/testdata/cases.json"
    ))
    .unwrap();
    let env: reclass::Envelopes = serde_json::from_value(data["envelopes"].clone()).unwrap();
    for case in data["cases"].as_array().unwrap() {
        let settings = &case["settings"];
        let n = |name| settings[name].as_u64().unwrap() as u32;
        let cfg = reclass::Config {
            k: n("k"),
            windows: n("windows"),
            interactive: n("interactive"),
            slow_every: n("slow_every"),
            recheck_min: 0,
            recheck_max: 0,
            backoff: n("backoff"),
            backoff_max: n("backoff_max"),
        };
        let class = serde_json::from_value::<Class>(case["class"].clone());
        let mut ranges = env.clone();
        if class.is_err() {
            ranges.remove(&Class::Standard);
        }
        let mut watcher = reclass::Watcher::new(cfg, ranges, 7).unwrap();
        watcher.answered("flow", class.unwrap_or(Class::Standard));
        let tin: Tin = serde_json::from_value(case["tin"].clone()).unwrap();
        for (i, w) in case["windows"].as_array().unwrap().iter().enumerate() {
            let w: Window = serde_json::from_value(w.clone()).unwrap();
            let ask = watcher.window("flow", tin, &w);
            let expected = &case["expect"][i];
            assert_eq!(
                ask.is_some(),
                !expected.is_null(),
                "{} at window {}",
                case["name"],
                i
            );
            if let Some(ask) = ask {
                assert!(!ask.recheck);
                assert!(!ask.reason.is_empty());
            }
        }
    }
}
#[test]
fn verifier_requires_two_consistent_windows_and_counts_missing_data() {
    let cfg = verify::Config::default();
    let mut samples = Vec::new();
    for i in 0..20 {
        samples.push(verify::Sample {
            ts: i as f64,
            tin: Some(if i < 10 { 10.0 } else { 30.0 }),
            probe: Some(if i < 10 { 20.0 } else { 5.0 }),
        });
    }
    assert_eq!(verify::judge(cfg, &samples, 10.0).verdict, "regressed");
    for s in &mut samples[15..] {
        s.tin = Some(10.0);
    }
    assert_eq!(verify::judge(cfg, &samples, 10.0).verdict, "improved");
    for s in &mut samples {
        s.tin = None;
        s.probe = None;
    }
    assert_eq!(
        verify::judge(cfg, &samples, 10.0).reason,
        "too few samples to judge"
    );
}
#[test]
fn leases_validate_known_hosts_limit_duplicates_and_expiry() {
    let h = Hasher::new(b"test");
    let mut known = HashSet::new();
    for ip in ["10.0.0.1", "10.0.0.2", "10.0.0.3"] {
        known.insert(h.host_id(ip.parse().unwrap()));
    }
    let mut store = Store::new("", h.clone()).unwrap();
    assert_eq!(
        store
            .create("10.0.0.4".parse().unwrap(), 10.0, &known)
            .unwrap_err()
            .0,
        404
    );
    assert_eq!(
        store
            .create("10.0.0.1".parse().unwrap(), 121.0, &known)
            .unwrap_err()
            .0,
        400
    );
    let first = store
        .create("10.0.0.1".parse().unwrap(), 10.0, &known)
        .unwrap();
    assert!(store.leased(&h.host_id("10.0.0.1".parse().unwrap())));
    assert_eq!(
        store
            .create("10.0.0.1".parse().unwrap(), 10.0, &known)
            .unwrap_err()
            .0,
        409
    );
    store
        .create("10.0.0.2".parse().unwrap(), 10.0, &known)
        .unwrap();
    assert_eq!(
        store
            .create("10.0.0.3".parse().unwrap(), 10.0, &known)
            .unwrap_err()
            .0,
        409
    );
    store.delete(&first.id).unwrap();
    store
        .create("10.0.0.3".parse().unwrap(), 0.00001, &known)
        .unwrap();
    std::thread::sleep(std::time::Duration::from_millis(3));
    assert_eq!(store.list().unwrap().len(), 1);
}
