//! Closed TypeSpec algebra, subtype relation and target-directed literal validation.
use super::{Ordered, Symbols, control_id, package_path, package_segment};
use num_bigint::BigInt;
use serde::{Deserialize, Deserializer, Serialize, Serializer, de::Error};
use serde_json::{Value, json, value::RawValue};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Scalar {
    String,
    Integer,
    Number,
    Boolean,
}

#[derive(Clone, Debug)]
pub enum TypeKind {
    Handle(String),
    Scalar {
        base: Scalar,
        enumeration: Option<Vec<Value>>,
    },
    Array(Box<TypeSpec>),
    TypedDict {
        properties: Symbols,
        required: Vec<String>,
    },
    OrderedMap(Box<TypeSpec>),
}

/// Always well formed when parsed from the public JSON text API.
#[derive(Clone, Debug)]
pub struct TypeSpec {
    pub kind: TypeKind,
    pub nullable: bool,
    pub description: Option<String>,
}

impl TypeSpec {
    pub fn handle(kind: &str) -> Result<Self, String> {
        if !resource_kind(kind) {
            return Err("handle kind must be <ModulePath>:<PublicClassName>".into());
        }
        Ok(Self {
            kind: TypeKind::Handle(kind.into()),
            nullable: false,
            description: None,
        })
    }

    pub fn contains_handle(&self) -> bool {
        match &self.kind {
            TypeKind::Handle(_) => true,
            TypeKind::Array(t) | TypeKind::OrderedMap(t) => t.contains_handle(),
            TypeKind::TypedDict { properties, .. } => {
                properties.values().any(Self::contains_handle)
            }
            TypeKind::Scalar { .. } => false,
        }
    }

    /// Nominal kinds referenced at any depth, in declaration order.
    pub fn handle_kinds(&self) -> Vec<&str> {
        match &self.kind {
            TypeKind::Handle(kind) => vec![kind],
            TypeKind::Array(t) | TypeKind::OrderedMap(t) => t.handle_kinds(),
            TypeKind::TypedDict { properties, .. } => {
                properties.values().flat_map(Self::handle_kinds).collect()
            }
            TypeKind::Scalar { .. } => vec![],
        }
    }

    /// External envelopes grant no I/O authority; hosts must authorize locators.
    pub fn accepts_external(
        &self,
        value: &Value,
        resources: &Ordered<super::ResourceSpec>,
    ) -> bool {
        self.accepts_value(value, &|kind, value| {
            value.as_object().is_some_and(|m| {
                m.len() == 2
                    && m.get("$handle").and_then(Value::as_str) == Some(kind)
                    && resources
                        .get(kind)
                        .and_then(|r| r.locator.as_ref())
                        .is_some_and(|t| m.get("locator").is_some_and(|v| t.accepts_literal(v)))
            })
        })
    }

    pub fn scalar(base: Scalar) -> Self {
        Self {
            kind: TypeKind::Scalar {
                base,
                enumeration: None,
            },
            nullable: false,
            description: None,
        }
    }
    pub fn array(items: Self) -> Self {
        Self {
            kind: TypeKind::Array(Box::new(items)),
            nullable: false,
            description: None,
        }
    }
    pub fn map(values: Self) -> Self {
        Self {
            kind: TypeKind::OrderedMap(Box::new(values)),
            nullable: false,
            description: None,
        }
    }
    pub fn strip_null(&self) -> Self {
        let mut t = self.clone();
        t.nullable = false;
        t
    }
    pub fn make_nullable(&self) -> Self {
        let mut t = self.clone();
        t.nullable = true;
        t
    }
    pub fn base(&self) -> &'static str {
        match self.kind {
            TypeKind::Handle(_) => "handle",
            TypeKind::Scalar {
                base: Scalar::String,
                ..
            } => "string",
            TypeKind::Scalar {
                base: Scalar::Integer,
                ..
            } => "integer",
            TypeKind::Scalar {
                base: Scalar::Number,
                ..
            } => "number",
            TypeKind::Scalar {
                base: Scalar::Boolean,
                ..
            } => "boolean",
            TypeKind::Array(_) => "array",
            TypeKind::TypedDict { .. } | TypeKind::OrderedMap(_) => "object",
        }
    }
    pub fn parse(v: &Value) -> Result<Self, String> {
        let m = v.as_object().ok_or("TypeSpec must be an object")?;
        let (base, nullable) = match m.get("type") {
            Some(Value::String(b)) => (b.as_str(), false),
            Some(Value::Array(a)) if a.len() == 2 => match (a[0].as_str(), a[1].as_str()) {
                (Some("null"), Some(b)) | (Some(b), Some("null")) if b != "null" => (b, true),
                _ => return Err("type union must contain one base type and null".into()),
            },
            _ => return Err("type must be a base name or [base, null]".into()),
        };
        let desc = match m.get("description") {
            None => None,
            Some(Value::String(s)) => Some(s.clone()),
            _ => return Err("description must be a string".into()),
        };
        let fields: &[&str] = match base {
            "handle" => &["type", "kind", "description"],
            "string" | "integer" | "number" | "boolean" => &["type", "description", "enum"],
            "array" => &["type", "description", "items"],
            "object" if m.contains_key("properties") => {
                &["type", "description", "properties", "required"]
            }
            "object" => &["type", "description", "additionalProperties"],
            _ => return Err(format!("unsupported base type {base}")),
        };
        if let Some(k) = m.keys().find(|k| !fields.contains(&k.as_str())) {
            return Err(format!("field {k} is not allowed on {base}"));
        }
        let kind = match base {
            "handle" => {
                Self::handle(
                    m.get("kind")
                        .and_then(Value::as_str)
                        .ok_or("handle requires kind")?,
                )?
                .kind
            }
            "array" => TypeKind::Array(Box::new(Self::parse(
                m.get("items").ok_or("array requires items")?,
            )?)),
            "object" if m.contains_key("properties") => {
                let props = m["properties"]
                    .as_object()
                    .filter(|m| !m.is_empty())
                    .ok_or("properties must be a nonempty object")?;
                let mut properties = Ordered::new();
                for (k, val) in props {
                    if !control_id(k) {
                        return Err(format!("invalid property {k}"));
                    }
                    properties.insert(
                        k.clone(),
                        Self::parse(val).map_err(|e| format!("property {k}: {e}"))?,
                    );
                }
                let required = if let Some(r) = m.get("required") {
                    let a = r.as_array().ok_or("required must be an array")?;
                    let mut names = Vec::new();
                    for v in a {
                        let s = v.as_str().ok_or("required entries must be strings")?;
                        if !properties.contains_key(s) || names.iter().any(|n| n == s) {
                            return Err(format!("invalid/duplicate required property {s}"));
                        }
                        names.push(s.to_owned());
                    }
                    names
                } else {
                    properties.keys().cloned().collect()
                };
                TypeKind::TypedDict {
                    properties,
                    required,
                }
            }
            "object" => TypeKind::OrderedMap(Box::new(Self::parse(
                m.get("additionalProperties")
                    .ok_or("object requires properties or additionalProperties")?,
            )?)),
            _ => {
                let base = match base {
                    "string" => Scalar::String,
                    "integer" => Scalar::Integer,
                    "number" => Scalar::Number,
                    _ => Scalar::Boolean,
                };
                let enumeration = if let Some(e) = m.get("enum") {
                    let a = e
                        .as_array()
                        .filter(|a| !a.is_empty())
                        .ok_or("enum must be a nonempty array")?;
                    for (i, val) in a.iter().enumerate() {
                        if !scalar_matches(val, base) || a[..i].iter().any(|x| literal_eq(x, val)) {
                            return Err("enum values must match base type and be unique".into());
                        }
                    }
                    Some(a.clone())
                } else {
                    None
                };
                TypeKind::Scalar { base, enumeration }
            }
        };
        Ok(Self {
            kind,
            nullable,
            description: desc,
        })
    }

    /// Structural equivalence deliberately ignores descriptions and declaration order.
    pub fn equivalent(&self, other: &Self) -> bool {
        self.base() == other.base() && self.is_subtype_of(other) && other.is_subtype_of(self)
    }

    pub fn is_subtype_of(&self, expected: &Self) -> bool {
        if self.nullable && !expected.nullable {
            return false;
        }
        match (&self.kind, &expected.kind) {
            (TypeKind::Handle(a), TypeKind::Handle(b)) => a == b,
            (
                TypeKind::Scalar {
                    base: a,
                    enumeration: ea,
                },
                TypeKind::Scalar {
                    base: b,
                    enumeration: eb,
                },
            ) => {
                (a == b || (*a == Scalar::Integer && *b == Scalar::Number))
                    && match eb {
                        None => true,
                        Some(b) => ea
                            .as_ref()
                            .is_some_and(|a| a.iter().all(|x| b.iter().any(|y| literal_eq(x, y)))),
                    }
            }
            (TypeKind::Array(a), TypeKind::Array(b))
            | (TypeKind::OrderedMap(a), TypeKind::OrderedMap(b)) => a.is_subtype_of(b),
            (
                TypeKind::TypedDict {
                    properties: a,
                    required: ra,
                },
                TypeKind::TypedDict {
                    properties: b,
                    required: rb,
                },
            ) => {
                rb.iter().all(|r| ra.contains(r))
                    && a.iter()
                        .all(|(k, t)| b.get(k).is_some_and(|e| t.is_subtype_of(e)))
            }
            _ => false,
        }
    }

    pub fn accepts(&self, value: &Value) -> bool {
        self.accepts_runtime(value)
    }

    /// Literals never confer resource identity, even inside containers.
    pub fn accepts_literal(&self, value: &Value) -> bool {
        self.accepts_value(value, &|_, _| false)
    }

    /// Shape validation grants no permission to dereference a resource.
    pub fn accepts_runtime(&self, value: &Value) -> bool {
        self.accepts_value(value, &|kind, value| {
            value.as_object().is_some_and(|m| {
                m.len() == 2
                    && m.get("$handle")
                        .and_then(Value::as_str)
                        .is_some_and(|s| !s.is_empty())
                    && m.get("kind").and_then(Value::as_str) == Some(kind)
            })
        })
    }

    fn accepts_value(&self, value: &Value, handle: &impl Fn(&str, &Value) -> bool) -> bool {
        if value.is_null() {
            return self.nullable;
        }
        match &self.kind {
            TypeKind::Handle(kind) => handle(kind, value),
            TypeKind::Scalar { base, enumeration } => {
                scalar_matches(value, *base)
                    && enumeration
                        .as_ref()
                        .is_none_or(|e| e.iter().any(|v| literal_eq(v, value)))
            }
            TypeKind::Array(t) => value
                .as_array()
                .is_some_and(|a| a.iter().all(|v| t.accepts_value(v, handle))),
            TypeKind::OrderedMap(t) => value
                .as_object()
                .is_some_and(|m| m.values().all(|v| t.accepts_value(v, handle))),
            TypeKind::TypedDict {
                properties,
                required,
            } => value.as_object().is_some_and(|m| {
                required.iter().all(|k| m.contains_key(k))
                    && m.iter().all(|(k, v)| {
                        properties
                            .get(k)
                            .is_some_and(|t| t.accepts_value(v, handle))
                    })
            }),
        }
    }

    pub fn to_value(&self) -> Value {
        let mut v = json!({"type": self.base()});
        if self.nullable {
            v["type"] = json!([self.base(), "null"]);
        }
        if let Some(d) = &self.description {
            v["description"] = json!(d);
        }
        match &self.kind {
            TypeKind::Handle(kind) => v["kind"] = json!(kind),
            TypeKind::Scalar {
                enumeration: Some(e),
                ..
            } => v["enum"] = json!(e),
            TypeKind::Scalar { .. } => (),
            TypeKind::Array(t) => v["items"] = t.to_value(),
            TypeKind::OrderedMap(t) => v["additionalProperties"] = t.to_value(),
            TypeKind::TypedDict {
                properties,
                required,
            } => {
                v["properties"] = json!(properties);
                v["required"] = json!(required);
            }
        }
        v
    }
}

pub fn resource_kind(kind: &str) -> bool {
    kind.split_once(':').is_some_and(|(module, class)| {
        package_path(module) && package_segment(class) && !class.starts_with('_')
    })
}

impl Serialize for TypeSpec {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        self.to_value().serialize(s)
    }
}
impl<'de> Deserialize<'de> for TypeSpec {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        // A JSON snapshot preserves actual object/number kinds when a Value
        // deserializer would replay private Number/RawValue marker maps. Reuse
        // the bounded decoder; adapters also bound each complete query argument.
        let raw = Box::<RawValue>::deserialize(d)?;
        let value = crate::source::load_json(raw.get(), crate::source::MAX_MODEL_DEPTH)
            .map_err(|(message, _)| D::Error::custom(message))?;
        Self::parse(&value).map_err(D::Error::custom)
    }
}

pub fn integer_literal(v: &Value) -> bool {
    v.as_number()
        .is_some_and(|n| !n.as_str().contains(['.', 'e', 'E']))
}

fn scalar_matches(v: &Value, base: Scalar) -> bool {
    match base {
        Scalar::String => v.is_string(),
        Scalar::Integer => integer_literal(v),
        Scalar::Number => v.is_number(),
        Scalar::Boolean => v.is_boolean(),
    }
}

/// Canonical sign, significant digits and base-ten exponent, without rounding.
fn decimal(s: &str) -> (bool, String, BigInt) {
    let negative = s.starts_with('-');
    let s = s.trim_start_matches('-');
    let (mantissa, exponent) = s.split_once(['e', 'E']).unwrap_or((s, "0"));
    let fraction = mantissa.split_once('.').map_or(0, |(_, f)| f.len());
    let mut digits: String = mantissa.chars().filter(|c| *c != '.').collect();
    let trailing = digits.len() - digits.trim_end_matches('0').len();
    digits = digits.trim_start_matches('0').trim_end_matches('0').into();
    if digits.is_empty() {
        return (false, String::new(), BigInt::from(0));
    }
    let exponent = exponent.parse::<BigInt>().expect("validated JSON exponent")
        + BigInt::from(trailing)
        - BigInt::from(fraction);
    (negative, digits, exponent)
}

/// Exact real ordering of JSON numbers, including exponents too large for f64.
pub fn number_cmp(a: &serde_json::Number, b: &serde_json::Number) -> std::cmp::Ordering {
    let (na, da, ea) = decimal(a.as_str());
    let (nb, db, eb) = decimal(b.as_str());
    if na != nb {
        return nb.cmp(&na);
    }
    let magnitude = match (da.is_empty(), db.is_empty()) {
        (true, true) => std::cmp::Ordering::Equal,
        (true, false) => std::cmp::Ordering::Less,
        (false, true) => std::cmp::Ordering::Greater,
        (false, false) => (ea + BigInt::from(da.len()))
            .cmp(&(eb + BigInt::from(db.len())))
            .then_with(|| {
                let len = da.len().max(db.len());
                da.bytes()
                    .chain(std::iter::repeat(b'0'))
                    .take(len)
                    .cmp(db.bytes().chain(std::iter::repeat(b'0')).take(len))
            }),
    };
    if na { magnitude.reverse() } else { magnitude }
}

/// Exact decimal equality, including integer/float spelling, without f64 conversion.
pub fn literal_eq(a: &Value, b: &Value) -> bool {
    match (a, b) {
        (Value::Number(a), Value::Number(b)) => decimal(a.as_str()) == decimal(b.as_str()),
        (Value::Array(a), Value::Array(b)) => {
            a.len() == b.len() && a.iter().zip(b).all(|(a, b)| literal_eq(a, b))
        }
        (Value::Object(a), Value::Object(b)) => {
            a.len() == b.len()
                && a.iter()
                    .all(|(k, v)| b.get(k).is_some_and(|b| literal_eq(v, b)))
        }
        _ => a == b,
    }
}
