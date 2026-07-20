FROM ghcr.io/scaleapi/swe-atlas:swe_atlas_TW_grafana_grafana_6902ef3ab97fe23e2ad27209_1.0

WORKDIR /app

# This image does not consistently include the Go toolchain. Install the
# version used by the task's test runner, then populate the repository's
# declared module graph while the image build has network access.
RUN if ! command -v go >/dev/null 2>&1; then \
        apt-get update \
        && apt-get install -y --no-install-recommends ca-certificates wget \
        && wget -q https://go.dev/dl/go1.23.1.linux-amd64.tar.gz -O /tmp/go.tar.gz \
        && tar -C /usr/local -xzf /tmp/go.tar.gz \
        && rm /tmp/go.tar.gz \
        && rm -rf /var/lib/apt/lists/*; \
    fi \
    && PATH="/usr/local/go/bin:${PATH}" go mod download

# Dependencies must come from the image, not from public module services at
# agent or verifier runtime.
ENV PATH="/usr/local/go/bin:${PATH}" \
    GOPROXY=off \
    GOSUMDB=off
