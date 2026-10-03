# Based on NVIDIA Isaac Sim's Apache-2.0 licensed tools/docker/web-viewer/Dockerfile:
# https://github.com/isaac-sim/IsaacSim/blob/main/tools/docker/web-viewer/Dockerfile
# Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# Build stage
FROM nvcr.io/nvidia/base/ubuntu:noble-20251013 AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates \
    && curl -fsSL --proto '=https' https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Scope the NVIDIA npm registry, scaffold the app, then clean up the credential
RUN echo '@nvidia:registry=https://edge.urm.nvidia.com/artifactory/api/npm/omniverse-client-npm/' >> /root/.npmrc \
    && npx @nvidia/create-ov-web-rtc-app@2.1.5 --name=web-viewer --sample=local-sample --external --outputDir=/app sample-app --framework=react \
    && rm -f /root/.npmrc

WORKDIR /app/web-viewer

# Patch stream config with build-time values
ARG ISAACSIM_HOST=127.0.0.1
ARG ISAACSIM_SIGNAL_PORT=49100
ARG ISAACSIM_STREAM_PORT=47998

RUN grep -qF "signalingServer: '127.0.0.1'," src/main.tsx \
    && sed -i "s/signalingServer: [^,]*/signalingServer: '${ISAACSIM_HOST}'/" src/main.tsx \
    && sed -i "/signalingServer:/a\\    signalingPort: ${ISAACSIM_SIGNAL_PORT},\n    mediaServer: '${ISAACSIM_HOST}',\n    mediaPort: ${ISAACSIM_STREAM_PORT},\n    forceWSS: false," src/main.tsx \
    && grep -qF "signalingPort: ${ISAACSIM_SIGNAL_PORT}," src/main.tsx \
    && grep -qF "mediaPort: ${ISAACSIM_STREAM_PORT}," src/main.tsx

# Patch: preserve browser shortcuts (DevTools, refresh) from being swallowed by the stream.
# F11: fullscreen toggle via Fullscreen API. On Mac, Fn+F11 sends F11; we also check e.code.
# F5 / Cmd+R or Ctrl+R: explicitly reload (capture phase stopPropagation would block browser default).
# F12: stopPropagation so stream does not see it (DevTools may need focus outside stream).
# F7: prevent Chrome caret-browsing dialog only.
# Clipboard shortcuts (Ctrl+C/V/X) are intentionally left for the streaming library to handle.
RUN grep -qF 'void app.initialize();' src/main.tsx \
    && sed -i '/void app.initialize/i\
const sc = document.getElementById("stream-container");\
if (sc) {\
    sc.addEventListener("keydown", (e: KeyboardEvent) => {\
        if (e.key === "F11" || e.code === "F11") { e.preventDefault(); e.stopPropagation(); if (document.fullscreenElement) { document.exitFullscreen(); } else { document.documentElement.requestFullscreen(); } return; }\
        if (e.key === "F7") { e.preventDefault(); return; }\
        if (["F5"].includes(e.key) || e.code === "F5") { e.preventDefault(); e.stopPropagation(); window.location.reload(); return; }\
        if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "r") { e.preventDefault(); e.stopPropagation(); window.location.reload(); return; }\
        if (["F12"].includes(e.key) || e.code === "F12") { e.stopPropagation(); return; }\
        if ((e.ctrlKey || e.metaKey) && e.shiftKey && ["i","j","c"].includes(e.key.toLowerCase())) { e.stopPropagation(); return; }\
        if (e.metaKey && e.altKey && ["i","j","c"].includes(e.key.toLowerCase())) { e.stopPropagation(); return; }\
    }, true);\
}' src/main.tsx \
    && grep -qF 'const sc = document.getElementById("stream-container");' src/main.tsx

RUN npm install --ignore-scripts
RUN npm run build

# Production stage
FROM nvcr.io/nvidia/base/ubuntu:noble-20251013

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
    && curl -fsSL --proto '=https' https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -r appuser --gid 1234 && useradd --no-log-init -r -g appuser --uid 1234 -d /app -s /usr/sbin/nologin appuser

WORKDIR /app

COPY --from=builder --chmod=755 /app/web-viewer/package.json ./

RUN echo '@nvidia:registry=https://edge.urm.nvidia.com/artifactory/api/npm/omniverse-client-npm/' >> /root/.npmrc \
    && npm install --ignore-scripts \
    && npm cache clean --force \
    && rm -f /root/.npmrc \
    && chown -R appuser:appuser node_modules

COPY --from=builder --chmod=755 /app/web-viewer/dist ./dist

USER appuser

EXPOSE 8210

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8210/ || exit 1

CMD ["./node_modules/.bin/vite", "preview", "--host", "--port", "8210"]
