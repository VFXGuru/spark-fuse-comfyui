# ComfyUI on Spark Fuse — Flux test image.
# Models are NOT baked in; they arrive per job under /input/models/ (ShareSync).
FROM yanwk/comfyui-boot:cu130-slim-v2

# The base's entrypoint copies the bundle to /root on first start; we skip all
# of that — nodes are installed into the bundle itself and the runner launches
# ComfyUI from the bundle path directly.
#
# ComfyUI's Python stack (torch, pip) lives under python3.13; plain `python3`
# in this base is a bare 3.12 with no pip. Everything here must use python3.13.
ENV COMFYUI_DIR=/default-comfyui-bundle/ComfyUI \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PIP_NO_CACHE_DIR=1

# The base ships ComfyUI-Manager as a pip package with its startup cache-update
# already patched out; this rm only guards against bundle layouts that drop a
# self-updating Manager into custom_nodes.
RUN rm -rf "$COMFYUI_DIR/custom_nodes/ComfyUI-Manager"

COPY nodes.json /build/nodes.json
COPY scripts/install_nodes.py /build/install_nodes.py
RUN python3.13 /build/install_nodes.py /build/nodes.json "$COMFYUI_DIR/custom_nodes" \
    && rm -rf /build

COPY runner/ /runner/

ENTRYPOINT ["python3.13", "/runner/spark_fuse_run.py"]
