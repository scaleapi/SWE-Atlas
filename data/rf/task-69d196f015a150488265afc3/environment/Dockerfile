FROM ghcr.io/scaleapi/swe-atlas:swe_atlas_RF_renovatebot_renovate_69d196f015a150488265afc3_1.0@sha256:6b52ec19f09f7641db84178c9d41e89630da82b74e5732d0037c31aa2fb3cb31

# Allow git-over-https so harbor's codex installer can bootstrap Node via nvm
# (image locks git to file-only by default). file:// still allowed -> verifier unaffected.
ENV GIT_ALLOW_PROTOCOL=file:git:https:ssh

ENTRYPOINT []
