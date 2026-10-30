use crate::{Class, Result, Tin, is_id};
use serde::{Deserialize, Serialize};
use std::{collections::HashMap, sync::OnceLock};
fn required_optional<'de, D: serde::Deserializer<'de>, T: Deserialize<'de>>(
    d: D,
) -> std::result::Result<Option<T>, D::Error> {
    Option::deserialize(d)
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FlowLabel {
    pub flow_id: String,
    pub host_id: String,
    pub tin: Tin,
    pub dscp: String,
    #[serde(deserialize_with = "required_optional")]
    pub previous_tin: Option<Tin>,
    #[serde(deserialize_with = "required_optional")]
    pub top_class: Option<Class>,
    #[serde(deserialize_with = "required_optional")]
    pub expected_damage: Option<f64>,
    pub decision_rule: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ShaperRate {
    pub direction: String,
    pub rate_kbit: i64,
    #[serde(deserialize_with = "required_optional")]
    pub previous_rate_kbit: Option<i64>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Proposal {
    pub schema_version: u32,
    pub proposal_id: String,
    pub ts_ns: i64,
    pub source: String,
    pub kind: String,
    pub reason: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub flow_label: Option<FlowLabel>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub shaper_rate: Option<ShaperRate>,
}
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Applied {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tin: Option<Tin>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub dscp: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub rate_kbit: Option<i64>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Verdict {
    pub schema_version: u32,
    pub proposal_id: String,
    pub ts_ns: i64,
    pub verdict: String,
    pub reason: String,
    pub applied: Option<Applied>,
}
#[derive(Deserialize)]
struct Dscp {
    tin: Tin,
}
#[derive(Deserialize)]
struct Taxonomy {
    dscp: HashMap<String, Dscp>,
    tins: HashMap<Tin, TinDscp>,
}
#[derive(Deserialize)]
struct TinDscp {
    dscp: String,
}
fn taxonomy() -> &'static Taxonomy {
    static TAXONOMY: OnceLock<Taxonomy> = OnceLock::new();
    TAXONOMY.get_or_init(|| {
        serde_json::from_str(include_str!("../../../contracts/taxonomy.json"))
            .expect("embedded taxonomy is valid")
    })
}
pub fn default_dscp(tin: Tin) -> &'static str {
    &taxonomy().tins[&tin].dscp
}
impl Proposal {
    pub fn validate(&self) -> Result<()> {
        if self.schema_version != 1
            || !is_id(&self.proposal_id)
            || self.ts_ns < 0
            || self.reason.is_empty()
        {
            return Err("invalid proposal metadata".into());
        }
        if ![
            "decision_engine",
            "reclassifier",
            "capacity_estimator",
            "intent_service",
            "verifier",
            "user_override",
        ]
        .contains(&self.source.as_str())
        {
            return Err("unknown proposal source".into());
        }
        match (&self.flow_label, &self.shaper_rate, self.kind.as_str()) {
            (Some(l), None, "flow_label") => {
                if !is_id(&l.flow_id) || !is_id(&l.host_id) {
                    return Err("bad flow or host id".into());
                }
                if !taxonomy().dscp.get(&l.dscp).is_some_and(|d| d.tin == l.tin) {
                    return Err("DSCP does not belong to tin".into());
                }
                if ![
                    "top_label",
                    "expected_damage",
                    "rules",
                    "override",
                    "fallback",
                ]
                .contains(&l.decision_rule.as_str())
                {
                    return Err("unknown decision rule".into());
                }
                if l.expected_damage.is_some_and(|v| !v.is_finite() || v < 0.0) {
                    return Err("invalid expected damage".into());
                }
            }
            (None, Some(r), "shaper_rate") => {
                if !["up", "down"].contains(&r.direction.as_str())
                    || r.rate_kbit < 1
                    || r.previous_rate_kbit.is_some_and(|v| v < 1)
                {
                    return Err("invalid shaper rate".into());
                }
            }
            _ => return Err("proposal must contain exactly its declared operation".into()),
        }
        Ok(())
    }
}
