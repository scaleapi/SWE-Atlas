FROM ghcr.io/scaleapi/swe-atlas:swe_atlas_TW_simple-login_app_6902ef3ab97fe23e2ad27254_1.0

WORKDIR /app

# The verifier resolves its interpreter out of the uv tool directory, but
# astral.sh is not on this task's allowlist. Install uv and the interpreter it
# needs while the image build still has network access.
RUN if command -v apt-get >/dev/null 2>&1; then \
        apt-get update \
        && apt-get install -y --no-install-recommends curl ca-certificates \
        && rm -rf /var/lib/apt/lists/*; \
    elif command -v apk >/dev/null 2>&1; then \
        apk add --no-cache curl ca-certificates; \
    fi

RUN curl -LsSf https://astral.sh/uv/0.7.13/install.sh | sh \
    && ln -sf "$HOME/.local/bin/uv" /usr/local/bin/uv \
    && uv python install 3.11

# Interpreters must come from the image, not from astral.sh at agent or
# verifier runtime.
ENV UV_PYTHON=3.11 \
    UV_PYTHON_DOWNLOADS=never
