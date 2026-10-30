use aqos_classify_rs::{Class, Engine, FeatureRecord, Kind, Mode, Model, Probabilities, Tin};
use serde::Deserialize;
use serde_json::{Value, json};
use std::{collections::HashMap, fs::File, path::PathBuf};

fn root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}
#[derive(Deserialize)]
struct Fixture {
    model_version: String,
    cases: Vec<Case>,
}
#[derive(Deserialize)]
struct Case {
    record: FeatureRecord,
    probs: [f64; 7],
    tins: HashMap<String, Tin>,
}
fn fixture() -> Fixture {
    serde_json::from_reader(flate2::read::GzDecoder::new(
        File::open(root().join("internal/classify/testdata/fixture.json.gz")).unwrap(),
    ))
    .unwrap()
}

#[test]
fn all_reference_probabilities_and_decisions_match() {
    let fx = fixture();
    let model =
        Model::load(root().join(format!("models/{}/trees.json", fx.model_version))).unwrap();
    assert_eq!(fx.cases.len(), 2000);
    for mode in [Mode::ExpectedDamage, Mode::TopLabel] {
        let engine = Engine::load(root().join("configs/damage_table.yaml"), mode, true).unwrap();
        for (i, c) in fx.cases.iter().enumerate() {
            let record = c.record.validate().unwrap();
            let probs = model.probabilities(&record.vector());
            for (actual, expected) in probs.values().iter().zip(c.probs) {
                assert!(
                    (actual - expected).abs() < 1e-9,
                    "case {i}: {actual} != {expected}"
                );
            }
            let expected = c.tins[mode.name()];
            assert_eq!(
                engine.choose(&probs, &[], Some(&record)).tin,
                expected,
                "case {i}"
            );
            // Separately check the rule with the reference probabilities.
            assert_eq!(
                engine
                    .choose(&Probabilities::new(c.probs).unwrap(), &[], Some(&record))
                    .tin,
                expected
            );
        }
    }
}

fn tiny_model() -> Value {
    json!({"model_version":"test", "features_version":1, "temperature":1.0,
        "model_classes": ["telephony","realtime_interactive","multimedia_conferencing","multimedia_streaming","standard","high_throughput_data","low_priority_data"],
        "trees": (0..7).map(|c| json!({"class":c,"root":-1,"nodes":[],"leaves":[0.0]})).collect::<Vec<_>>()})
}
fn load(v: &Value) -> aqos_classify_rs::Result<Model> {
    Model::from_json(&serde_json::to_vec(v).unwrap())
}

#[test]
fn rejects_malformed_models_before_inference() {
    let good = tiny_model();
    assert!(load(&good).is_ok());
    for (key, value) in [
        ("temperature", json!(0)),
        ("temperature", json!(-1)),
        ("features_version", json!(2)),
        ("trees", json!([])),
    ] {
        let mut bad = good.clone();
        bad[key] = value;
        assert!(load(&bad).is_err(), "{key}");
    }
    let mut bad = good.clone();
    bad["model_classes"][1] = json!("telephony");
    assert!(load(&bad).is_err());
    let mut bad = good.clone();
    bad["model_classes"] = json!(["telephony"]);
    assert!(load(&bad).is_err());
    let mut bad = good.clone();
    bad["trees"][0] = Value::Null;
    assert!(load(&bad).is_err());
    let mut bad = good.clone();
    bad["trees"][0]["class"] = json!(7);
    assert!(load(&bad).is_err());
    let mut bad = good.clone();
    bad["trees"][0]["class"] = json!(1);
    assert!(load(&bad).is_err());
    for root in [0_i64, -2, i64::MIN, i64::MAX] {
        let mut bad = good.clone();
        bad["trees"][0]["root"] = json!(root);
        assert!(load(&bad).is_err());
    }
    let mut bad = good.clone();
    bad["temperature"] = json!(1e308);
    bad["trees"][0]["leaves"] = json!([1e308]);
    assert!(load(&bad).is_err());
    for node in [
        json!([28, 0.0, true, true, -1, -1]),
        json!([0.5, 0.0, true, true, -1, -1]),
        json!([0, 0.0, true, true, 0, -1]),
        json!([0, 0.0, true, true, 2, -1]),
        json!([0, 0.0, true, true, -2, -1]),
    ] {
        let mut bad = good.clone();
        bad["trees"][0]["nodes"] = json!([node]);
        bad["trees"][0]["root"] = json!(0);
        assert!(load(&bad).is_err());
    }
    // Even an unreachable cycle is rejected.
    let mut bad = good;
    bad["trees"][0]["nodes"] = json!([[0, 0.0, true, true, 0, -1]]);
    assert!(load(&bad).is_err());
}

#[test]
fn nan_routing_and_leaf_roots_match_lightgbm_semantics() {
    let mut doc = tiny_model();
    doc["trees"][0] =
        json!({"class":0,"root":0,"nodes":[[0,-0.5,true,true,-1,-2]],"leaves":[2.0,-2.0]});
    let x = [f64::NAN; 28];
    assert!(load(&doc).unwrap().probabilities(&x).values()[0] > 0.5);
    doc["trees"][0]["nodes"][0][3] = json!(false); // missing counts as zero
    assert!(load(&doc).unwrap().probabilities(&x).values()[0] < 0.1);
    doc["trees"][0]["nodes"][0][3] = json!(true);
    doc["trees"][0]["nodes"][0][2] = json!(false); // default right
    assert!(load(&doc).unwrap().probabilities(&x).values()[0] < 0.1);
}

#[test]
fn split_boundaries_missing_values_and_nonzero_roots() {
    // Exercise both child tags, equality, signed zero, both missing policies,
    // and a root that is not node zero. Distinct leaf scores expose misrouting.
    for feature in 0..28 {
        for threshold in [-2.0_f64, -0.0, 0.0, 3.5] {
            for default_left in [false, true] {
                for nan_missing in [false, true] {
                    let mut doc = tiny_model();
                    doc["trees"][0] = json!({"class":0,"root":1,
                        "nodes":[[feature,threshold,default_left,nan_missing,-2,-3],
                                 [feature,threshold,default_left,nan_missing,0,-1]],
                        "leaves":[-2.0,2.0,0.0]});
                    let model = load(&doc).unwrap();
                    for value in [
                        f64::NAN,
                        f64::NEG_INFINITY,
                        f64::INFINITY,
                        -0.0,
                        0.0,
                        threshold,
                        threshold - 1.0,
                        threshold + 1.0,
                    ] {
                        let mut x = [0.0; 28];
                        x[feature] = value;
                        let left = if value.is_nan() {
                            if nan_missing {
                                default_left
                            } else {
                                0.0 <= threshold
                            }
                        } else {
                            value <= threshold
                        };
                        let score = if left { 2.0_f64 } else { -2.0_f64 };
                        let expected = score.exp() / (score.exp() + 6.0);
                        assert!((model.probabilities(&x).values()[0] - expected).abs() < 1e-14);
                    }
                }
            }
        }
    }
}

#[test]
fn probability_and_record_validation_and_bulk_guards() {
    for p in [
        [0.0; 7],
        [1.0; 7],
        [f64::NAN; 7],
        [f64::INFINITY; 7],
        [-0.1, 1.1, 0.0, 0.0, 0.0, 0.0, 0.0],
    ] {
        assert!(Probabilities::new(p).is_err());
    }
    let fx = fixture();
    let mut rec = fx
        .cases
        .iter()
        .find(|c| c.record.kind == Kind::Window)
        .unwrap()
        .record
        .clone();
    let engine = Engine::load(
        root().join("configs/damage_table.yaml"),
        Mode::ExpectedDamage,
        true,
    )
    .unwrap();
    let p = Probabilities::new([0.0, 0.0, 0.0, 0.0, 0.03, 0.0, 0.97]).unwrap();
    rec.protocol = 6;
    assert_eq!(
        engine.choose(&p, &[], Some(&rec.validate().unwrap())).tin,
        Tin::Bulk
    );
    rec.protocol = 17;
    assert!(
        engine
            .choose(&p, &[], Some(&rec.validate().unwrap()))
            .guarded
    );
    rec.protocol = 6;
    rec.kind = Kind::Initial;
    assert!(
        engine
            .choose(&p, &[], Some(&rec.validate().unwrap()))
            .guarded
    );
    // Duplicate leases must not multiply the cost more than once.
    assert_eq!(
        engine.choose(&p, &[Class::Telephony], None).expected,
        engine
            .choose(&p, &[Class::Telephony, Class::Telephony], None)
            .expected
    );
    rec.kind = Kind::Window;
    rec.window = None;
    assert!(rec.validate().is_err());
    rec.kind = Kind::Initial;
    rec.pkt_sizes[0] = 0;
    assert!(rec.validate().is_err());
}
