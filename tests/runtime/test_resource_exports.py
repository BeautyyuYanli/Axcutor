"""Resource kinds identify public package exports, including direct file definitions."""

import importlib
import sys
from uuid import uuid4

import pytest
from axcutor_runtime.resources import ResourceContext, ResourceStore


@pytest.fixture
def installed(tmp_path, monkeypatch):
    name = "ax_exports_" + uuid4().hex
    root = tmp_path / name
    root.mkdir()
    (root / "__init__.py").write_text("")
    monkeypatch.syspath_prepend(str(tmp_path))
    yield name, root
    for key in list(sys.modules):
        if key == name or key.startswith(name + "."):
            del sys.modules[key]
    importlib.invalidate_caches()


@pytest.mark.parametrize("module", [None, "tx", "_tx"])
async def test_public_package_exports_resolve_to_one_canonical_kind(installed, module):
    name, root = installed
    source = "from axcutor_runtime import resource\n@resource\nclass Tx:\n    pass\n"
    if module:
        (root / f"{module}.py").write_text(source)
        (root / "__init__.py").write_text(f"from .{module} import Tx\n")
    else:
        (root / "__init__.py").write_text(source)
    # Runtime loading must not consult the static header, even when it is invalid.
    (root / "__ax_resources__.yaml").write_text("invalid: [\n")
    cls = importlib.import_module(name).Tx
    physical = ResourceStore("pool-worker-" + uuid4().hex, {"infra": name})
    assert physical.resolve_resource_class("infra:Tx") is cls
    context = ResourceContext(physical, [])
    value = cls()
    handle = await context.put(value)
    assert handle.kind == "infra:Tx"
    assert await context.get(handle) is value
    await context.close(set())
    assert not physical.entries
    with pytest.raises(LookupError):
        ResourceStore("pool", {}).resolve_resource_class("infra:Tx")


@pytest.mark.parametrize("layout", ["child_package", "sibling_package", "alias", "unexported"])
async def test_resource_exports_reject_cross_package_aliases_and_unexported_classes(
    installed, layout
):
    name, root = installed
    source = "from axcutor_runtime import resource\n@resource\nclass Tx:\n    pass\n"
    if layout in ("child_package", "sibling_package"):
        other = root / "other"
        other.mkdir()
        (other / "__init__.py").write_text(source)
        (root / "__init__.py").write_text("from .other import Tx\n")
        if layout == "sibling_package":
            consumer = root / "consumer"
            consumer.mkdir()
            (consumer / "__init__.py").write_text("from ..other import Tx\n")
    else:
        (root / "_tx.py").write_text(source)
        if layout == "alias":
            (root / "__init__.py").write_text("from ._tx import Tx as Alias\n")
    physical = ResourceStore("pool", {"infra": name})
    kind = {
        "child_package": "infra:Tx",
        "sibling_package": "infra.consumer:Tx",
        "alias": "infra:Alias",
        "unexported": "infra:Tx",
    }[layout]
    with pytest.raises((TypeError, AttributeError)):
        physical.resolve_resource_class(kind)
    if layout == "unexported":
        cls = importlib.import_module(name + "._tx").Tx
        with pytest.raises(AttributeError):
            await ResourceContext(physical, []).put(cls())


def test_resource_kind_cannot_address_a_single_file_module(installed):
    name, root = installed
    (root / "tx.py").write_text(
        "from axcutor_runtime import resource\n@resource\nclass Tx:\n    pass\n"
    )
    with pytest.raises(ImportError, match="layout"):
        ResourceStore("pool", {"infra": name}).resolve_resource_class("infra.tx:Tx")
