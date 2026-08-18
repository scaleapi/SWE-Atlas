FROM ghcr.io/scaleapi/swe-atlas:swe_atlas_RF_renovatebot_renovate_69d196f015a150488265afc2_1.0@sha256:e0f24e5f7e3b723b2589997316c6231c82e1aafc2c98a2653fe70a91ab218e0e

# Allow git-over-https so harbor's codex installer can bootstrap Node via nvm
# (image locks git to file-only by default). file:// still allowed -> verifier unaffected.
ENV GIT_ALLOW_PROTOCOL=file:git:https:ssh

ENTRYPOINT []
