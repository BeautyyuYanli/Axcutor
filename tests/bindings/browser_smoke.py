"""Generate temporary HTML from the template and execute real WASM in Chrome.

Embedding the generated glue, WASM and oracle keeps this check independent of
host networking. The HTML is generated; browser.html.in is the maintained source.
"""
import base64
from pathlib import Path
import os
import shutil
import signal
import subprocess
import tempfile

root = Path(__file__).resolve().parents[2]
chrome = os.environ.get("AXCUTOR_CHROME") or next((shutil.which(name) for name in ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"] if shutil.which(name)), None)
if chrome is None and Path("/opt/google/chrome/chrome").is_file():
    chrome = "/opt/google/chrome/chrome"
if chrome is None:
    raise SystemExit("Chrome/Chromium is required for the browser check")


def encoded(path):
    return base64.b64encode(path.read_bytes()).decode("ascii")


html = (root / "tests/bindings/browser.html.in").read_text()
html = html.replace("AXCUTOR_GLUE_URL", "data:text/javascript;base64," + encoded(root / "target/wasm-web/axcutor_wasm.js"))
html = html.replace("AXCUTOR_WASM_BASE64", encoded(root / "target/wasm-web/axcutor_wasm_bg.wasm"))
html = html.replace("AXCUTOR_ORACLE_BASE64", encoded(root / "target/bindings-oracle.json"))
with tempfile.TemporaryDirectory(prefix="axcutor-browser-") as directory:
    page = Path(directory) / "check.html"
    page.write_text(html)
    profile = Path(directory) / "profile"
    process = subprocess.Popen([
        chrome, "--headless", "--no-sandbox", "--disable-gpu",
        "--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check",
        f"--user-data-dir={profile}", "--dump-dom", "--timeout=10000", page.as_uri(),
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=30)
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
    if process.returncode != 0 or '<pre id="result">PASS:' not in stdout:
        raise AssertionError(stdout[:2000] + "\n" + stderr)
    print(stdout.split('<pre id="result">', 1)[1].split("</pre>", 1)[0])
