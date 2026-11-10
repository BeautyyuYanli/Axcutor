"""Shared source documents, always compiled by Rust before real execution."""


def source_mounts(packages):
    mounts = {}
    for name, source in packages.items():
        mounts.setdefault(name.split(".")[0], {"packages": {}})["packages"][name] = source
    return mounts


INTEGER = {"type": "integer"}
N = {"type": "number"}
B = {"type": "boolean"}
S = {"type": "string"}


def array(items=INTEGER):
    return {"type": "array", "items": items}


def mapping(values=INTEGER):
    return {"type": "object", "additionalProperties": values}


def literal(value):
    return {"value": value}


def builtin(op, inputs, outputs=None, **modifiers):
    bindings = {"input": inputs}
    if outputs is not None:
        bindings["output"] = outputs
    return {"use": f"builtin:{op}", "with": bindings, **modifiers}


def assign(source, target):
    return builtin("assign", {"source": source}, {"value": target})


def action(run, inputs=None, outputs=None, **modifiers):
    return {
        "run": {"executor": "builtin", "content": run},
        "input": inputs or [],
        "output": outputs or {},
        **modifiers,
    }


def graph(
    nodes, edges, *, inputs=None, outputs=None, variables=None, max_steps=1000, branches=None
):
    document = {
        "input": inputs or {},
        "output": outputs or {},
        "graph": {
            "variables": variables or {},
            "nodes": nodes,
            "edges": edges,
            "max_steps": max_steps,
        },
    }
    if branches is not None:
        document["branches"] = branches
    return document


def inline(document, captures=None, **modifiers):
    return {**document, "input": captures or [], **modifiers}


def call(name, inputs=None, outputs=None, **modifiers):
    return {
        "use": f"workflow:{name}",
        "with": {"input": inputs or {}, "output": outputs or {}},
        **modifiers,
    }
