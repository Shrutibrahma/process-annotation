"""Start the app: python run.py  (reads .env if present)."""
import os
from pathlib import Path

env = Path(__file__).with_name(".env")
if env.exists():
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

import uvicorn  # noqa: E402

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    if os.environ.get("WORKSTUDY_NO_BROWSER") != "1":
        import webbrowser, threading
        threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    # behind a tunnel or proxy: trust its X-Forwarded-Proto so login cookies are marked secure on https
    uvicorn.run("app.main:app", host="127.0.0.1", port=port, proxy_headers=True, forwarded_allow_ips="127.0.0.1")
