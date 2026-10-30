use aqos_runtime_rs::{
    Tin,
    contracts::Proposal,
    extract::{Counters, Packet, Table, header_len, parse_packet},
    shield::{self, Config, Rate, State, Tracker},
};
fn cfg() -> Config {
    Config::load(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../configs/shield.yaml"
    ))
    .unwrap()
}
fn label() -> Proposal {
    serde_json::from_str(include_str!(
        "../../../contracts/examples/policy_proposal.flow_label.json"
    ))
    .unwrap()
}
fn rate() -> Proposal {
    serde_json::from_str(include_str!(
        "../../../contracts/examples/policy_proposal.shaper_rate.json"
    ))
    .unwrap()
}
#[test]
fn pinning_preserves_confirmed_state_until_clear_succeeds() {
    let mut tr = Tracker::new(cfg()).unwrap();
    let mut p = label();
    let l = p.flow_label.as_ref().unwrap();
    let id = l.flow_id.clone();
    tr.seen(&id, &l.host_id);
    for (i, tin) in [Tin::Video, Tin::Voice, Tin::Video].into_iter().enumerate() {
        let l = p.flow_label.as_mut().unwrap();
        l.previous_tin = tr.state.flows[&id].tin;
        l.tin = tin;
        l.dscp = aqos_runtime_rs::contracts::default_dscp(tin).into();
        let o = tr.decide(&p, i as i64 * 1_000_000_000);
        assert_eq!(o.code, "ok");
        tr.applied(&p, &o.verdict, i as i64 * 1_000_000_000);
    }
    let l = p.flow_label.as_mut().unwrap();
    l.previous_tin = Some(Tin::Video);
    l.tin = Tin::Bulk;
    l.dscp = "CS1".into();
    let out = tr.decide(&p, 3_000_000_000);
    assert!(out.unlabel);
    assert!(tr.state.flows[&id].pinned);
    assert!(tr.state.flows[&id].pending_clear);
    assert_eq!(tr.state.flows[&id].tin, Some(Tin::Video));
    assert_eq!(tr.decide(&p, 4_000_000_000).code, "pinned");
    tr.cleared(&id);
    assert_eq!(tr.state.flows[&id].tin, None);
    assert!(!tr.state.flows[&id].pending_clear);
    assert!(tr.state.flows[&id].pinned);
}
#[test]
fn stale_unknown_starved_and_invalid_proposals_fail_closed() {
    let c = cfg();
    let mut s = State::default();
    let mut p = label();
    assert_eq!(shield::check(&c, &s, Some(&p), 1).code, "unknown_flow");
    let l = p.flow_label.as_ref().unwrap();
    let id = l.flow_id.clone();
    s.flows.insert(
        id.clone(),
        shield::Flow {
            host_id: l.host_id.clone(),
            ..Default::default()
        },
    );
    assert_eq!(shield::check(&c, &s, Some(&p), 1).code, "ok");
    s.starvation = true;
    assert_eq!(shield::check(&c, &s, Some(&p), 1).code, "starvation");
    s.starvation = false;
    s.flows.get_mut(&id).unwrap().tin = Some(Tin::Voice);
    assert_eq!(shield::check(&c, &s, Some(&p), 1).code, "stale");
    p.flow_label.as_mut().unwrap().expected_damage = Some(f64::NAN);
    assert_eq!(shield::check(&c, &s, Some(&p), 1).code, "invalid");
    p.flow_label.as_mut().unwrap().expected_damage = None;
    p.shaper_rate = rate().shaper_rate;
    assert_eq!(shield::check(&c, &s, Some(&p), 1).code, "invalid");
}
#[test]
fn rates_never_escape_absolute_and_step_bounds() {
    let cfg = cfg();
    let mut p = rate();
    let mut st = State::default();
    for current in [
        1,
        499,
        500,
        999,
        1000,
        3000,
        10000,
        199999,
        200000,
        1000000,
        i64::MAX,
    ] {
        for wanted in [
            0,
            1,
            500,
            700,
            5000,
            20000,
            100000,
            200000,
            999999,
            i64::MAX,
        ] {
            for dir in ["up", "down"] {
                st.rates.insert(
                    dir.into(),
                    Rate {
                        kbit: current,
                        last_increase: None,
                    },
                );
                let r = p.shaper_rate.as_mut().unwrap();
                r.direction = dir.into();
                r.rate_kbit = wanted;
                r.previous_rate_kbit = Some(current);
                let o = shield::check(&cfg, &st, Some(&p), 100_000_000_000);
                if let Some(a) = o.verdict.applied {
                    let n = a.rate_kbit.unwrap();
                    assert!(n >= cfg.min_rate_kbit[dir] && n <= cfg.max_rate_kbit[dir]);
                    if n > current {
                        assert!(
                            n <= ((current as f64) * 1.1).floor() as i64
                                || n == cfg.min_rate_kbit[dir]
                        );
                    }
                    if n < current {
                        assert!(
                            n >= ((current as f64) * 0.5).ceil() as i64
                                || n == cfg.max_rate_kbit[dir]
                        );
                    }
                }
            }
        }
    }
}
#[test]
fn increases_wait_but_decreases_do_not() {
    let cfg = cfg();
    let mut st = State::default();
    st.rates.insert(
        "down".into(),
        Rate {
            kbit: 50000,
            last_increase: Some(0),
        },
    );
    let mut p = rate();
    let r = p.shaper_rate.as_mut().unwrap();
    r.direction = "down".into();
    r.previous_rate_kbit = None;
    r.rate_kbit = 52000;
    assert_eq!(
        shield::check(&cfg, &st, Some(&p), 9_999_999_999).code,
        "rate_interval"
    );
    assert_eq!(
        shield::check(&cfg, &st, Some(&p), 10_000_000_000).code,
        "ok"
    );
    p.shaper_rate.as_mut().unwrap().rate_kbit = 40000;
    assert_eq!(shield::check(&cfg, &st, Some(&p), 1).code, "ok");
}
#[test]
fn required_null_fields_cannot_be_omitted() {
    let original = serde_json::to_value(label()).unwrap();
    for field in ["previous_tin", "top_class", "expected_damage"] {
        let mut v = original.clone();
        v["flow_label"].as_object_mut().unwrap().remove(field);
        assert!(serde_json::from_value::<Proposal>(v).is_err());
    }
    let mut v = original;
    v["extra"] = true.into();
    assert!(serde_json::from_value::<Proposal>(v).is_err());
}
fn packet() -> Packet {
    Packet {
        ts_ns: 0,
        protocol: 17,
        src: "10.0.0.1:30000".parse().unwrap(),
        dst: "203.0.113.1:443".parse().unwrap(),
        l3_len: 100,
        tcp_flags: 0,
    }
}
#[test]
fn overflow_never_emits_a_partial_flow() {
    let mut t = Table::new(vec![], b"key", 4);
    for i in 0..4 {
        let mut p = packet();
        p.ts_ns = i * 1000;
        t.packet(p);
    }
    t.overflow();
    for i in 4..20 {
        let mut p = packet();
        p.ts_ns = i * 1000;
        assert!(t.packet(p).unwrap().1.is_none());
    }
}
#[test]
fn byte_counter_reset_discards_window_instead_of_wrapping() {
    let mut t = Table::new(vec![], b"key", 4);
    let (key, _, _) = t.packet(packet()).unwrap();
    let f = t.flows.get_mut(&key).unwrap();
    for (pkts, bytes) in [(10, 1000), (20, 2000)] {
        f.counters(Counters {
            pkts_up: pkts,
            bytes_up: bytes,
            ..Default::default()
        });
    }
    assert!(f.window().is_some());
    f.counters(Counters {
        pkts_up: 21,
        bytes_up: 0,
        ..Default::default()
    });
    assert!(f.window().is_none());
    f.counters(Counters {
        pkts_up: 22,
        bytes_up: 100,
        ..Default::default()
    });
    assert_eq!(f.window().unwrap().bytes_up_per_s, 100.0);
}
#[test]
fn parser_never_indexes_outside_malformed_headers() {
    let mut state = 7_u64;
    for size in 0..128 {
        for _ in 0..100 {
            let mut b = vec![0; size];
            for v in &mut b {
                state = state.wrapping_mul(6364136223846793005).wrapping_add(1);
                *v = (state >> 32) as u8;
            }
            let _ = parse_packet(&b, 0);
            assert!(header_len(&b) <= size);
        }
    }
}
