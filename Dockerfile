# ComfyUI on Spark Fuse — Flux test image.
# Models are NOT baked in; they arrive per job under /input/models/ (ShareSync).
FROM yanwk/comfyui-boot:cu130-slim-v2

# The base declares VOLUME /root and its entrypoint copies the bundle there on
# first start. We skip all of that: nodes are installed into the bundle itself
# and the runner launches ComfyUI from the bundle path directly.
ENV COMFYUI_DIR=/default-comfyui-bundle/ComfyUI \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PIP_NO_CACHE_DIR=1

# The base bundles ComfyUI-Manager; it must not be present on Spark Fuse
# (it tries to self-update inside the container).
RUN rm -rf "$COMFYUI_DIR/custom_nodes/ComfyUI-Manager"

COPY nodes.json /build/nodes.json
COPY scripts/install_nodes.py /build/install_nodes.py
RUN python3 /build/install_nodes.py /build/nodes.json "$COMFYUI_DIR/custom_nodes" \
    && rm -rf /build

COPY runner/ /runner/

ENTRYPOINT ["python3", "/runner/spark_fuse_run.py"]
