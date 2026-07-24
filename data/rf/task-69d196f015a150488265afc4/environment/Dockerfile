FROM ghcr.io/scaleapi/swe-atlas:swe_atlas_RF_renovatebot_renovate_69d196f015a150488265afc4_1.0@sha256:b839cb6f798a0ce50f4dc285d352b548615897b0ca85bff3bec736a234e5728d

# Allow git-over-https so harbor's codex installer can bootstrap Node via nvm
# (image locks git to file-only by default). file:// still allowed -> verifier unaffected.
ENV GIT_ALLOW_PROTOCOL=file:git:https:ssh

ENTRYPOINT []
