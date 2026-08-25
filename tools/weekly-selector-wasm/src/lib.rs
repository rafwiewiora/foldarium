use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet, HashSet};
use thiserror::Error;
use wasm_bindgen::prelude::*;

pub const KIT_SCHEMA_VERSION: &str = "foldarium.weekly-selector-kit/v2";
pub const SUBMISSION_SCHEMA_VERSION: &str = "foldarium.selector-submission/v2";
pub const MAX_SUBMISSION_PAYLOAD_BYTES: usize = 65_536;
const ENVIRONMENTS: &[&str] = &["production", "preview", "development"];

const FORBIDDEN_KEYS: &[&str] = &[
    "accepted_correct",
    "answer",
    "answer_metadata",
    "artifact_sha256",
    "correct",
    "coordinates",
    "crystal",
    "private_index",
    "reference",
    "reference_uri",
    "reveal_manifest",
    "rmsd",
    "run_id",
    "sample_id",
    "score",
];

const SUBMISSION_TOP_KEYS: &[&str] = &[
    "schema_version",
    "submission_id",
    "environment",
    "round_id",
    "blind_manifest_sha256",
    "kit_sha256",
    "items",
];

const SUBMISSION_ITEM_KEYS: &[&str] = &["item_id", "clustered", "unclustered"];

#[derive(Debug, Error, PartialEq)]
pub enum ContractError {
    #[error("{0}")]
    Message(String),
}

#[derive(Debug, Clone, Deserialize)]
struct KitChoice {
    choice_id: String,
    cluster_id: String,
}

#[derive(Debug, Clone, Deserialize)]
struct KitItem {
    item_id: String,
    choices: Vec<KitChoice>,
}

#[derive(Debug, Clone, Deserialize)]
struct PublicKitManifest {
    schema_version: String,
    environment: String,
    round_id: String,
    blind_manifest_sha256: String,
    kit_sha256: String,
    items: Vec<KitItem>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(tag = "selection_kind", deny_unknown_fields)]
pub enum ClusteredDecision {
    #[serde(rename = "cluster")]
    Cluster { cluster_id: String },
    #[serde(rename = "none")]
    None,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(tag = "selection_kind", deny_unknown_fields)]
pub enum UnclusteredDecision {
    #[serde(rename = "exact")]
    Exact { choice_id: String },
    #[serde(rename = "none")]
    None,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct SubmissionItem {
    pub item_id: String,
    pub clustered: ClusteredDecision,
    pub unclustered: UnclusteredDecision,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Submission {
    pub schema_version: String,
    pub submission_id: String,
    pub environment: String,
    pub round_id: String,
    pub blind_manifest_sha256: String,
    pub kit_sha256: String,
    pub items: Vec<SubmissionItem>,
}

fn contract_error(message: impl Into<String>) -> ContractError {
    ContractError::Message(message.into())
}

fn is_safe_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value
            .chars()
            .next()
            .map(|ch| ch.is_ascii_alphanumeric())
            .unwrap_or(false)
        && value
            .chars()
            .all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '.' | '_' | ':' | '-'))
}

fn is_sha256(value: &str) -> bool {
    value.len() == 64 && value.chars().all(|ch| matches!(ch, '0'..='9' | 'a'..='f'))
}

fn assert_exact_keys(value: &Map<String, Value>, allowed: &[&str], label: &str) -> Result<(), ContractError> {
    for key in value.keys() {
        if !allowed.contains(&key.as_str()) {
            return Err(contract_error(format!("{label} contains unknown key: {key}")));
        }
    }
    Ok(())
}

fn assert_decision_shape(
    value: &Value,
    label: &str,
    selected_kind: &str,
    identity_key: &str,
) -> Result<(), ContractError> {
    let map = value
        .as_object()
        .ok_or_else(|| contract_error(format!("{label} must be an object")))?;
    match map.get("selection_kind").and_then(Value::as_str) {
        Some("none") => assert_exact_keys(map, &["selection_kind"], label),
        Some(kind) if kind == selected_kind => {
            assert_exact_keys(map, &["selection_kind", identity_key], label)?;
            if !map.contains_key(identity_key) {
                return Err(contract_error(format!(
                    "{label} must include {identity_key}"
                )));
            }
            Ok(())
        }
        _ => Err(contract_error(format!(
            "{label} must be an exact {selected_kind}/none decision"
        ))),
    }
}

fn reject_forbidden_keys(value: &Value, path: &str) -> Result<(), ContractError> {
    match value {
        Value::Object(map) => {
            for key in map.keys() {
                if FORBIDDEN_KEYS.contains(&key.as_str()) {
                    return Err(contract_error(format!("{path} contains forbidden key: {key}")));
                }
            }
            for (key, nested) in map {
                reject_forbidden_keys(nested, &format!("{path}.{key}"))?;
            }
        }
        Value::Array(items) => {
            for (index, nested) in items.iter().enumerate() {
                reject_forbidden_keys(nested, &format!("{path}[{index}]"))?;
            }
        }
        _ => {}
    }
    Ok(())
}

pub fn canonical_json(value: &Value) -> String {
    canonicalize(value).to_string()
}

fn canonicalize(value: &Value) -> Value {
    match value {
        Value::Object(map) => {
            let mut sorted = BTreeMap::new();
            for (key, nested) in map {
                sorted.insert(key.clone(), canonicalize(nested));
            }
            Value::Object(sorted.into_iter().collect())
        }
        Value::Array(items) => Value::Array(items.iter().map(canonicalize).collect()),
        _ => value.clone(),
    }
}

pub fn sha256_hex(value: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(value.as_bytes());
    format!("{:x}", hasher.finalize())
}

fn parse_public_kit_manifest(raw: &str) -> Result<PublicKitManifest, ContractError> {
    let raw_value: Value = serde_json::from_str(raw)
        .map_err(|error| contract_error(format!("manifest JSON is invalid: {error}")))?;
    reject_forbidden_keys(&raw_value, "manifest")?;
    let manifest: PublicKitManifest = serde_json::from_str(raw)
        .map_err(|error| contract_error(format!("manifest JSON is invalid: {error}")))?;
    if manifest.schema_version != KIT_SCHEMA_VERSION {
        return Err(contract_error(format!(
            "manifest.schema_version must be {KIT_SCHEMA_VERSION}"
        )));
    }
    if !is_safe_id(&manifest.round_id) {
        return Err(contract_error("manifest.round_id is invalid"));
    }
    if !ENVIRONMENTS.contains(&manifest.environment.as_str()) {
        return Err(contract_error("manifest.environment is invalid"));
    }
    if !is_sha256(&manifest.blind_manifest_sha256) {
        return Err(contract_error(
            "manifest.blind_manifest_sha256 must be a lowercase SHA-256 digest",
        ));
    }
    if !is_sha256(&manifest.kit_sha256) {
        return Err(contract_error(
            "manifest.kit_sha256 must be a lowercase SHA-256 digest",
        ));
    }
    if manifest.items.is_empty() {
        return Err(contract_error("manifest.items must be a non-empty list"));
    }
    kit_indexes(&manifest)?;
    Ok(manifest)
}

fn kit_indexes(
    manifest: &PublicKitManifest,
) -> Result<BTreeMap<String, (HashSet<String>, HashSet<String>)>, ContractError> {
    let mut indexes = BTreeMap::new();
    for item in &manifest.items {
        if !is_safe_id(&item.item_id) {
            return Err(contract_error("manifest item_id is invalid"));
        }
        if indexes.contains_key(&item.item_id) {
            return Err(contract_error(format!(
                "duplicate manifest item_id: {}",
                item.item_id
            )));
        }
        if item.choices.is_empty() {
            return Err(contract_error(format!(
                "manifest item {} has no choices",
                item.item_id
            )));
        }
        let mut choice_ids = HashSet::new();
        let mut cluster_ids = HashSet::new();
        for choice in &item.choices {
            if !is_safe_id(&choice.choice_id) || !is_safe_id(&choice.cluster_id) {
                return Err(contract_error(format!(
                    "manifest choice identity is invalid for item {}",
                    item.item_id
                )));
            }
            if !choice_ids.insert(choice.choice_id.clone()) {
                return Err(contract_error(format!(
                    "duplicate manifest choice_id in item {}: {}",
                    item.item_id, choice.choice_id
                )));
            }
            cluster_ids.insert(choice.cluster_id.clone());
        }
        indexes.insert(item.item_id.clone(), (choice_ids, cluster_ids));
    }
    Ok(indexes)
}

fn build_submission_template(manifest: &PublicKitManifest) -> Vec<SubmissionItem> {
    let mut items: Vec<SubmissionItem> = manifest
        .items
        .iter()
        .map(|item| SubmissionItem {
            item_id: item.item_id.clone(),
            clustered: ClusteredDecision::None,
            unclustered: UnclusteredDecision::None,
        })
        .collect();
    items.sort_by(|left, right| left.item_id.cmp(&right.item_id));
    items
}

pub fn validate_complete_submission(
    submission_raw: &str,
    manifest_raw: &str,
) -> Result<Submission, ContractError> {
    let manifest = parse_public_kit_manifest(manifest_raw)?;
    let value: Value = serde_json::from_str(submission_raw)
        .map_err(|error| contract_error(format!("submission JSON is invalid: {error}")))?;
    let map = value
        .as_object()
        .ok_or_else(|| contract_error("submission must be an object"))?;

    assert_exact_keys(map, SUBMISSION_TOP_KEYS, "submission")?;
    reject_forbidden_keys(&value, "submission")?;

    if map.get("schema_version").and_then(Value::as_str) != Some(SUBMISSION_SCHEMA_VERSION) {
        return Err(contract_error(format!(
            "submission.schema_version must be {SUBMISSION_SCHEMA_VERSION}"
        )));
    }

    let submission_id = map
        .get("submission_id")
        .and_then(Value::as_str)
        .ok_or_else(|| contract_error("submission_id must be a UUID"))?;
    let parsed_uuid = uuid::Uuid::parse_str(submission_id)
        .map_err(|_| contract_error("submission_id must be a UUID"))?;
    if submission_id != parsed_uuid.to_string() {
        return Err(contract_error(
            "submission_id must be a canonical lowercase UUID",
        ));
    }

    let environment = map
        .get("environment")
        .and_then(Value::as_str)
        .filter(|value| ENVIRONMENTS.contains(value))
        .ok_or_else(|| contract_error("submission environment is invalid"))?;
    if environment != manifest.environment {
        return Err(contract_error(
            "submission.environment does not match kit.environment",
        ));
    }

    let round_id = map
        .get("round_id")
        .and_then(Value::as_str)
        .filter(|value| is_safe_id(value))
        .ok_or_else(|| contract_error("submission round_id is invalid"))?;
    if round_id != manifest.round_id {
        return Err(contract_error(
            "submission.round_id does not match kit.round_id",
        ));
    }

    let blind_manifest_sha256 = map
        .get("blind_manifest_sha256")
        .and_then(Value::as_str)
        .filter(|value| is_sha256(value))
        .ok_or_else(|| {
            contract_error(
                "submission blind_manifest_sha256 must be a lowercase SHA-256 digest",
            )
        })?;
    if blind_manifest_sha256 != manifest.blind_manifest_sha256 {
        return Err(contract_error(
            "submission.blind_manifest_sha256 does not match kit.blind_manifest_sha256",
        ));
    }

    let kit_sha256 = map
        .get("kit_sha256")
        .and_then(Value::as_str)
        .filter(|value| is_sha256(value))
        .ok_or_else(|| {
            contract_error("submission kit_sha256 must be a lowercase SHA-256 digest")
        })?;
    if kit_sha256 != manifest.kit_sha256 {
        return Err(contract_error(
            "submission.kit_sha256 does not match kit.kit_sha256",
        ));
    }

    let items_value = map
        .get("items")
        .and_then(Value::as_array)
        .filter(|items| !items.is_empty())
        .ok_or_else(|| contract_error("submission items must be a non-empty array"))?;

    let indexes = kit_indexes(&manifest)?;
    let expected_items: BTreeSet<String> = indexes.keys().cloned().collect();
    let mut seen_items = HashSet::new();
    let mut normalized_items = Vec::new();

    for (index, item_value) in items_value.iter().enumerate() {
        let item_map = item_value
            .as_object()
            .ok_or_else(|| contract_error(format!("submission.items[{index}] must be an object")))?;
        assert_exact_keys(
            item_map,
            SUBMISSION_ITEM_KEYS,
            &format!("submission.items[{index}]"),
        )?;

        let item_id = item_map
            .get("item_id")
            .and_then(Value::as_str)
            .filter(|value| is_safe_id(value))
            .ok_or_else(|| {
                contract_error(format!("submission.items[{index}].item_id is invalid"))
            })?;
        if !expected_items.contains(item_id) {
            return Err(contract_error(format!(
                "submission references unknown item_id: {item_id}"
            )));
        }
        if !seen_items.insert(item_id.to_string()) {
            return Err(contract_error(format!("duplicate submission item_id: {item_id}")));
        }
        if !item_map.contains_key("clustered") || !item_map.contains_key("unclustered") {
            return Err(contract_error(
                "each submission item must include clustered and unclustered",
            ));
        }

        let (choice_ids, cluster_ids) = indexes
            .get(item_id)
            .expect("item id was validated against manifest");
        assert_decision_shape(
            item_map
                .get("clustered")
                .expect("clustered key was checked"),
            &format!("submission.items[{index}].clustered"),
            "cluster",
            "cluster_id",
        )?;
        let clustered: ClusteredDecision = serde_json::from_value(
            item_map
                .get("clustered")
                .expect("clustered key was checked")
                .clone(),
        )
        .map_err(|_| {
            contract_error(format!(
                "submission.items[{index}].clustered must be an exact cluster/none decision"
            ))
        })?;
        if let ClusteredDecision::Cluster { cluster_id } = &clustered {
            if !is_safe_id(cluster_id) || !cluster_ids.contains(cluster_id) {
                return Err(contract_error(format!(
                    "cluster_id is not valid for item {item_id}"
                )));
            }
        }
        assert_decision_shape(
            item_map
                .get("unclustered")
                .expect("unclustered key was checked"),
            &format!("submission.items[{index}].unclustered"),
            "exact",
            "choice_id",
        )?;
        let unclustered: UnclusteredDecision = serde_json::from_value(
            item_map
                .get("unclustered")
                .expect("unclustered key was checked")
                .clone(),
        )
        .map_err(|_| {
            contract_error(format!(
                "submission.items[{index}].unclustered must be an exact/none decision"
            ))
        })?;
        if let UnclusteredDecision::Exact { choice_id } = &unclustered {
            if !is_safe_id(choice_id) || !choice_ids.contains(choice_id) {
                return Err(contract_error(format!(
                    "choice_id is not valid for item {item_id}"
                )));
            }
        }

        normalized_items.push(SubmissionItem {
            item_id: item_id.to_string(),
            clustered,
            unclustered,
        });
    }

    let seen_set: BTreeSet<String> = seen_items.into_iter().collect();
    if seen_set != expected_items {
        let missing: Vec<String> = expected_items.difference(&seen_set).cloned().collect();
        return Err(contract_error(format!(
            "submission must include exactly one decision for every round item; missing {missing:?}"
        )));
    }

    if normalized_items
        .windows(2)
        .any(|pair| pair[0].item_id.as_str() > pair[1].item_id.as_str())
    {
        return Err(contract_error(
            "submission payload is not in canonical item order",
        ));
    }
    normalized_items.sort_by(|left, right| left.item_id.cmp(&right.item_id));
    let normalized = Submission {
        schema_version: SUBMISSION_SCHEMA_VERSION.to_string(),
        submission_id: parsed_uuid.to_string(),
        environment: environment.to_string(),
        round_id: round_id.to_string(),
        blind_manifest_sha256: blind_manifest_sha256.to_string(),
        kit_sha256: kit_sha256.to_string(),
        items: normalized_items,
    };

    let payload = canonical_json(
        &serde_json::to_value(&normalized)
            .map_err(|error| contract_error(format!("submission serialization failed: {error}")))?,
    );
    if payload.len() > MAX_SUBMISSION_PAYLOAD_BYTES {
        return Err(contract_error(format!(
            "submission payload exceeds {MAX_SUBMISSION_PAYLOAD_BYTES} bytes"
        )));
    }

    Ok(normalized)
}

pub fn build_selector_submission(
    manifest_raw: &str,
    submission_id: &str,
    items_raw: &str,
) -> Result<Submission, ContractError> {
    let manifest = parse_public_kit_manifest(manifest_raw)?;
    let mut items: Vec<SubmissionItem> = serde_json::from_str(items_raw)
        .map_err(|error| contract_error(format!("items JSON is invalid: {error}")))?;
    items.sort_by(|left, right| left.item_id.cmp(&right.item_id));
    let submission = Submission {
        schema_version: SUBMISSION_SCHEMA_VERSION.to_string(),
        submission_id: submission_id.to_string(),
        environment: manifest.environment.clone(),
        round_id: manifest.round_id.clone(),
        blind_manifest_sha256: manifest.blind_manifest_sha256.clone(),
        kit_sha256: manifest.kit_sha256.clone(),
        items,
    };
    validate_complete_submission(
        &serde_json::to_string(&submission).expect("submission serializes"),
        manifest_raw,
    )
}

pub fn digest_submission(submission: &Submission) -> String {
    let value = serde_json::to_value(submission).expect("submission serializes");
    sha256_hex(&canonical_json(&value))
}

fn to_js_error(error: ContractError) -> JsValue {
    JsValue::from_str(&error.to_string())
}

#[wasm_bindgen(js_name = validateSubmission)]
pub fn validate_submission_wasm(manifest_json: &str, submission_json: &str) -> Result<String, JsValue> {
    let normalized = validate_complete_submission(submission_json, manifest_json)
        .map_err(to_js_error)?;
    Ok(canonical_json(
        &serde_json::to_value(&normalized).map_err(|error| JsValue::from_str(&error.to_string()))?,
    ))
}

#[wasm_bindgen(js_name = buildSubmission)]
pub fn build_submission_wasm(
    manifest_json: &str,
    submission_id: &str,
    items_json: &str,
) -> Result<String, JsValue> {
    let normalized = build_selector_submission(manifest_json, submission_id, items_json)
        .map_err(to_js_error)?;
    Ok(canonical_json(
        &serde_json::to_value(&normalized).map_err(|error| JsValue::from_str(&error.to_string()))?,
    ))
}

#[wasm_bindgen(js_name = buildTemplate)]
pub fn build_template_wasm(manifest_json: &str) -> Result<String, JsValue> {
    let manifest = parse_public_kit_manifest(manifest_json).map_err(to_js_error)?;
    let template = build_submission_template(&manifest);
    Ok(canonical_json(
        &serde_json::to_value(template).map_err(|error| JsValue::from_str(&error.to_string()))?,
    ))
}

#[wasm_bindgen(js_name = digestSubmission)]
pub fn digest_submission_wasm(manifest_json: &str, submission_json: &str) -> Result<String, JsValue> {
    let normalized = validate_complete_submission(submission_json, manifest_json)
        .map_err(to_js_error)?;
    Ok(digest_submission(&normalized))
}

#[cfg(test)]
mod tests {
    use super::*;

    const MANIFEST: &str = r#"{
      "schema_version": "foldarium.weekly-selector-kit/v2",
      "environment": "preview",
      "round_id": "weekly-2026-08-08",
      "blind_manifest_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "kit_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "items": [
        {
          "item_id": "item-a",
          "choices": [
            {"choice_id": "choice-1", "cluster_id": "cluster-x"},
            {"choice_id": "choice-2", "cluster_id": "cluster-x"}
          ]
        },
        {
          "item_id": "item-b",
          "choices": [
            {"choice_id": "choice-4", "cluster_id": "cluster-y"}
          ]
        }
      ]
    }"#;

    #[test]
    fn validates_complete_submission() {
        let submission = validate_complete_submission(
            r#"{
              "schema_version": "foldarium.selector-submission/v2",
              "submission_id": "00000000-0000-4000-8000-000000000001",
              "environment": "preview",
              "round_id": "weekly-2026-08-08",
              "blind_manifest_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
              "kit_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
              "items": [
                {
                  "item_id": "item-a",
                  "clustered": {"selection_kind": "cluster", "cluster_id": "cluster-x"},
                  "unclustered": {"selection_kind": "exact", "choice_id": "choice-1"}
                },
                {
                  "item_id": "item-b",
                  "clustered": {"selection_kind": "none"},
                  "unclustered": {"selection_kind": "none"}
                }
              ]
            }"#,
            MANIFEST,
        )
        .expect("submission validates");

        assert_eq!(submission.items.len(), 2);
        assert_eq!(submission.submission_id, "00000000-0000-4000-8000-000000000001");
    }

    #[test]
    fn rejects_unknown_item_ids() {
        let error = validate_complete_submission(
            r#"{
              "schema_version": "foldarium.selector-submission/v2",
              "submission_id": "00000000-0000-4000-8000-000000000001",
              "environment": "preview",
              "round_id": "weekly-2026-08-08",
              "blind_manifest_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
              "kit_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
              "items": [
                {
                  "item_id": "missing",
                  "clustered": {"selection_kind": "none"},
                  "unclustered": {"selection_kind": "none"}
                }
              ]
            }"#,
            MANIFEST,
        )
        .expect_err("unknown item should fail");
        assert!(error.to_string().contains("unknown item_id"));
    }
}
