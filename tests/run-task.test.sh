#!/usr/bin/env bash
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

pass=0
fail=0

run_case() {
  local want="$1" label="$2"
  shift 2
  local tmp out rc
  tmp="$(mktemp -d)"
  mkdir -p "$tmp/bin" "$tmp/home/.ssh"
  printf 'Host deploy-target\n  HostName control.invalid\n' > "$tmp/ssh_config"
  cat > "$tmp/bin/ssh" <<'SSH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$SSH_CALLS"
printf '%s\n' "${STUB_REMOTE_OUTPUT:-safe remote output}"
exit "${STUB_SSH_RC:-0}"
SSH
  chmod 755 "$tmp/bin/ssh"
  : > "$tmp/calls"
  out="$(env -i PATH="$tmp/bin:$PATH" HOME="$tmp/home" SSH_CONFIG="$tmp/ssh_config" \
    SSH_CALLS="$tmp/calls" APP= TASK= MODE= KOMIZO_APP_NAME= \
    "$@" bash run-task/run.sh 2>&1)"
  rc=$?
  if [ "$rc" -eq "$want" ]; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    printf 'FAIL  %s: expected rc=%s, got rc=%s\n%s\n' "$label" "$want" "$rc" "$out"
  fi
  LAST_TMP=$tmp
  LAST_OUT=$out
}

assert_no_call() {
  if [ ! -s "$LAST_TMP/calls" ]; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    printf 'FAIL  malformed input reached ssh: %s\n' "$(cat "$LAST_TMP/calls")"
  fi
}

echo "== exact allowed invocations =="
for mode in dry-run apply constrain; do
  run_case 0 "$mode accepted" APP=termcade TASK=release-identity-backfill MODE="$mode"
  expected="deploy-target doas /usr/local/bin/task-termcade release-identity-backfill $mode"
  if grep -qxF "$expected" "$LAST_TMP/calls"; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    printf 'FAIL  fixed argv for %s, got: %s\n' "$mode" "$(cat "$LAST_TMP/calls")"
  fi
done
for mode in inspect backup drill seal reset rollback; do
  run_case 0 "production-data $mode accepted" APP=termcade TASK=production-data MODE="$mode"
  expected="deploy-target doas /usr/local/bin/task-termcade production-data $mode"
  if grep -qxF "$expected" "$LAST_TMP/calls"; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    printf 'FAIL  fixed production-data argv for %s, got: %s\n' "$mode" "$(cat "$LAST_TMP/calls")"
  fi
done

echo "== malformed input denied before ssh =="
while IFS='|' read -r label app task mode; do
  run_case 64 "$label" APP="$app" TASK="$task" MODE="$mode"
  assert_no_call
done <<'CASES'
missing app||release-identity-backfill|dry-run
wrong app|other|release-identity-backfill|dry-run
unknown task|termcade|other|dry-run
missing mode|termcade|release-identity-backfill|
path|termcade|release-identity-backfill|/bin/sh
image|termcade|release-identity-backfill|ghcr.io/evil/image
service|termcade|release-identity-backfill|db
environment|termcade|release-identity-backfill|X=1
shell|termcade|release-identity-backfill|dry-run;id
cross-task mode|termcade|production-data|apply
unknown data mode|termcade|production-data|destroy
CASES
run_case 64 "control character" APP=termcade TASK=release-identity-backfill MODE=$'dry-run\napply'
assert_no_call

echo "== connection seam, exit propagation, and redaction =="
run_case 1 "missing connect alias" APP=termcade TASK=release-identity-backfill MODE=dry-run SSH_CONFIG=/nonexistent
assert_no_call

run_case 23 "ssh exit propagates" APP=termcade TASK=release-identity-backfill MODE=dry-run STUB_SSH_RC=23
if [[ "$LAST_OUT" == *"::stop-commands::"* && "$LAST_OUT" == *"::komizo-task-"* ]]; then
  pass=$((pass + 1))
else
  fail=$((fail + 1))
  printf 'FAIL  remote-output fence did not close:\n%s\n' "$LAST_OUT"
fi

secret=credential-must-not-appear
run_case 0 "secret is not forwarded" APP=termcade TASK=release-identity-backfill MODE=dry-run \
  KOMIZO_DEPLOY_KEY="$secret"
if ! grep -qF "$secret" "$LAST_TMP/calls" && [[ "$LAST_OUT" != *"$secret"* ]] && ! grep -q 'GITHUB_OUTPUT' run-task/run.sh; then
  pass=$((pass + 1))
else
  fail=$((fail + 1))
  printf 'FAIL  secret or output channel reached task invocation\n'
fi

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
