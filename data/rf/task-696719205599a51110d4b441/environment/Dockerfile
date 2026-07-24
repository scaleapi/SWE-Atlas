FROM ghcr.io/scaleapi/swe-atlas:swe_atlas_RF_grafana_k6_696719205599a51110d4b441_1.0@sha256:9da4905045175cce58a722e8e0b4e74f1bdf9beb1d615f0a7cb088aff0b94fbf

ENV PATH="/usr/local/go/bin:${PATH}"

# Make offline (agent-phase, allowlisted) Go builds work without a module proxy.
# Vendored repos already carry their modules in vendor/ (fetching the full graph
# would chase dead vanity hosts like go.buf.build); just build from vendor, which
# also triggers any needed toolchain download. Non-vendored repos: download the
# module graph. Either way, install the resolved toolchain as the default GOROOT.
# No WORKDIR override: the base image's WORKDIR is the repo root (varies per repo).
RUN if [ -d vendor ]; then \
        GOTOOLCHAIN=auto go build -mod=vendor ./... 2>/dev/null || true; \
    else \
        GOTOOLCHAIN=auto go mod download all; \
    fi && \
    TC="$(ls -d /go/pkg/mod/golang.org/toolchain@*/ 2>/dev/null | sort -V | tail -1)" && \
    if [ -n "$TC" ]; then rm -rf /usr/local/go && cp -a "${TC%/}" /usr/local/go && chmod -R a+rX /usr/local/go; fi

ENV GOPROXY=off \
    GOSUMDB=off \
    GOTOOLCHAIN=local
