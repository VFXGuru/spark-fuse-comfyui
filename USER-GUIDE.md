# Spark Fuse ComfyUI: User Guide

This guide explains how to render a ComfyUI workflow on a Spark Fuse cloud GPU
using this image. It assumes no prior knowledge of the project. By the end you
will be able to stage your models once, submit a workflow, watch it run, and
download the result.

## What this is

This project lets you run heavy ComfyUI workflows (Flux and similar) on rented
cloud GPUs instead of your own machine. There are three pieces:

1. **The image** (this repository): a Docker image that runs ComfyUI headlessly,
   published at `ghcr.io/vfxguru/spark-fuse-comfyui`. You do not build it; Spark
   Fuse pulls it automatically.
2. **The messenger**: a small Python client and command line tool that talks to
   the Spark Fuse API. You use it to submit jobs, watch logs, and download
   results. See the companion repository,
   [spark-fuse-messenger](https://github.com/VFXGuru/spark-fuse-messenger).
3. **ShareSync**: Spark Fuse's cloud storage. Your models and workflow live here;
   results come back here.

Your local ComfyUI and the cloud ComfyUI never talk to each other directly. Your
local ComfyUI is only used to design and export a workflow. Everything is handed
back and forth as files on ShareSync.

## How a render flows

```
Your machine                         Spark Fuse cloud GPU
  author workflow in ComfyUI
  export it as API format  ──┐
                             ▼
                       ShareSync folder           pulled at job start
                       (workflow.json    ───────────────►  /input/
                        + models/)                          │
                             ▲                              ▼
  spark-fuse submit  ────────┘                    ComfyUI renders the workflow
  spark-fuse logs    ◄───── live log stream                 │
  spark-fuse download ◄──── results via ShareSync ◄──── /output/
```

## One-time setup

You only do this section once.

### 1. Spark Fuse account and credentials

You need a Spark Fuse account with API access (an email and password). These are
your alpha credentials from Spark Cloud Studio.

### 2. The messenger client

Clone and install the messenger, then add your credentials:

```powershell
git clone https://github.com/VFXGuru/spark-fuse-messenger
cd spark-fuse-messenger
uv sync
```

Copy `.env.example` to `.env` and fill in your details:

```
SPARK_HOST=https://api.prod.aapse1.sparkcloud.studio
SPARK_EMAIL=you@yourcompany.com
SPARK_PASSWORD=your-password
```

`.env` is never committed. Activate the environment in each new terminal:

```powershell
# PowerShell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
.\.venv\Scripts\Activate.ps1
```

Confirm it works:

```powershell
spark-fuse login
```

### 3. The Spark desktop app (for staging models)

Install the Spark desktop app from
[sparkcloud.studio/downloads](https://sparkcloud.studio/downloads). It mounts your
ShareSync storage as an ordinary folder in File Explorer (Windows) or Finder
(macOS), so you can copy files into it like any local folder. Anything you place
there syncs up to ShareSync and becomes available to your jobs.

### Optional: Docker, for local testing

If you have Docker Desktop and an NVIDIA GPU, you can run the exact same image
locally to test a workflow without using cloud time. This is optional. See the
[README](README.md) for the local `docker run` command.

## Stage your models once

Spark Fuse reads your models from ShareSync, so you upload them once and reuse
them on every job. Models are never built into the image.

In your ShareSync folder, create a working folder for the workflow and lay it out
exactly as ComfyUI expects under `models/`:

```
<your ShareSync folder>/my-workflow/
├── workflow.json
└── models/
    ├── checkpoints/
    ├── diffusion_models/        e.g. flux-2-klein-9b-fp8.safetensors
    ├── text_encoders/           e.g. the text encoder your workflow loads
    ├── vae/
    ├── loras/
    └── controlnet/
```

Use only the subfolders your workflow needs. Copy the model files into place using
File Explorer, then wait for the desktop app to finish uploading. You can confirm
completion in the ShareSync web interface: the folder should show its full size
once everything has synced.

A note on upload time: the upload runs at your connection's upload speed, which is
often much slower than your download speed. A large model library can take a while
the first time. You only pay this once; later jobs reference the same folder with
no further upload.

## Render a workflow

### 1. Author and export the workflow

Build your workflow in your local ComfyUI as usual. Then export it in **API
format**, not the ordinary save format. In ComfyUI, use the workflow menu's
**Export (API)** option. If you do not see it, enable the developer options in
ComfyUI settings first. Save the file as `workflow.json`.

The API format is required because Spark Fuse submits the workflow to ComfyUI's
programmatic endpoint. An ordinary UI save will be rejected with a clear error.

### 2. Place the workflow with your models

Put `workflow.json` in the same ShareSync working folder as your `models/` folder
(see the layout above). Any model name referenced inside the workflow must match
the file's location under `models/`, for example a checkpoint named
`flux-2-klein-9b-fp8.safetensors` must sit in `models/diffusion_models/`.

If your workflow was authored on Windows, check that model paths inside it do not
contain back slashes, since the container runs on Linux. A plain file name with no
folder prefix is safest.

### 3. Choose a GPU

List the available GPU types and see a cost estimate:

```powershell
spark-fuse skus
spark-fuse estimate g7e.2xlarge
```

Pick a GPU with enough memory for your models. As a guide, a workflow whose model
weights total around 17 GB runs comfortably on any 24 GB card, and faster still on
larger cards. See "Choosing a GPU" below for more.

### 4. Submit the job

Replace the instance type with your chosen GPU and the input path with your
ShareSync folder:

```powershell
spark-fuse submit --image ghcr.io/vfxguru/spark-fuse-comfyui:latest --command python3.13 --command /runner/spark_fuse_run.py --instance-type g7e.2xlarge --input-path "/my-workflow/"
```

The command prints a job ID. Copy it. Spark Fuse checks that the path exists before
it starts, so a wrong path fails immediately with an HTTP 400 and no GPU is billed.

### 5. Watch the logs

Connect to the live log stream straight away (there is no replay):

```powershell
spark-fuse logs <job-id>
```

You will see the GPU provision, the image download, your input folder copy into
`/input`, and then the runner starting ComfyUI, loading the models, and rendering.
The first few minutes are cold start (image and input download); the render itself
is usually quick.

### 6. Download the result

When the runner reports success, pull the output back:

```powershell
spark-fuse download <job-id> .\results
```

Your rendered images land in the `results` folder.

## Choosing a GPU and seeing the cost

`spark-fuse skus` lists every available instance type with its GPU and memory.
`spark-fuse estimate <instance-type>` shows the hourly rate. The rate is per hour;
your actual cost depends on how long the job runs, which includes the one-time cold
start as well as the render.

For routine work, choose the smallest GPU that fits your models, to keep cost down.
For the fastest single render, choose a larger or newer GPU.

## Troubleshooting

The runner reports a clear exit code, shown in the job summary:

| Exit code | Meaning | What to do |
| --- | --- | --- |
| 0 | Success | Download your output. |
| 1 | Workflow executed but ended in error | Read the ComfyUI error in the log; usually a bad node setting. |
| 2 | Bad input | `workflow.json` is missing, unreadable, or not API format; or a model path is wrong. |
| 3 | ComfyUI rejected the workflow | The log names the missing node type; that custom node is not in the image. |
| 4 | Job timed out | Increase the timeout, or check why the render stalled. |
| 5 | Completed but produced no files | Your workflow has no save node, so nothing was written to `/output`. |
| 6 | ComfyUI failed to start or died | Check the log near startup; report it if it persists. |

Other common situations:

- **Submission fails with `account_check_failed` or an estimate returns HTTP 404
  about "no pricing row".** That GPU type has no price configured on the platform.
  Choose a different instance type, or ask Spark Cloud Studio to add the pricing
  row. (During the alpha, some SKUs lacked pricing whilst others, such as
  `g7e.2xlarge`, worked.)
- **The workflow is rejected as the wrong format.** Re-export it using
  **Export (API)**, not an ordinary save.
- **A model is not found.** Check that the file sits under the correct `models/`
  subfolder and that the name in the workflow matches exactly, then confirm the
  upload to ShareSync has finished.
- **The render is slower than your local machine for a single image.** This is
  expected. Each job pays a one-time cold start to download the image and your
  models. The value is in offloading work and in running many images per job, not
  in beating local latency on one image.

## Current limitations and notes

- Each job downloads your input folder afresh into `/input`. Staging once on
  ShareSync removes repeated uploads from your machine, but the server side still
  copies the folder per job. Spark Cloud Studio is working on a persistent assets
  mount that will remove this for repeated runs.
- The image carries a fixed set of custom nodes (see the [README](README.md)). A
  workflow that uses a node outside that set will be rejected at submission with
  the missing node named.
- One workflow runs per job.

## Licence

[MIT](LICENSE), Copyright (c) 2026 VFXGuru.
