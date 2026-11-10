"""Directory snapshots enforce package boundaries without importing Python."""

from pathlib import Path

import pytest
from axcutor_runtime.bundle import CompilationError, compile_bundle
from axcutor_runtime.codec import loads
from axcutor_runtime.packages import (
    PackageMount,
    load_mounts,
    package_path,
    snapshot_mounts,
    snapshot_packages,
    snapshot_resources,
)


def package(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "__init__.py").write_text("raise AssertionError('must not import')\n")
    return path


def test_snapshot_includes_resource_only_roots_and_colocated_headers(tmp_path):
    root = package(tmp_path / "physical")
    resource = package(root / "pool")
    header = "resources: {Tx: {description: transaction}, Conn: null}\n"
    (resource / "__ax_resources__.yaml").write_text(header)
    document = (
        "input: {tx: {type: handle, kind: 'infra.pool:Tx'}}\noutput: {}\nrun: {executor: signal}\n"
    )
    (resource / "__axcutor__.yaml").write_text(document)
    mount = PackageMount("infra", root, ("signal",))
    snapshot = snapshot_mounts([mount])
    assert snapshot["infra"] == {
        "packages": snapshot_packages([mount]),
        "resources": {"infra.pool": {"format": "yaml", "text": header}},
        "allowed_executors": ["signal"],
    }
    assert snapshot["infra"]["resources"] == snapshot_resources([mount])
    assert load_mounts(resource, [mount]) == ("infra.pool", snapshot)
    assert loads(compile_bundle("infra.pool", snapshot))["entry"] == "infra.pool"
    # The compiler sees an immutable snapshot even after files are removed.
    (resource / "__ax_resources__.yaml").unlink()
    assert loads(compile_bundle("infra.pool", snapshot))["entry"] == "infra.pool"
    with pytest.raises(CompilationError, match="type.unknown_resource"):
        compile_bundle("infra.pool", snapshot_mounts([mount]))
    (resource / "__axcutor__.yaml").unlink()
    (resource / "__ax_resources__.yaml").write_text(header)
    assert snapshot_mounts([mount])["infra"]["packages"] == {}
    anonymous = tmp_path / "entry.json"
    anonymous.write_text('{"input":{},"output":{},"run":{"executor":"signal"}}')
    entry, combined = load_mounts(anonymous, [mount])
    assert entry == "__main__" and combined["infra"]["resources"]
    assert loads(compile_bundle(entry, combined))["entry"] == "__main__"


@pytest.mark.parametrize("filename", ["__axcutor__.yaml", "__ax_resources__.yaml"])
@pytest.mark.parametrize(
    "invalid", ["initializer", "document", "ancestor", "module", "reserved", "dotted", "collision"]
)
def test_both_header_kinds_reject_filesystem_aliases(tmp_path, filename, invalid):
    root = package(tmp_path / "physical")
    child_name = "__ax_resources__" if invalid == "reserved" else "child"
    if invalid in ("dotted", "collision"):
        child_name = "group.step"
    child = package(root / child_name)
    document = child / filename
    document.write_text("resources: {Tx: null}\n")
    outside = tmp_path / "outside.yaml"
    outside.write_text("resources: {Tx: null}\n")
    if invalid == "initializer":
        initializer = child / "__init__.py"
        initializer.unlink()
        initializer.symlink_to(outside)
    elif invalid == "document":
        document.unlink()
        document.symlink_to(outside)
    elif invalid == "ancestor":
        (child / "__init__.py").unlink()
    elif invalid == "module":
        (root / "child.py").write_text("")
    elif invalid == "collision":
        group = package(root / "group")
        step = package(group / "step")
        (step / filename).write_text("resources: {Other: null}\n")
    match = "invalid declaration package directory" if invalid in ("dotted", "collision") else None
    with pytest.raises(ValueError, match=match):
        snapshot_mounts([PackageMount("infra", root)])


def test_resources_reject_duplicate_mounts_and_reserved_segments(tmp_path):
    root = package(tmp_path / "physical")
    mount = PackageMount("infra", root)
    for mounts in ([mount, mount], [mount, PackageMount("other", root)]):
        with pytest.raises(ValueError, match="conflicting"):
            snapshot_resources(mounts)
    for name in ("infra.__ax_resources__", "infra.__axcutor__", "__ax_resources__"):
        assert not package_path(name)
