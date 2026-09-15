#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

for claim in 'own immutable tag' 'releases are immutable' 'Both are immutable'; do
  if grep -Fqi "$claim" scripts/release.sh .github/workflows/release.yml; then
    printf 'unsupported release immutability claim remains: %s\n' "$claim" >&2
    exit 1
  fi
done

grep -Fq 'this workflow never moves release tags' scripts/release.sh
grep -Fq 'GitHub release records remain editable' scripts/release.sh .github/workflows/release.yml
grep -Fq 'superseded by v0.0.7 and later' README.md
if grep -Fq 'superseded by v0.0.6' README.md; then
  printf 'README names the unshipped release candidate as published\n' >&2
  exit 1
fi
