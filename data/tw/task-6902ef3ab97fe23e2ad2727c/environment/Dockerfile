FROM ghcr.io/scaleapi/swe-atlas:swe_atlas_TW_grafana_k6_6902ef3ab97fe23e2ad2727c_1.0

WORKDIR /app

# Populate the repository pinned Go module graph while the image build has
# network access. Agent and verifier runtime must use only this baked cache.
RUN go mod download

ENV GOPROXY=off \
    GOSUMDB=off
