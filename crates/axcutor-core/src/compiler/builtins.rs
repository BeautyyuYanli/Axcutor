//! Sixteen static builtin contracts. This module never evaluates an operator.
//! Constants are checked against their consumer; ambiguous literals never acquire
//! an `any` type. Only non-null scalar literals have an intrinsic narrow type.
use super::{check::*, *};

struct Checker<'a> {
    bindings: &'a Bindings,
    symbols: &'a Symbols,
    address: &'a Address,
    diagnostics: &'a mut Vec<Diagnostic>,
}

impl Checker<'_> {
    fn error(&mut self, param: &str, message: impl Into<String>) {
        self.address.at("with").at("input").at(param).error(
            self.diagnostics,
            "node",
            "builtin.type",
            message,
        );
    }
    fn keys(&mut self, required: &[&str], optional: &[&str], outputs: &[&str]) {
        for p in required {
            if !self.bindings.input.contains_key(*p) {
                self.address.at("with").at("input").at(p).error(
                    self.diagnostics,
                    "node",
                    "builtin.signature",
                    format!("missing required parameter {p}"),
                );
            }
        }
        for p in self.bindings.input.keys() {
            if !required.contains(&p.as_str()) && !optional.contains(&p.as_str()) {
                self.address.at("with").at("input").at(p).error(
                    self.diagnostics,
                    "node",
                    "builtin.signature",
                    format!("unknown parameter {p}"),
                );
            }
        }
        for p in outputs {
            if !self.bindings.output.contains_key(*p) {
                self.address.at("with").at("output").at(p).error(
                    self.diagnostics,
                    "node",
                    "builtin.signature",
                    format!("missing required output {p}"),
                );
            }
        }
        for p in self.bindings.output.keys() {
            if !outputs.contains(&p.as_str()) {
                self.address.at("with").at("output").at(p).error(
                    self.diagnostics,
                    "node",
                    "builtin.signature",
                    format!("unknown output {p}"),
                );
            }
        }
    }
    fn target(&self, field: &str) -> Option<(String, TypeSpec)> {
        let name = self.bindings.output.get(field)?;
        self.symbols.get(name).map(|t| (name.clone(), t.clone()))
    }
    fn arg(&mut self, p: &str, expected: &TypeSpec) {
        if let Some(arg) = self.bindings.input.get(p) {
            argument(
                arg,
                expected,
                self.symbols,
                &self.address.at("with").at("input").at(p),
                self.diagnostics,
            );
        }
    }
    fn output(&mut self, p: &str, t: &TypeSpec) {
        if let Some(n) = self.bindings.output.get(p) {
            if !self.symbols.contains_key(n) {
                return;
            }
            produced(
                t,
                n,
                self.symbols,
                &self.address.at("with").at("output").at(p),
                self.diagnostics,
            );
        }
    }
    fn intrinsic(&mut self, p: &str) -> Option<TypeSpec> {
        let arg = self.bindings.input.get(p)?;
        let t = arg_type(arg, self.symbols);
        if t.is_none() && matches!(arg, ArgValue::Const { .. }) {
            self.error(p, "this input needs a typed reference or a scalar literal");
        }
        t
    }
    /// Container literals get their element/value contract from the output target.
    /// References retain their actual type, including enums and nullability.
    fn contextual(&mut self, p: &str, expected: Option<&TypeSpec>) -> Option<TypeSpec> {
        let arg = self.bindings.input.get(p)?;
        match arg {
            ArgValue::Path { path } => type_of(self.symbols, path).ok(),
            ArgValue::Const { .. } => {
                if let Some(t) = expected {
                    self.arg(p, t);
                    Some(t.clone())
                } else {
                    self.intrinsic(p)
                }
            }
        }
    }
    fn op(&mut self, values: &[&str]) -> Option<String> {
        match self.bindings.input.get("op") {
            Some(ArgValue::Const {
                value: Value::String(s),
            }) if values.contains(&s.as_str()) => Some(s.clone()),
            Some(_) => {
                self.error(
                    "op",
                    format!("op must be a wrapped string literal in {values:?}"),
                );
                None
            }
            None => None,
        }
    }
    fn distinct(&mut self, out: &str, params: &[&str]) {
        let Some(name) = self.bindings.output.get(out) else {
            return;
        };
        for p in params {
            if let Some(ArgValue::Path { path }) = self.bindings.input.get(*p) {
                if &path.root == name {
                    self.address.at("with").at("output").at(out).error(
                        self.diagnostics,
                        "variable",
                        "builtin.alias",
                        format!("output {name} must differ from the root of {p}"),
                    );
                }
            }
        }
    }
    fn normal(&self) -> Vec<NodePort> {
        vec![port(
            None,
            self.bindings.output.values().cloned().collect(),
            vec![],
        )]
    }
    fn optional(&self, out: &str, none: &str, extra: Vec<String>) -> Vec<NodePort> {
        let output = self
            .bindings
            .output
            .get(out)
            .cloned()
            .into_iter()
            .collect::<Vec<_>>();
        let mut some = output.clone();
        some.extend(extra);
        vec![
            port(Some("some"), some, vec![]),
            port(Some(none), vec![], output),
        ]
    }
}

fn port(branch: Option<&str>, output: Vec<String>, kill_vars: Vec<String>) -> NodePort {
    NodePort {
        branch_id: branch.map(str::to_owned),
        kind: PortKind::Normal,
        output,
        kill_vars,
    }
}
fn boolean_ports() -> Vec<NodePort> {
    vec![
        port(Some("true"), vec![], vec![]),
        port(Some("false"), vec![], vec![]),
    ]
}
fn numeric(t: &TypeSpec) -> bool {
    !t.nullable && matches!(t.base(), "integer" | "number")
}

pub(super) fn check(
    op: &Builtin,
    bindings: &Bindings,
    symbols: &Symbols,
    a: &Address,
    ds: &mut Vec<Diagnostic>,
) -> Vec<NodePort> {
    let mut c = Checker {
        bindings,
        symbols,
        address: a,
        diagnostics: ds,
    };
    for (p, n) in &bindings.output {
        if !symbols.contains_key(n) {
            a.at("with").at("output").at(p).error(
                c.diagnostics,
                "variable",
                "symbol.undeclared_output",
                format!("output variable {n} needs an explicit TypeSpec declaration"),
            );
        }
    }
    match op {
        Builtin::Assign => {
            c.keys(&["source"], &[], &["value"]);
            if let Some((_, t)) = c.target("value") {
                c.arg("source", &t);
            }
            c.normal()
        }
        Builtin::Unwrap => {
            c.keys(&["source"], &["default"], &["value"]);
            if let Some((_, target)) = c.target("value") {
                if target.nullable {
                    c.error("source", "unwrap output must be non-null");
                }
                if let Some(arg) = bindings.input.get("source") {
                    match arg {
                        ArgValue::Path { path } => {
                            if let Ok(src) = type_of(symbols, path) {
                                if !src.nullable {
                                    c.error("source", "unwrap requires a nullable source");
                                }
                                c.output("value", &src.strip_null());
                            }
                        }
                        ArgValue::Const { value } if value.is_null() => (),
                        ArgValue::Const { .. } => {
                            c.error("source", "unwrap cannot narrow a non-null literal")
                        }
                    }
                }
                c.arg("default", &target);
                if matches!(
                    bindings.input.get("default"),
                    Some(ArgValue::Const { value: Value::Null })
                ) {
                    c.error("default", "unwrap default cannot be null");
                }
            }
            c.distinct("value", &["source"]);
            if bindings.input.contains_key("default") {
                c.normal()
            } else {
                c.optional("value", "none", vec![])
            }
        }
        Builtin::Switch => {
            c.keys(&["source"], &[], &[]);
            if bindings.output_present {
                a.at("with").at("output").error(
                    c.diagnostics,
                    "node",
                    "builtin.signature",
                    "switch forbids with.output",
                );
            }
            if let Some(t) = c.intrinsic("source") {
                if !t.nullable {
                    match &t.kind {
                        TypeKind::Scalar {
                            base: Scalar::Boolean,
                            ..
                        } => return boolean_ports(),
                        TypeKind::Scalar {
                            base: Scalar::String,
                            enumeration: Some(values),
                        } => {
                            let mut ports = vec![];
                            for value in values {
                                let b = value.as_str().unwrap();
                                if !control_id(b) {
                                    c.error(
                                        "source",
                                        format!("enum value {b} is not a branch identifier"),
                                    );
                                } else {
                                    ports.push(port(Some(b), vec![], vec![]));
                                }
                            }
                            return ports;
                        }
                        _ => (),
                    }
                }
                c.error("source", "switch needs a non-null boolean or string enum");
            }
            vec![]
        }
        Builtin::Compare => {
            let outputs = if bindings.output_present {
                vec!["result"]
            } else {
                vec![]
            };
            c.keys(&["op", "left", "right"], &[], &outputs);
            if let Some(op) = c.op(&["eq", "ne", "lt", "lte", "gt", "gte"]) {
                if matches!(op.as_str(), "eq" | "ne") {
                    equality(&mut c);
                } else {
                    let l = c.intrinsic("left");
                    let r = c.intrinsic("right");
                    if let (Some(l), Some(r)) = (l, r) {
                        if !(numeric(&l) && numeric(&r)
                            || (!l.nullable
                                && !r.nullable
                                && l.base() == "string"
                                && r.base() == "string"))
                        {
                            c.error("left","ordered comparison needs two non-null numeric operands or two strings");
                        }
                    }
                }
            }
            c.output("result", &TypeSpec::scalar(Scalar::Boolean));
            if bindings.output_present {
                c.normal()
            } else {
                boolean_ports()
            }
        }
        Builtin::Logic => {
            let op = c.op(&["and", "or", "not"]);
            let inputs = if op.as_deref() == Some("not") {
                vec!["op", "source"]
            } else {
                vec!["op", "left", "right"]
            };
            let outputs = if bindings.output_present {
                vec!["result"]
            } else {
                vec![]
            };
            c.keys(&inputs, &[], &outputs);
            for p in &inputs[1..] {
                c.arg(p, &TypeSpec::scalar(Scalar::Boolean));
            }
            c.output("result", &TypeSpec::scalar(Scalar::Boolean));
            if bindings.output_present {
                c.normal()
            } else {
                boolean_ports()
            }
        }
        Builtin::Math => {
            let op = c.op(&["add", "sub", "mul", "div", "mod"]);
            let partial = matches!(op.as_deref(), Some("div" | "mod"));
            c.keys(
                &["op", "left", "right"],
                if partial { &["default"] } else { &[] },
                &["result"],
            );
            let l = c.intrinsic("left");
            let r = c.intrinsic("right");
            if let (Some(l), Some(r)) = (l, r) {
                if !numeric(&l)
                    || !numeric(&r)
                    || (op.as_deref() == Some("mod")
                        && (l.base() != "integer" || r.base() != "integer"))
                {
                    c.error("left", "math needs non-null numbers; mod requires integers");
                }
                let base = if op.as_deref() != Some("div")
                    && l.base() == "integer"
                    && r.base() == "integer"
                {
                    Scalar::Integer
                } else {
                    Scalar::Number
                };
                c.output("result", &TypeSpec::scalar(base));
            }
            if let Some((_, target)) = c.target("result") {
                c.arg("default", &target);
            }
            if partial && !bindings.input.contains_key("default") {
                c.distinct("result", &["left", "right"]);
                c.optional("result", "none", vec![])
            } else {
                c.normal()
            }
        }
        Builtin::Length => {
            c.keys(&["source"], &[], &["result"]);
            // Length inspects only the container boundary. It does not manufacture
            // element types for literals whose elements are never consumed.
            match bindings.input.get("source") {
                Some(ArgValue::Const { value })
                    if value.is_string() || value.is_array() || value.is_object() => {}
                Some(ArgValue::Const { .. }) => {
                    c.error("source", "length requires a string, array or OrderedMap")
                }
                _ => {
                    if let Some(t) = c.intrinsic("source") {
                        if t.nullable
                            || !(t.base() == "string"
                                || matches!(t.kind, TypeKind::Array(_) | TypeKind::OrderedMap(_)))
                        {
                            c.error(
                                "source",
                                "length requires a non-null string, array or OrderedMap",
                            );
                        }
                    }
                }
            }
            c.output("result", &TypeSpec::scalar(Scalar::Integer));
            c.normal()
        }
        Builtin::ArrayGet | Builtin::MapGet => {
            let array = matches!(op, Builtin::ArrayGet);
            let key = if array { "index" } else { "key" };
            c.keys(&["source", key], &["default"], &["value"]);
            let target = c.target("value").map(|(_, t)| t);
            let context = target.as_ref().map(|t| {
                if array {
                    TypeSpec::array(t.clone())
                } else {
                    TypeSpec::map(t.clone())
                }
            });
            if let Some(src) = c.contextual("source", context.as_ref()) {
                let elem = match (&src.kind, array) {
                    (TypeKind::Array(t), true) | (TypeKind::OrderedMap(t), false)
                        if !src.nullable =>
                    {
                        Some(t.as_ref())
                    }
                    _ => None,
                };
                if let Some(t) = elem {
                    c.output("value", t);
                } else {
                    c.error(
                        "source",
                        format!(
                            "source must be a non-null {}",
                            if array { "array" } else { "OrderedMap" }
                        ),
                    );
                }
            }
            c.arg(
                key,
                &TypeSpec::scalar(if array {
                    Scalar::Integer
                } else {
                    Scalar::String
                }),
            );
            if let Some(t) = target {
                c.arg("default", &t);
            }
            if !array || !bindings.input.contains_key("default") {
                c.distinct("value", &["source"]);
            }
            if array && !bindings.input.contains_key("default") {
                c.distinct("value", &["index"]);
            }
            if bindings.input.contains_key("default") {
                c.normal()
            } else {
                c.optional("value", "none", vec![])
            }
        }
        Builtin::ArrayShift => {
            c.keys(&["source"], &[], &["item", "rest"]);
            if matches!(bindings.input.get("source"), Some(ArgValue::Const { .. })) {
                // The two outputs may widen differently. Check a literal against
                // both consumers instead of choosing either target as its type.
                if let Some((_, item)) = c.target("item") {
                    c.arg("source", &TypeSpec::array(item));
                }
                if let Some((_, rest)) = c.target("rest") {
                    c.arg("source", &rest.strip_null());
                }
            } else if let Some(src) = c.intrinsic("source") {
                if let TypeKind::Array(t) = &src.kind {
                    if src.nullable {
                        c.error("source", "array_shift requires a non-null array");
                    }
                    c.output("item", t);
                    c.output("rest", &src);
                } else {
                    c.error("source", "array_shift requires an array");
                }
            }
            c.optional(
                "item",
                "empty",
                bindings.output.get("rest").cloned().into_iter().collect(),
            )
        }
        Builtin::ArrayAppend | Builtin::ArrayConcat | Builtin::MapSet => {
            let map = matches!(op, Builtin::MapSet);
            let concat = matches!(op, Builtin::ArrayConcat);
            c.keys(
                if map {
                    &["source", "key", "value"]
                } else if concat {
                    &["left", "right"]
                } else {
                    &["source", "item"]
                },
                &[],
                &["result"],
            );
            if let Some((_, t)) = c.target("result") {
                let inner = match (&t.kind, map) {
                    (TypeKind::Array(e), false) | (TypeKind::OrderedMap(e), true)
                        if !t.nullable =>
                    {
                        Some(e.as_ref())
                    }
                    _ => None,
                };
                if let Some(inner) = inner {
                    if concat {
                        c.arg("left", &t);
                        c.arg("right", &t);
                    } else {
                        c.arg("source", &t);
                        c.arg(if map { "value" } else { "item" }, inner);
                    }
                } else {
                    c.error(
                        "source",
                        "target must be a non-null container of the operator's kind",
                    );
                }
            }
            if map {
                c.arg("key", &TypeSpec::scalar(Scalar::String));
            }
            c.normal()
        }
        Builtin::ArrayFlatten => {
            c.keys(&["source"], &[], &["result"]);
            let context = c.target("result").and_then(|(_, t)| match t.kind {
                TypeKind::Array(inner) => Some(TypeSpec::array(TypeSpec::array(*inner))),
                _ => None,
            });
            if let Some(src) = c.contextual("source", context.as_ref()) {
                if let TypeKind::Array(inner) = &src.kind {
                    if let TypeKind::Array(elem) = &inner.kind {
                        if src.nullable || inner.nullable {
                            c.error("source", "both array layers must be non-null");
                        }
                        c.output("result", &TypeSpec::array(elem.as_ref().clone()));
                    } else {
                        c.error("source", "flatten requires an array of arrays");
                    }
                } else {
                    c.error("source", "flatten requires an array of arrays");
                }
            }
            c.normal()
        }
        Builtin::MapDelete => {
            c.keys(&["source", "key"], &[], &["result"]);
            let context = c.target("result").map(|(_, t)| t.strip_null());
            if let Some(src) = c.contextual("source", context.as_ref()) {
                if src.nullable || !matches!(src.kind, TypeKind::OrderedMap(_)) {
                    c.error("source", "map_delete requires a non-null OrderedMap");
                }
                c.output("result", &src);
            }
            c.arg("key", &TypeSpec::scalar(Scalar::String));
            c.normal()
        }
        Builtin::ObjectMerge => {
            object_merge(&mut c);
            c.normal()
        }
    }
}

fn equality(c: &mut Checker<'_>) {
    let (Some(l), Some(r)) = (c.bindings.input.get("left"), c.bindings.input.get("right")) else {
        return;
    };
    let compatible = match (l, r) {
        (ArgValue::Path { path: l }, ArgValue::Path { path: r }) => {
            let (Ok(l), Ok(r)) = (type_of(c.symbols, l), type_of(c.symbols, r)) else {
                return;
            };
            l.strip_null().is_subtype_of(&r.strip_null())
                || r.strip_null().is_subtype_of(&l.strip_null())
        }
        (ArgValue::Path { path }, ArgValue::Const { value })
        | (ArgValue::Const { value }, ArgValue::Path { path }) => {
            let Ok(t) = type_of(c.symbols, path) else {
                return;
            };
            if value.is_null() {
                t.nullable
            } else {
                t.strip_null().accepts_literal(value)
                    || (matches!(t.base(), "integer" | "number") && value.is_number())
            }
        }
        (ArgValue::Const { value: Value::Null }, ArgValue::Const { value: Value::Null }) => true,
        (ArgValue::Const { value: l }, ArgValue::Const { value: r }) => {
            // Either literal may supply the scalar context for the other. Composite
            // equality needs a declared schema rather than guessed container types.
            let lt = arg_type(&ArgValue::Const { value: l.clone() }, c.symbols);
            let rt = arg_type(&ArgValue::Const { value: r.clone() }, c.symbols);
            match (lt, rt) {
                (Some(l), Some(r)) => l.is_subtype_of(&r) || r.is_subtype_of(&l),
                _ => false,
            }
        }
    };
    if !compatible {
        c.error(
            "left",
            "operands are not comparable under their declared types",
        );
    }
}

fn object_merge(c: &mut Checker<'_>) {
    // Field names are data-dependent parameters, but output shape is fixed.
    let fields: Vec<_> = c
        .bindings
        .input
        .keys()
        .filter(|k| k.as_str() != "base")
        .cloned()
        .collect();
    let allowed: Vec<_> = c.bindings.input.keys().map(String::as_str).collect();
    c.keys(&[], &allowed, &["result"]);
    if fields.is_empty() {
        c.error(
            "base",
            "object_merge requires at least one explicit field override",
        );
    }
    let Some((_, target)) = c.target("result") else {
        return;
    };
    let TypeKind::TypedDict {
        properties,
        required,
    } = &target.kind
    else {
        c.error("base", "object_merge target must be a TypedDict");
        return;
    };
    if target.nullable {
        c.error("base", "object_merge target must be non-null");
    }
    for field in &fields {
        if let Some(t) = properties.get(field) {
            c.arg(field, t);
        } else {
            c.error(
                field,
                "field is not declared by the closed target TypedDict",
            );
        }
    }
    let mut covered = fields.clone();
    if let Some(arg) = c.bindings.input.get("base") {
        match arg {
            ArgValue::Path { path } => {
                if let Ok(base) = type_of(c.symbols, path) {
                    if let TypeKind::TypedDict {
                        properties: bp,
                        required: br,
                    } = &base.kind
                    {
                        if base.nullable {
                            c.error("base", "base must be non-null");
                        }
                        covered.extend(br.clone());
                        for (name, t) in bp {
                            match properties.get(name) {
                                None => c.error(
                                    "base",
                                    format!("base contains undeclared property {name}"),
                                ),
                                Some(expected)
                                    if !fields.contains(name) && !t.is_subtype_of(expected) =>
                                {
                                    c.error(
                                        "base",
                                        format!("retained property {name} is incompatible"),
                                    )
                                }
                                _ => (),
                            }
                        }
                    } else {
                        c.error("base", "base must be a TypedDict");
                    }
                }
            }
            ArgValue::Const { value } => {
                if let Some(base) = value.as_object() {
                    covered.extend(base.keys().cloned());
                    for (name, value) in base {
                        match properties.get(name) {
                            None => {
                                c.error("base", format!("base contains undeclared property {name}"))
                            }
                            Some(expected)
                                if !fields.contains(name) && !expected.accepts_literal(value) =>
                            {
                                c.error("base", format!("retained property {name} is incompatible"))
                            }
                            _ => (),
                        }
                    }
                } else {
                    c.error("base", "base literal must be an object");
                }
            }
        }
    }
    if !required.iter().all(|name| covered.contains(name)) {
        c.error(
            "base",
            "base and explicit fields do not cover all required properties",
        );
    }
}
