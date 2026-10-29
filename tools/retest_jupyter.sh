#!/usr/bin/env bash
# Run from Jupyter Terminal after updating the submission files.
set -euo pipefail

TEAM="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OFFICIAL="${OFFICIAL:-/workspace/rtlagent2026}"
OUT="${OUT:-/var/tmp/rtl-compare-$(date +%Y%m%d_%H%M%S)}"
SAMPLES="${SAMPLES:-1}"

cd "$TEAM"
python3 "$TEAM/tools/check_submission.py"

test -f "$OFFICIAL/selftest/run_selftest.sh" || {
  echo "Official selftest not found: $OFFICIAL/selftest/run_selftest.sh" >&2
  exit 1
}

export LLM_BACKEND=openai
export LLM_BASE_URL="${LLM_BASE_URL:-http://127.0.0.1:8000/v1}"
export LLM_API_KEY="${LLM_API_KEY:-dummy}"
export LLM_MODEL="${LLM_MODEL:-Qwen3-8B}"
export MODEL_NAME="$LLM_MODEL"
export LLM_MAX_TOKENS="${LLM_MAX_TOKENS:-16384}"
export LLM_TEMPERATURE=0.0
export AGENT_BEHAVIOR_REVIEW="${AGENT_BEHAVIOR_REVIEW:-1}"
export AGENT_REVIEW_MODE="${AGENT_REVIEW_MODE:-evidence}"
export AGENT_TMP="${AGENT_TMP:-/var/tmp}"
export EDA_TMP="${EDA_TMP:-/var/tmp/eda}"
export SELFTEST_TMP="${SELFTEST_TMP:-/var/tmp}"
export XILINXD_LICENSE_FILE="${XILINXD_LICENSE_FILE:-2100@vivado-lic.fpgachina.svc.cluster.local}"
export FLEXLM_TIMEOUT="${FLEXLM_TIMEOUT:-300000000}"
mkdir -p "$AGENT_TMP" "$EDA_TMP" "$SELFTEST_TMP"

# The official driver sources env.sh again. Catch its hardcoded mock default
# before spending time on a run that would not use the declared real model.
if [ -f "$OFFICIAL/selftest/env.sh" ]; then
  if ! bash -c '
    AGENT_DIR="$4"
    OUT_DIR="$5"
    source "$1" >/dev/null 2>&1
    [ "${LLM_BACKEND:-mock}" = openai ] &&
    [ "${LLM_MODEL:-}" = "$2" ] && [ "${LLM_BASE_URL:-}" = "$3" ] &&
    [ "$AGENT_DIR" = "$4" ] && [ "$OUT_DIR" = "$5" ]
  ' bash "$OFFICIAL/selftest/env.sh" "$LLM_MODEL" "$LLM_BASE_URL" "$TEAM" "$OUT"; then
    echo "Official selftest/env.sh overrides model settings or AGENT_DIR/OUT_DIR." >&2
    echo "Use LLM_BACKEND=openai, LLM_MODEL=$LLM_MODEL, LLM_BASE_URL=$LLM_BASE_URL; remove stale AGENT_DIR/OUT_DIR assignments." >&2
    exit 1
  fi
fi

if ! command -v vivado >/dev/null 2>&1; then
  if [ -f /tools/Xilinx/2026.1/Vivado/settings64.sh ]; then
    # Vendor setup scripts are not necessarily nounset-clean.
    set +u
    source /tools/Xilinx/2026.1/Vivado/settings64.sh
    set -u
  fi
fi
for command in python3 xvlog xelab xsim vivado; do
  command -v "$command" >/dev/null || { echo "Missing command: $command" >&2; exit 1; }
done

# The official driver executes these two scripts directly.
chmod u+x "$TEAM/run.sh" "$TEAM/run_baseline.sh"
python3 - <<'PY'
import json
import os
from urllib.request import Request, urlopen

headers = {"Authorization": "Bearer " + os.environ["LLM_API_KEY"]}
request = Request(os.environ["LLM_BASE_URL"].rstrip("/") + "/models", headers=headers)
try:
    with urlopen(request, timeout=10) as response:
        names = [m["id"] for m in json.load(response).get("data", [])]
except Exception as exc:
    raise SystemExit("Model API not ready: " + type(exc).__name__) from exc
if os.environ["LLM_MODEL"] not in names:
    raise SystemExit("LLM_MODEL does not match served IDs: " + repr(names))
print("Real model API ready:", os.environ["LLM_MODEL"])
PY

cd "$OFFICIAL/selftest"
echo "Results: $OUT"
bash ./run_selftest.sh --tasks "$OFFICIAL/tasks" --agent "$TEAM" --out "$OUT" --samples "$SAMPLES"
python3 "$TEAM/tools/check_submission.py" --results "$OUT"
echo "Score: $OUT/score.json"
