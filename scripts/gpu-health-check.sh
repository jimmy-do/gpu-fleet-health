#!/usr/bin/env bash
# gpu-health-check.sh: local GPU health probe built on nvidia-smi.
#
# Exit codes:
#   0  OK
#   1  a GPU is over the temperature threshold
#   2  GPU missing (found fewer/more GPUs than expected)
#   3  cannot query GPUs (nvidia-smi absent, driver/NVML failure, unreadable value)
#
# On failure it PRINTS the kubectl cordon/drain commands an operator would run.
# It never runs them: taking a node out of service is a human decision.
#
# Env overrides: EXPECTED_GPUS (default 1), TEMP_LIMIT_C (default 83), NODE_NAME.
set -euo pipefail

EXPECTED_GPUS="${EXPECTED_GPUS:-1}"
TEMP_LIMIT_C="${TEMP_LIMIT_C:-83}"
NODE_NAME="${NODE_NAME:-$(hostname)}"

# Print, never execute, the remediation commands.
print_remediation() {
  echo "DRY RUN: would run (not executed):"
  echo "  kubectl cordon ${NODE_NAME}"
  echo "  kubectl drain ${NODE_NAME} --ignore-daemonsets --delete-emptydir-data --timeout=10m"
}

is_int() { [[ "$1" =~ ^[0-9]+$ ]]; }

if ! is_int "$EXPECTED_GPUS" || ! is_int "$TEMP_LIMIT_C"; then
  echo "UNKNOWN: EXPECTED_GPUS and TEMP_LIMIT_C must be non-negative integers" >&2
  exit 3
fi

if ! command -v nvidia-smi >/dev/null 2>&1; then
  echo "CRITICAL: nvidia-smi not found; is the NVIDIA driver installed?"
  print_remediation
  exit 3
fi

# One CSV line per GPU: "0, 41, NVIDIA GeForce RTX 4090". Name goes last
# because it contains spaces. "|| rc=$?" keeps set -e from exiting early.
rc=0
output=$(nvidia-smi --query-gpu=index,temperature.gpu,name \
  --format=csv,noheader,nounits 2>&1) || rc=$?

found=0
status=0
if [[ $rc -ne 0 ]]; then
  # "No devices were found" means the driver works but sees no GPU: that is a
  # missing GPU (exit 2), not a query failure.
  if [[ "$output" != *"No devices were found"* ]]; then
    echo "CRITICAL: nvidia-smi failed (rc=${rc}): ${output}"
    print_remediation
    exit 3
  fi
else
  while IFS=, read -r idx temp name; do
    found=$((found + 1))
    temp="${temp// /}"
    if ! is_int "$temp"; then
      echo "CRITICAL: GPU ${idx}: unreadable temperature '${temp}'"
      print_remediation
      exit 3
    fi
    if [[ "$temp" -gt "$TEMP_LIMIT_C" ]]; then
      echo "WARNING: GPU ${idx} (${name# }) at ${temp}C > ${TEMP_LIMIT_C}C"
      status=1
    else
      echo "OK: GPU ${idx} (${name# }) at ${temp}C"
    fi
  done <<< "$output"
fi

# A missing GPU outranks a hot one.
if [[ "$found" -ne "$EXPECTED_GPUS" ]]; then
  echo "CRITICAL: expected ${EXPECTED_GPUS} GPU(s), found ${found}"
  status=2
fi

if [[ "$status" -ne 0 ]]; then
  print_remediation
else
  echo "OK: ${found}/${EXPECTED_GPUS} GPU(s) healthy on ${NODE_NAME}"
fi
exit "$status"
