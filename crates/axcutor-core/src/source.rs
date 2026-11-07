//! Source decoding preserves exact numbers and insertion order. serde_json owns
//! JSON syntax and escapes; YAML events admit only JSON-compatible standard tags.
//! Neither format admits duplicate keys or nesting beyond 128 levels.
//! Canvas adapters reuse the bounded JSON decoder with a model wrapper allowance.
use num_bigint::BigInt;
use regex::Regex;
use serde::{
    Deserialize, Deserializer, Serialize,
    de::{self, MapAccess, Visitor},
};
use serde_json::{Map, Value, value::RawValue};
use std::{collections::HashSet, fmt, sync::LazyLock};
use yaml_rust2::{
    parser::{Event, Parser, Tag},
    scanner::{Marker, TScalarStyle},
};

type LoadError = (String, Option<SourceLocation>);
pub(crate) const MAX_SOURCE_DEPTH: usize = 128;
// GraphAnalysis's deepest TypeSpec slot is nodes.<id>.injected.<var>.
pub(crate) const MAX_MODEL_DEPTH: usize = MAX_SOURCE_DEPTH + 4;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum SourceFormat {
    Yaml,
    Json,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SourceDocument {
    pub format: SourceFormat,
    pub text: String,
}

/// One-based coordinates proved by the source parser.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct SourceLocation {
    pub line: usize,
    pub column: usize,
}

fn location(mark: Marker) -> Option<SourceLocation> {
    Some(SourceLocation {
        line: mark.line(),
        column: mark.col() + 1,
    })
}

pub(crate) fn load(source: &SourceDocument) -> Result<Value, LoadError> {
    if matches!(source.format, SourceFormat::Json) {
        return load_json(&source.text, MAX_SOURCE_DEPTH);
    }
    let mut parser = Parser::new_from_str(&source.text);
    let mut events = vec![];
    loop {
        let (event, mark) = parser
            .next_token()
            .map_err(|e| (e.info().into(), location(*e.marker())))?;
        let done = event == Event::StreamEnd;
        events.push((event, mark));
        if done {
            break;
        }
    }
    let mut i = 0;
    while i < events.len() && matches!(events[i].0, Event::StreamStart | Event::DocumentStart) {
        i += 1;
    }
    let value = node(&events, &mut i, 0)?;
    if i < events.len() && events[i].0 == Event::DocumentEnd {
        i += 1;
    }
    if i >= events.len() || events[i].0 != Event::StreamEnd {
        return Err((
            "exactly one source document is required".into(),
            events.get(i).and_then(|(_, m)| location(*m)),
        ));
    }
    Ok(value)
}

pub(crate) fn load_json(text: &str, max_depth: usize) -> Result<Value, LoadError> {
    let raw = serde_json::from_str::<&RawValue>(text).map_err(|e| json_error(text, text, e))?;
    json_node(text, raw, 0, max_depth)
}

/// Preserve ordinary object keys even when serde_json reserves their spelling.
pub(crate) fn deserialize_value<'de, D: Deserializer<'de>>(d: D) -> Result<Value, D::Error> {
    let raw = Box::<RawValue>::deserialize(d)?;
    load_json(raw.get(), MAX_MODEL_DEPTH).map_err(|(message, _)| de::Error::custom(message))
}

// RawValue borrows a parser-proved span of the original source. Translate the
// parser's fragment coordinates without searching for potentially repeated text.
fn json_location(
    source: &str,
    fragment: &str,
    line: usize,
    column: usize,
) -> Option<SourceLocation> {
    if line == 0 {
        return None;
    }
    let offset = fragment.as_ptr() as usize - source.as_ptr() as usize;
    let prefix = &source[..offset];
    Some(SourceLocation {
        line: prefix.bytes().filter(|b| *b == b'\n').count() + line,
        column: if line == 1 {
            prefix.rsplit('\n').next().unwrap().len() + column
        } else {
            column
        },
    })
}

fn json_error(source: &str, fragment: &str, e: serde_json::Error) -> LoadError {
    let mark = json_location(source, fragment, e.line(), e.column());
    (e.to_string(), mark)
}

struct JsonMap;
impl<'de> Visitor<'de> for JsonMap {
    type Value = Vec<(String, &'de RawValue)>;

    fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("an object with unique string keys")
    }

    fn visit_map<M: MapAccess<'de>>(self, mut map: M) -> Result<Self::Value, M::Error> {
        let mut keys = HashSet::new();
        let mut entries = vec![];
        while let Some(key) = map.next_key::<String>()? {
            if !keys.insert(key.clone()) {
                return Err(de::Error::custom(format!("duplicate mapping key {key}")));
            }
            entries.push((key, map.next_value::<&RawValue>()?));
        }
        Ok(entries)
    }
}

fn json_node(
    source: &str,
    raw: &RawValue,
    depth: usize,
    max_depth: usize,
) -> Result<Value, LoadError> {
    let text = raw.get();
    if depth > max_depth {
        return Err((
            format!("JSON nesting exceeds {max_depth} levels"),
            json_location(source, text, 1, 1),
        ));
    }
    match text.as_bytes()[0] {
        b'{' => {
            // Deserialize real objects explicitly: serde_json::Value otherwise
            // interprets its private arbitrary-precision number key as a number.
            let entries = serde_json::Deserializer::from_str(text)
                .deserialize_map(JsonMap)
                .map_err(|e| json_error(source, text, e))?;
            let mut map = Map::new();
            for (key, value) in entries {
                map.insert(key, json_node(source, value, depth + 1, max_depth)?);
            }
            Ok(Value::Object(map))
        }
        b'[' => {
            let values: Vec<&RawValue> =
                serde_json::from_str(text).map_err(|e| json_error(source, text, e))?;
            values
                .into_iter()
                .map(|v| json_node(source, v, depth + 1, max_depth))
                .collect::<Result<Vec<_>, _>>()
                .map(Value::Array)
        }
        _ => serde_json::from_str(text).map_err(|e| json_error(source, text, e)),
    }
}

fn node(events: &[(Event, Marker)], i: &mut usize, depth: usize) -> Result<Value, LoadError> {
    let Some((event, mark)) = events.get(*i) else {
        return Err(("missing document value".into(), None));
    };
    let fail = |message: &str| (message.into(), location(*mark));
    if depth > MAX_SOURCE_DEPTH {
        return Err(fail("source nesting exceeds 128 levels"));
    }
    *i += 1;
    match event {
        Event::Scalar(s, style, anchor, tag) => {
            if *anchor != 0 {
                return Err(fail("YAML anchors are unsupported"));
            }
            if let Some(tag) = tag {
                return tagged_scalar(s, tag).map_err(|e| fail(&e));
            }
            if *style != TScalarStyle::Plain {
                return Ok(Value::String(s.clone()));
            }
            scalar(s).map_err(|e| fail(&e))
        }
        Event::SequenceStart(anchor, tag) => {
            if *anchor != 0 || tag.as_ref().is_some_and(|t| standard_tag(t) != Some("seq")) {
                return Err(fail(
                    "expected an unanchored sequence with no tag or standard !!seq",
                ));
            }
            let mut a = vec![];
            while events
                .get(*i)
                .is_some_and(|(e, _)| *e != Event::SequenceEnd)
            {
                a.push(node(events, i, depth + 1)?);
            }
            *i += 1;
            Ok(Value::Array(a))
        }
        Event::MappingStart(anchor, tag) => {
            if *anchor != 0 || tag.as_ref().is_some_and(|t| standard_tag(t) != Some("map")) {
                return Err(fail(
                    "expected an unanchored mapping with no tag or standard !!map",
                ));
            }
            let mut m = Map::new();
            while events.get(*i).is_some_and(|(e, _)| *e != Event::MappingEnd) {
                let key_mark = events[*i].1;
                let Value::String(k) = node(events, i, depth + 1)? else {
                    return Err(("mapping keys must be strings".into(), location(key_mark)));
                };
                if m.contains_key(&k) {
                    return Err((format!("duplicate mapping key {k}"), location(key_mark)));
                }
                let v = node(events, i, depth + 1)?;
                m.insert(k, v);
            }
            *i += 1;
            Ok(Value::Object(m))
        }
        Event::Alias(_) => Err(fail("YAML aliases are unsupported")),
        _ => Err(fail("expected a JSON-compatible value")),
    }
}

fn standard_tag(tag: &Tag) -> Option<&str> {
    if tag.handle == "tag:yaml.org,2002:" {
        Some(&tag.suffix)
    } else if tag.handle.is_empty() {
        tag.suffix.strip_prefix("tag:yaml.org,2002:")
    } else {
        None
    }
}

fn tagged_scalar(s: &str, tag: &Tag) -> Result<Value, String> {
    if standard_tag(tag) == Some("str") {
        return Ok(Value::String(s.into()));
    }
    let value = scalar(s)?;
    match (standard_tag(tag), &value) {
        (Some("int"), Value::Number(n)) if !n.as_str().contains(['.', 'e', 'E']) => Ok(value),
        (Some("float"), Value::Number(n)) if DECIMAL.is_match(s) => {
            if n.as_str().contains(['.', 'e', 'E']) {
                Ok(value)
            } else {
                serde_json::from_str(&format!("{n}.0")).map_err(|e| e.to_string())
            }
        }
        (Some("bool"), Value::Bool(_)) | (Some("null"), Value::Null) => Ok(value),
        _ => Err("unsupported YAML tag or tag/value type mismatch".into()),
    }
}

static DECIMAL: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$").unwrap()
});

fn scalar(s: &str) -> Result<Value, String> {
    match s {
        "" | "~" | "null" | "Null" | "NULL" => return Ok(Value::Null),
        "true" | "True" | "TRUE" => return Ok(Value::Bool(true)),
        "false" | "False" | "FALSE" => return Ok(Value::Bool(false)),
        _ => (),
    }
    if matches!(
        s.to_ascii_lowercase().as_str(),
        ".inf" | "-.inf" | "+.inf" | ".nan"
    ) {
        return Err("non-finite numbers are unsupported".into());
    }
    let unsigned = s.trim_start_matches(['+', '-']);
    if let Some((digits, radix)) = unsigned
        .strip_prefix("0x")
        .map(|s| (s, 16))
        .or_else(|| unsigned.strip_prefix("0o").map(|s| (s, 8)))
    {
        if let Some(n) = BigInt::parse_bytes(digits.as_bytes(), radix) {
            let n = if s.starts_with('-') { -n } else { n };
            return serde_json::from_str(&n.to_string()).map_err(|e| e.to_string());
        }
    }
    if DECIMAL.is_match(s) {
        let negative = s.starts_with('-');
        let s = s.trim_start_matches(['+', '-']);
        let (mantissa, exponent) = s
            .split_once(['e', 'E'])
            .map_or((s, None), |(a, b)| (a, Some(b)));
        let (whole, fraction) = mantissa
            .split_once('.')
            .map_or((mantissa, None), |(a, b)| (a, Some(b)));
        let whole = whole.trim_start_matches('0');
        let mut normalized = format!(
            "{}{}",
            if negative { "-" } else { "" },
            if whole.is_empty() { "0" } else { whole }
        );
        if let Some(f) = fraction {
            normalized.push('.');
            normalized.push_str(if f.is_empty() { "0" } else { f });
        }
        if let Some(e) = exponent {
            normalized.push('e');
            normalized.push_str(e);
        }
        return serde_json::from_str(&normalized).map_err(|e| e.to_string());
    }
    Ok(Value::String(s.into()))
}
