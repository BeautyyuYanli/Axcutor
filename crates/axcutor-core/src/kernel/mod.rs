//! Deterministic, owned-value runtime primitives. No I/O, clocks or global state.
//! Inputs are compiler-checked ArgValues and symbol tables. Broken engine
//! invariants are distinct from recoverable executor contract violations.
//! Routing (including kill_vars), frames and scheduling belong to the host.
use crate::{ast::*, compiler::arg_type, source};
use num_bigint::BigInt;
use serde::Serialize;
use serde_json::{Map, Number, Value};
use std::{cmp::Ordering, fmt};

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum KernelError {
    Argument(String),
    Input(String),
    Contract(String),
    Engine(String),
}

impl fmt::Display for KernelError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Argument(s) | Self::Input(s) | Self::Contract(s) | Self::Engine(s) => {
                f.write_str(s)
            }
        }
    }
}
impl std::error::Error for KernelError {}

pub type Environment = Map<String, Value>;

#[derive(Clone, Debug, Serialize)]
pub struct ExecutionResult {
    pub branch: Option<String>,
    /// Only committed output fields, already renamed into the caller's scope.
    pub output: Environment,
}

fn engine(message: impl Into<String>) -> KernelError {
    KernelError::Engine(message.into())
}

/// Only a missing final leaf becomes null. Missing roots and intermediate
/// objects violate compiler-checked definite assignment/projection invariants.
pub fn eval_arg(arg: &ArgValue, env: &Environment) -> Result<Value, KernelError> {
    match arg {
        ArgValue::Const { value } => Ok(value.clone()),
        ArgValue::Path { path } => {
            let mut value = env
                .get(&path.root)
                .ok_or_else(|| engine(format!("unbound {}", path.root)))?;
            for (i, field) in path.fields.iter().enumerate() {
                let object = value
                    .as_object()
                    .ok_or_else(|| engine(format!("invalid path {}", path.text())))?;
                match object.get(field) {
                    Some(next) => value = next,
                    None if i + 1 == path.fields.len() => return Ok(Value::Null),
                    None => {
                        return Err(engine(format!(
                            "missing intermediate field in {}",
                            path.text()
                        )));
                    }
                }
            }
            Ok(value.clone())
        }
    }
}

/// Validate and project an executor/graph response atomically. Extra top-level
/// outputs are ignored; TypeSpec::accepts still closes nested TypedDict values.
pub fn validate_run_contract(
    response: &Value,
    schema: &Symbols,
    branches: &Ordered<BranchContract>,
) -> Result<ExecutionResult, KernelError> {
    let fail = |s: &str| KernelError::Contract(s.into());
    let response = response
        .as_object()
        .ok_or_else(|| fail("response must be an object"))?;
    let branch = match response.get("branch") {
        None | Some(Value::Null) if branches.is_empty() => None,
        Some(Value::String(b)) if branches.contains_key(b) => Some(b.clone()),
        _ => return Err(fail("invalid response branch")),
    };
    let fields: Vec<_> = match &branch {
        Some(b) => branches[b].output.iter().map(String::as_str).collect(),
        None => schema.keys().map(String::as_str).collect(),
    };
    let raw = response
        .get("output")
        .and_then(Value::as_object)
        .ok_or_else(|| fail("output must be an object"))?;
    let mut output = Environment::new();
    for name in fields {
        let typespec = schema
            .get(name)
            .ok_or_else(|| engine(format!("unknown contract output {name}")))?;
        let value = raw
            .get(name)
            .ok_or_else(|| fail(&format!("missing output {name}")))?;
        if !typespec.accepts_runtime(value) {
            return Err(fail(&format!("output {name} violates TypeSpec")));
        }
        output.insert(name.into(), value.clone());
    }
    Ok(ExecutionResult { branch, output })
}

/// Root ingress errors cannot be consumed by a DSL node's retry/catch.
pub fn validate_input(input: &Value, schema: &Symbols) -> Result<Value, KernelError> {
    let fail = || {
        KernelError::Input(
            "input must contain exactly the declared parameters with valid types".into(),
        )
    };
    let values = input.as_object().ok_or_else(fail)?;
    if values.len() != schema.len()
        || !schema
            .iter()
            .all(|(k, t)| values.get(k).is_some_and(|v| t.accepts_runtime(v)))
    {
        return Err(fail());
    }
    Ok(input.clone())
}

fn array(v: &Value) -> Result<&Vec<Value>, KernelError> {
    v.as_array().ok_or_else(|| engine("expected array"))
}
fn object(v: &Value) -> Result<&Environment, KernelError> {
    v.as_object().ok_or_else(|| engine("expected object"))
}
fn string(v: &Value) -> Result<&str, KernelError> {
    v.as_str().ok_or_else(|| engine("expected string"))
}
fn boolean(v: &Value) -> Result<bool, KernelError> {
    v.as_bool().ok_or_else(|| engine("expected boolean"))
}
fn integer(v: &Value) -> Result<BigInt, KernelError> {
    if !integer_literal(v) {
        return Err(engine("expected integer"));
    }
    v.as_number()
        .unwrap()
        .as_str()
        .parse()
        .map_err(|_| engine("invalid integer"))
}
fn float(v: &Value) -> Result<f64, KernelError> {
    v.as_f64()
        .filter(|n| n.is_finite())
        .ok_or_else(|| engine("operand cannot be represented as a finite f64"))
}

/// All sixteen builtins return fresh output values. Partial branches omit
/// outputs; the host applies the compiled route's kill_vars (never here).
pub fn eval_builtin(
    op: &Builtin,
    bindings: &Bindings,
    env: &Environment,
    symbols: &Symbols,
) -> Result<ExecutionResult, KernelError> {
    let arg = |name: &str| {
        bindings
            .input
            .get(name)
            .ok_or_else(|| engine(format!("missing input {name}")))
    };
    let get = |name: &str| eval_arg(arg(name)?, env);
    let mut result = ExecutionResult {
        branch: None,
        output: Environment::new(),
    };
    let mut put = |name: &str, value: Value| -> Result<(), KernelError> {
        let target = bindings
            .output
            .get(name)
            .ok_or_else(|| engine(format!("missing output binding {name}")))?;
        result.output.insert(target.clone(), value);
        Ok(())
    };
    // Optional retrieval must distinguish a present null from an absent slot.
    let optional = |value: Option<Value>| -> Result<(Option<String>, Option<Value>), KernelError> {
        if bindings.input.contains_key("default") {
            Ok((
                None,
                Some(match value {
                    Some(v) => v,
                    None => get("default")?,
                }),
            ))
        } else {
            Ok((
                Some(if value.is_some() { "some" } else { "none" }.into()),
                value,
            ))
        }
    };
    match op {
        Builtin::Assign => put("value", get("source")?)?,
        Builtin::Unwrap | Builtin::ArrayGet | Builtin::MapGet => {
            let source = get("source")?;
            let value = match op {
                Builtin::Unwrap => (!source.is_null()).then_some(source),
                Builtin::ArrayGet => {
                    let source = array(&source)?;
                    let mut index = integer(&get("index")?)?;
                    if index < BigInt::from(0) {
                        index += BigInt::from(source.len());
                    }
                    index
                        .to_string()
                        .parse::<usize>()
                        .ok()
                        .and_then(|i| source.get(i))
                        .cloned()
                }
                _ => object(&source)?.get(string(&get("key")?)?).cloned(),
            };
            let (branch, value) = optional(value)?;
            if let Some(value) = value {
                put("value", value)?;
            }
            result.branch = branch;
        }
        Builtin::Switch => {
            let value = get("source")?;
            result.branch = Some(match value {
                Value::Bool(b) => b.to_string(),
                Value::String(s) => s,
                _ => return Err(engine("invalid switch operand")),
            });
        }
        Builtin::Compare | Builtin::Logic => {
            let operator = get("op")?;
            let operator = string(&operator)?;
            let value = if matches!(op, Builtin::Compare) {
                let (left, right) = (get("left")?, get("right")?);
                if matches!(operator, "eq" | "ne") {
                    literal_eq(&left, &right) == (operator == "eq")
                } else {
                    let order = match (&left, &right) {
                        (Value::Number(a), Value::Number(b)) => number_cmp(a, b),
                        (Value::String(a), Value::String(b)) => a.cmp(b),
                        _ => return Err(engine("invalid comparison operands")),
                    };
                    match operator {
                        "lt" => order == Ordering::Less,
                        "lte" => order != Ordering::Greater,
                        "gt" => order == Ordering::Greater,
                        "gte" => order != Ordering::Less,
                        _ => return Err(engine("invalid comparison operator")),
                    }
                }
            } else {
                match operator {
                    "not" => !boolean(&get("source")?)?,
                    "and" => boolean(&get("left")?)? & boolean(&get("right")?)?,
                    "or" => boolean(&get("left")?)? | boolean(&get("right")?)?,
                    _ => return Err(engine("invalid logic operator")),
                }
            };
            if bindings.output_present {
                put("result", Value::Bool(value))?;
            } else {
                result.branch = Some(value.to_string());
            }
        }
        Builtin::Math => {
            let operator = get("op")?;
            let operator = string(&operator)?;
            let (left, right) = (get("left")?, get("right")?);
            let partial = matches!(operator, "div" | "mod");
            let value = if partial && literal_eq(&right, &Value::from(0)) {
                None
            } else {
                let ints = ["left", "right"].iter().all(|p| {
                    arg(p)
                        .ok()
                        .and_then(|a| arg_type(a, symbols))
                        .is_some_and(|t| t.base() == "integer")
                });
                let number = if operator != "div" && ints {
                    let (a, b) = (integer(&left)?, integer(&right)?);
                    let value = match operator {
                        "add" => a + b,
                        "sub" => a - b,
                        "mul" => a * b,
                        "mod" => {
                            let r = a % &b;
                            if r != BigInt::from(0) && r.sign() != b.sign() {
                                r + b
                            } else {
                                r
                            }
                        }
                        _ => return Err(engine("invalid math operator")),
                    };
                    value
                        .to_string()
                        .parse::<Number>()
                        .map_err(|_| engine("invalid integer result"))?
                } else {
                    let (a, b) = (float(&left)?, float(&right)?);
                    let value = match operator {
                        "add" => a + b,
                        "sub" => a - b,
                        "mul" => a * b,
                        "div" => a / b,
                        _ => return Err(engine("invalid floating point operator")),
                    };
                    Number::from_f64(value).ok_or_else(|| engine("non-finite arithmetic result"))?
                };
                Some(Value::Number(number))
            };
            let (branch, value) = if partial {
                optional(value)?
            } else {
                (None, value)
            };
            if let Some(value) = value {
                put("result", value)?;
            }
            result.branch = branch;
        }
        Builtin::Length => {
            let n = match get("source")? {
                Value::String(s) => s.chars().count(),
                Value::Array(a) => a.len(),
                Value::Object(m) => m.len(),
                _ => return Err(engine("invalid length operand")),
            };
            put("result", Value::from(n))?;
        }
        Builtin::ArrayShift => {
            let source = get("source")?;
            let source = array(&source)?;
            if let Some(first) = source.first() {
                put("item", first.clone())?;
                put("rest", Value::Array(source[1..].to_vec()))?;
                result.branch = Some("some".into());
            } else {
                result.branch = Some("empty".into());
            }
        }
        Builtin::ArrayAppend | Builtin::ArrayConcat | Builtin::ArrayFlatten => {
            let value = match op {
                Builtin::ArrayAppend => {
                    let mut a = array(&get("source")?)?.clone();
                    a.push(get("item")?);
                    a
                }
                Builtin::ArrayConcat => {
                    let mut a = array(&get("left")?)?.clone();
                    a.extend(array(&get("right")?)?.clone());
                    a
                }
                _ => {
                    let mut a = vec![];
                    for inner in array(&get("source")?)? {
                        a.extend(array(inner)?.clone());
                    }
                    a
                }
            };
            put("result", Value::Array(value))?;
        }
        Builtin::MapSet | Builtin::MapDelete | Builtin::ObjectMerge => {
            let mut value = if matches!(op, Builtin::ObjectMerge) {
                if bindings.input.contains_key("base") {
                    object(&get("base")?)?.clone()
                } else {
                    Environment::new()
                }
            } else {
                object(&get("source")?)?.clone()
            };
            match op {
                Builtin::MapSet => {
                    value.insert(string(&get("key")?)?.into(), get("value")?);
                }
                Builtin::MapDelete => {
                    value.shift_remove(string(&get("key")?)?);
                }
                _ => {
                    for name in bindings.input.keys().filter(|n| n.as_str() != "base") {
                        value.insert(name.clone(), get(name)?);
                    }
                }
            }
            put("result", Value::Object(value))?;
        }
    }
    Ok(result)
}

fn decode(text: &str) -> Result<Value, KernelError> {
    source::load_json(text, source::MAX_MODEL_DEPTH).map_err(|(s, _)| KernelError::Argument(s))
}
fn model<T: serde::de::DeserializeOwned>(text: &str) -> Result<T, KernelError> {
    // The outer bounded pass also rejects duplicate keys in typed wrappers.
    decode(text)?;
    serde_json::from_str(text).map_err(|e| KernelError::Argument(e.to_string()))
}
fn encode(v: &impl Serialize) -> Result<String, KernelError> {
    serde_json::to_string(v).map_err(|e| engine(e.to_string()))
}
fn env(text: &str) -> Result<Environment, KernelError> {
    match decode(text)? {
        Value::Object(env) => Ok(env),
        _ => Err(KernelError::Argument(
            "environment must be an object".into(),
        )),
    }
}
fn symbols(text: &str) -> Result<Symbols, KernelError> {
    env(text)?
        .iter()
        .map(|(k, v)| {
            Ok((
                k.clone(),
                TypeSpec::parse(v).map_err(KernelError::Argument)?,
            ))
        })
        .collect()
}

pub fn eval_arg_json(arg: &str, environment: &str) -> Result<String, KernelError> {
    encode(&eval_arg(&model(arg)?, &env(environment)?)?)
}
pub fn eval_builtin_json(
    op: &str,
    bindings: &str,
    environment: &str,
    types: &str,
) -> Result<String, KernelError> {
    let op: Builtin = serde_json::from_value(Value::String(op.into()))
        .map_err(|e| KernelError::Argument(e.to_string()))?;
    encode(&eval_builtin(
        &op,
        &model(bindings)?,
        &env(environment)?,
        &symbols(types)?,
    )?)
}
pub fn validate_run_contract_json(
    response: &str,
    schema: &str,
    branches: &str,
) -> Result<String, KernelError> {
    let schema = symbols(schema)?;
    let branches = model(branches)?;
    let response = decode(response).map_err(|e| KernelError::Contract(e.to_string()))?;
    encode(&validate_run_contract(&response, &schema, &branches)?)
}
pub fn validate_input_json(input: &str, schema: &str) -> Result<String, KernelError> {
    encode(&validate_input(&decode(input)?, &symbols(schema)?)?)
}

/// Validate the actual host entry separately from compilation of reusable definitions.
pub fn validate_root_interface_json(input: &str, output: &str) -> Result<String, KernelError> {
    if symbols(input)?
        .values()
        .chain(symbols(output)?.values())
        .any(TypeSpec::contains_handle)
    {
        return Err(KernelError::Input(
            "root input and output must contain only pure values".into(),
        ));
    }
    Ok("null".into())
}

pub fn literal_equal_json(left: &str, right: &str) -> Result<bool, KernelError> {
    Ok(literal_eq(&decode(left)?, &decode(right)?))
}
