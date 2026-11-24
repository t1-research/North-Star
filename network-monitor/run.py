"""North Star Monitor — entry point.

    python run.py

Loads config.yaml and starts the FastAPI server with uvicorn.
Dashboard: http://<this-machine>:8080 (port comes from config.yaml).
"""
import uvicorn

from app.config import load_config

if __name__ == "__main__":
    cfg = load_config()
    print(f"North Star Monitor starting on http://{cfg['host']}:{cfg['port']}", flush=True)
    print("LAN-only — never port-forward this to the internet.", flush=True)
    uvicorn.run("app.main:app", host=cfg["host"], port=cfg["port"])
