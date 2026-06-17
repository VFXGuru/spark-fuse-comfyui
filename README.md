# Spark Fuse ComfyUI

Flux-only Docker image that runs [ComfyUI](https://github.com/comfyanonymous/ComfyUI)
headlessly on [Spark Fuse](https://sparkcloud.studio) GPU nodes. One container = one
workflow render: the job submits an API-format workflow, the container renders it,
and the results come back through ShareSync.

Jobs are submitted with the companion client,
[spark-fuse-messenger](https://github.com/VFXGuru/spark-fuse-messenger).

## How it works

```
Spark Fuse GPU host (e.g. NVIDIA L4 / g6.xlarge)
└── this image (one job per container)
    ├── /input   ← read-only, staged from ShareSync
    │     ├── workflow.json          (ComfyUI "Save (API Format)" export)
    │     └── models/checkpoints/…   (e.g. flux1-dev-fp8.safetensors)
    ├── runner   (PID 1 — runner/spark_fuse_run.py)
    │     1. spawn ComfyUI on 127.0.0.1:8188 (loopback only, no ports published)
    │     2. wait for /system_stats
    │     3. POST workflow → /prompt
    │     4. poll /history/{prompt_id} until done
    │     5. verify + log results, exit 0 / non-zero
    └── /output  ← ComfyUI writes images here → persisted to ShareSync
```

ComfyUI writes directly into `/output` (`--output-directory`), so there is no
copy step. All ComfyUI logs and runner progress go to stdout, which the
messenger streams live (`spark-fuse logs <job-id>`).

## Job contract

| Path | Meaning |
| --- | --- |
| `/input/workflow.json` | **API-format** workflow JSON (UI-format exports are rejected with a clear error) |
| `/input/models/<kind>/…` | models, by the usual ComfyUI folder names (`checkpoints/`, `loras/`, `vae/`, `controlnet/`, …) |
| `/output/` | rendered results; anything written here returns via ShareSync |

Runner exit codes: `0` success · `1` workflow execution error · `2` bad input ·
`3` workflow rejected at submission · `4` job timeout · `5` completed but no
output files · `6` ComfyUI failed to start or died.

Tunables (env vars): `STARTUP_TIMEOUT_SECONDS` (default 300),
`JOB_TIMEOUT_SECONDS` (default 0 = unlimited), `COMFY_PORT` (default 8188).

## Models are not in the image

Models live on ShareSync and arrive per job under `/input/models/`. At startup the
runner generates ComfyUI's `extra_model_paths.yaml` from the `MODEL_BASE_DIR`
environment variable, which defaults to `/input/models`. This makes the model
location a submit-time choice: when Spark Fuse's persistent assets mount is
available, set `MODEL_BASE_DIR=/assets/models` and the same image reads models from
there, with no rebuild and no workflow changes.

## Custom nodes

The image carries a pinned Flux node set matching the maintainer's desktop
install — see [nodes.json](nodes.json). Registry nodes are installed from the
[Comfy Registry](https://registry.comfy.org) at exact versions; the two
git-only nodes are fetched at exact commits. ComfyUI-Manager is deliberately
removed (it must not self-update inside a batch container).

## Build & publish

GitHub Actions ([build.yml](.github/workflows/build.yml)) builds `linux/amd64`
on every push to `main` and publishes to GHCR as
`ghcr.io/vfxguru/spark-fuse-comfyui` with `latest` and `sha-<commit>` tags.
The GHCR package must be **public** (Spark Fuse pulls anonymously) — flipped
once in the package settings after the first push.

## Local test run

With Docker + an NVIDIA GPU, the same image can be tested locally:

```powershell
docker run --rm --gpus all `
  -v "C:\path\to\job\input:/input:ro" `
  -v "C:\path\to\job\output:/output" `
  ghcr.io/vfxguru/spark-fuse-comfyui:latest
```

where `input\` contains `workflow.json` and `models\checkpoints\…`.

## License

[MIT](LICENSE) — Copyright (c) 2026 VFXGuru
