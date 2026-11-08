"""Build browser bindings with the CLI version pinned by the Rust workspace.

Generated glue is ignored by Git. Run this after a clean checkout and after any
Rust compiler change; the generated declaration is the TypeScript import contract.
"""

import os
import shutil
import subprocess
import tomllib
from pathlib import Path

root = Path(__file__).resolve().parents[2]
manifest = tomllib.loads((root / "Cargo.toml").read_text())
version = manifest["workspace"]["dependencies"]["wasm-bindgen"].removeprefix("=")
cargo = os.environ.get("AXCUTOR_CARGO", "cargo")
bindgen = os.environ.get("AXCUTOR_WASM_BINDGEN", "wasm-bindgen")
if not shutil.which(bindgen):
    raise SystemExit(
        f"Install matching bindings: cargo install wasm-bindgen-cli --version {version} --locked"
    )
actual = subprocess.check_output([bindgen, "--version"], text=True).strip().split()[-1]
if actual != version:
    raise SystemExit(f"wasm-bindgen {version} required, found {actual}")
subprocess.run(
    [
        cargo,
        "build",
        "--locked",
        "--release",
        "-p",
        "axcutor-wasm",
        "--target",
        "wasm32-unknown-unknown",
    ],
    cwd=root,
    check=True,
)
# Cargo can place artifacts outside the checkout through CARGO_TARGET_DIR.
target = Path(os.environ.get("CARGO_TARGET_DIR", root / "target"))
if not target.is_absolute():
    target = root / target
subprocess.run(
    [
        bindgen,
        "--target",
        "web",
        "--out-dir",
        str(root / "playground/src/wasm-pkg"),
        str(target / "wasm32-unknown-unknown/release/axcutor_wasm.wasm"),
    ],
    cwd=root,
    check=True,
)
