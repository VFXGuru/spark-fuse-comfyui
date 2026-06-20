#!/usr/bin/env python3
"""Spark Fuse job runner — drives one headless ComfyUI workflow per container.

Job contract:
  /input/workflow.json   ComfyUI API-format workflow (read-only mount)
  /input/models/...      model files (checkpoints/, loras/, vae/, ...)
  /output/               everything written here persists to ShareSync

The ComfyUI server only ever listens on container-internal loopback; Spark
Fuse publishes no ports and none are needed.

Exit codes:
  0  success — workflow completed, files written to /output
  1  workflow executed but ended in error
  2  bad input (workflow.json missing / unparsable / not API format, or
     /output not writable)
  3  ComfyUI rejected the workflow at submission
  4  JOB_TIMEOUT_SECONDS exceeded
  5  workflow completed but produced no files in /output
  6  ComfyUI server failed to start or died mid-run
"""
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

COMFYUI_DIR = Path(os.environ.get("COMFYUI_DIR", "/default-comfyui-bundle/ComfyUI"))
INPUT_DIR = Path(os.environ.get("INPUT_DIR", "/input"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "/output"))
WORKFLOW_PATH = Path(os.environ.get("WORKFLOW_PATH", str(INPUT_DIR / "workflow.json")))
PORT = int(os.environ.get("COMFY_PORT", "8188"))
BASE_URL = f"http://127.0.0.1:{PORT}"
STARTUP_TIMEOUT = int(os.environ.get("STARTUP_TIMEOUT_SECONDS", "300"))
JOB_TIMEOUT = int(os.environ.get("JOB_TIMEOUT_SECONDS", "0"))  # 0 = no limit
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL_SECONDS", "2"))
HEARTBEAT_EVERY = 60
# Render this many images in one job (one cold start + one model load amortised
# across all of them). Set per job via the BATCH_COUNT env var; clamped to 1..100.
try:
    BATCH_COUNT = max(1, min(int(os.environ.get("BATCH_COUNT", "1")), 100))
except ValueError:
    BATCH_COUNT = 1
# Inputs whose value is randomised between batch renders so each image differs.
SEED_KEYS = ("seed", "noise_seed")
# Where ComfyUI looks for models. Defaults to the per-job /input mount today;
# set MODEL_BASE_DIR=/assets/models to use Spark Fuse's persistent assets mount
# when it ships (one submit-time --env flag, no image rebuild, no workflow edits).
MODEL_BASE_DIR = os.environ.get("MODEL_BASE_DIR", str(INPUT_DIR / "models"))
MODEL_SUBDIRS = [
    "checkpoints", "diffusion_models", "unet", "text_encoders", "clip",
    "clip_vision", "vae", "loras", "controlnet", "upscale_models",
    "embeddings", "configs",
]
# Generated at runtime from MODEL_BASE_DIR (see write_extra_model_paths()).
EXTRA_MODEL_PATHS = Path("/tmp/spark_fuse_extra_model_paths.yaml")


def log(msg):
    print(f"[runner] {msg}", flush=True)


def die(code, msg):
    log(f"ERROR: {msg}")
    sys.exit(code)


def http_json(path, payload=None, timeout=30):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(BASE_URL + path, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    return json.loads(body) if body else {}


def log_banner():
    log(f"spark-fuse-comfyui runner (python {sys.version.split()[0]})")
    version_file = COMFYUI_DIR / "comfyui_version.py"
    if version_file.is_file():
        match = re.search(r'__version__\s*=\s*"([^"]+)"', version_file.read_text())
        if match:
            log(f"ComfyUI version: {match.group(1)}")
    try:
        smi = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,driver_version,memory.total",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=20,
        )
        if smi.returncode == 0:
            log(f"GPU: {smi.stdout.strip()}")
    except (OSError, subprocess.TimeoutExpired):
        log("nvidia-smi not available")


def load_workflow():
    if not WORKFLOW_PATH.is_file():
        die(2, f"workflow not found at {WORKFLOW_PATH}")
    try:
        workflow = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        die(2, f"cannot parse {WORKFLOW_PATH}: {err}")
    if not isinstance(workflow, dict):
        die(2, "workflow.json is not a JSON object")
    if "nodes" in workflow and "links" in workflow:
        die(2, "workflow.json is a UI-format export; re-export with "
               "'Save (API Format)' (enable dev mode options in ComfyUI settings)")
    bad = [k for k, v in workflow.items()
           if not (isinstance(v, dict) and "class_type" in v)]
    if bad:
        die(2, f"workflow.json does not look like API format "
               f"(offending keys: {bad[:5]})")
    log(f"workflow loaded: {len(workflow)} nodes")
    return workflow


def check_output_writable():
    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        probe = OUTPUT_DIR / ".write-probe"
        probe.write_text("")
        probe.unlink()
    except OSError as err:
        die(2, f"{OUTPUT_DIR} is not writable: {err}")


def write_extra_model_paths():
    """Generate ComfyUI's extra_model_paths.yaml from MODEL_BASE_DIR.

    Done at runtime, not baked into the image, so the model library location is
    a submit-time choice: /input/models by default, or /assets/models once the
    Spark Fuse assets mount is available, with no rebuild and no workflow edits.
    """
    lines = ["spark_fuse:", f"  base_path: {MODEL_BASE_DIR}"]
    lines += [f"  {name}: {name}" for name in MODEL_SUBDIRS]
    EXTRA_MODEL_PATHS.write_text("\n".join(lines) + "\n")
    log(f"model search base: {MODEL_BASE_DIR}")


def start_comfyui():
    cmd = [
        sys.executable, "main.py",
        "--listen", "127.0.0.1",
        "--port", str(PORT),
        "--extra-model-paths-config", str(EXTRA_MODEL_PATHS),
        "--output-directory", str(OUTPUT_DIR),
        "--input-directory", str(INPUT_DIR),
        "--temp-directory", "/tmp/comfyui",
    ]
    log(f"starting ComfyUI: {' '.join(cmd)}")
    # stdout/stderr inherited so ComfyUI's own logs reach the Spark Fuse stream
    return subprocess.Popen(cmd, cwd=COMFYUI_DIR)


def wait_ready(proc):
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            die(6, f"ComfyUI exited during startup (code {proc.returncode})")
        try:
            stats = http_json("/system_stats", timeout=5)
            names = ", ".join(d.get("name", "?") for d in stats.get("devices", []))
            log(f"ComfyUI ready (devices: {names or 'unknown'})")
            return
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            time.sleep(1)
    die(6, f"ComfyUI not ready after {STARTUP_TIMEOUT}s")


def submit(workflow):
    payload = {"prompt": workflow, "client_id": "spark-fuse-runner"}
    try:
        resp = http_json("/prompt", payload=payload, timeout=60)
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="replace")
        log(f"ComfyUI rejected the workflow (HTTP {err.code}):")
        print(body, flush=True)
        die(3, "workflow validation failed")
    prompt_id = resp.get("prompt_id")
    if not prompt_id:
        die(3, f"no prompt_id in /prompt response: {resp}")
    log(f"workflow queued, prompt_id={prompt_id}")
    return prompt_id


def wait_done(proc, prompt_id):
    start = time.monotonic()
    last_beat = start
    while True:
        elapsed = time.monotonic() - start
        if JOB_TIMEOUT and elapsed > JOB_TIMEOUT:
            die(4, f"job exceeded JOB_TIMEOUT_SECONDS={JOB_TIMEOUT}")
        if proc.poll() is not None:
            die(6, f"ComfyUI died mid-run (code {proc.returncode})")
        try:
            history = http_json(f"/history/{prompt_id}", timeout=15)
        except (urllib.error.URLError, OSError):
            history = {}
        entry = history.get(prompt_id)
        if entry:
            status = entry.get("status") or {}
            if status.get("status_str") == "error":
                for item in status.get("messages", []):
                    event = item[0]
                    data = item[1] if len(item) > 1 else {}
                    if event in ("execution_error", "execution_interrupted"):
                        log(f"{event}:")
                        print(json.dumps(data, indent=2), flush=True)
                die(1, "workflow execution failed")
            if status.get("completed"):
                log(f"workflow completed in {elapsed:.0f}s")
                return entry
        if time.monotonic() - last_beat >= HEARTBEAT_EVERY:
            log(f"still running ({elapsed:.0f}s elapsed)")
            last_beat = time.monotonic()
        time.sleep(POLL_INTERVAL)


def report_outputs():
    files = sorted(p for p in OUTPUT_DIR.rglob("*") if p.is_file())
    for f in files:
        log(f"output: {f.relative_to(OUTPUT_DIR)} ({f.stat().st_size:,} bytes)")
    if not files:
        die(5, "workflow completed but wrote nothing to /output — "
               "does the workflow contain a save node?")
    return files


def shutdown(proc):
    if proc.poll() is not None:
        return
    log("shutting down ComfyUI")
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()


def randomize_seeds(workflow):
    """Give each batch render a fresh seed so the images differ. ComfyUI caches
    node outputs, so an identical re-submit would return the same image; changing
    the seed re-runs only sampling onward, reusing the already-loaded model."""
    changed = 0
    for node in workflow.values():
        inputs = node.get("inputs") if isinstance(node, dict) else None
        if not isinstance(inputs, dict):
            continue
        for key in inputs:
            value = inputs[key]
            if key in SEED_KEYS and isinstance(value, int) and not isinstance(value, bool):
                inputs[key] = random.randint(0, 2**32 - 1)
                changed += 1
    return changed


def main():
    log_banner()
    workflow = load_workflow()
    check_output_writable()
    write_extra_model_paths()
    proc = start_comfyui()
    try:
        wait_ready(proc)
        if BATCH_COUNT > 1:
            log(f"batch render: {BATCH_COUNT} images in one job (model loads once)")
        for i in range(BATCH_COUNT):
            if i > 0:
                n = randomize_seeds(workflow)
                log(f"render {i + 1}/{BATCH_COUNT} (randomised {n} seed input(s))")
            elif BATCH_COUNT > 1:
                log(f"render 1/{BATCH_COUNT}")
            prompt_id = submit(workflow)
            wait_done(proc, prompt_id)
        files = report_outputs()
        log(f"job complete: {len(files)} file(s) in {OUTPUT_DIR}")
    finally:
        shutdown(proc)


if __name__ == "__main__":
    main()
