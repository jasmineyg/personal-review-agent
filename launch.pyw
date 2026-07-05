"""Start the retained desktop bridge UI in a browser.

The Tauri desktop app is still the primary desktop shell under
frontends/desktop/. This launcher keeps the old `ga desktop`/`python launch.pyw`
entry point useful without reviving the removed Streamlit and bot frontends.
"""
import atexit
import os
import subprocess
import sys
import time
import webbrowser


ROOT = os.path.dirname(os.path.abspath(__file__))
BRIDGE = os.path.join(ROOT, "frontends", "desktop_bridge.py")
HOST = os.environ.get("BRIDGE_HOST", "127.0.0.1")
PORT = os.environ.get("BRIDGE_PORT", "14168")
URL = f"http://{HOST}:{PORT}/"


def main() -> int:
    if not os.path.exists(BRIDGE):
        print(f"desktop_bridge.py not found: {BRIDGE}", file=sys.stderr)
        return 1
    proc = subprocess.Popen([sys.executable, BRIDGE], cwd=ROOT)
    atexit.register(proc.terminate)
    time.sleep(1.0)
    webbrowser.open(URL)
    print(f"Desktop bridge running at {URL}")
    try:
        return proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
