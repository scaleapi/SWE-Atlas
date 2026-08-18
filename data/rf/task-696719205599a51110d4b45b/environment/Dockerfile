FROM ghcr.io/scaleapi/swe-atlas:swe_atlas_RF_grafana_grafana_696719205599a51110d4b45b_1.0@sha256:bacf98df3c3c6517847552302313d169ef26f06e4030310a5ca5e9c92e75ee80

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
