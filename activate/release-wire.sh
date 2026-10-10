#!/usr/bin/env bash
# Framing is shared by legacy and scoped activation; no app secret is encoded.
proof_file=""
komizo_prepare_release_wire() {
  if [ -n "${RELEASE_MANIFEST:-}" ] && [ "$VERSION" = "${GITHUB_SHA:-}" ]; then
    if [ -z "$REGISTRY" ] || [ -z "$REGISTRY_USER" ] || [ -z "$REGISTRY_TOKEN" ]; then
      echo "::error::release proof requires registry credentials for the framed deploy protocol."
      return 1
    fi
    proof_file=$(mktemp)
    local library_root
    library_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    python3 "$library_root/scripts/release-proof.py" envelope --version "$VERSION" --source "$RELEASE_MANIFEST" --output "$proof_file"
  fi
}
komizo_release_wire() {
  if [ -n "$proof_file" ]; then
    printf '%s\nkomizo-release/v1:' "$REGISTRY_TOKEN"
    base64 -w 0 "$proof_file"
  else
    printf '%s' "$REGISTRY_TOKEN"
  fi
}
