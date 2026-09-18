#!/usr/bin/env bash
# Run the SOC test split N times (default 3) and show per-run + average results.
#
# Usage:
#   ./scripts/run_soc_test.sh          # 3 runs
#   ./scripts/run_soc_test.sh 5        # 5 runs
#
# Requires OPENAI_API_KEY in .env. Sets LANGSMITH_TRACING=false.

set -euo pipefail
cd "$(dirname "$0")/.."

# Load .env
set -a
. .env
set +a
export LANGSMITH_TRACING=false

# Prompt/model under test — recorded per run so saved results are never
# ambiguous about which prompt/model produced them.
export WFM_PROMPT_VERSION="${WFM_PROMPT_VERSION:-v1}"
export LLM_MODEL_NAME="${LLM_MODEL_NAME:-gpt-4.1}"

RUNS="${1:-3}"
RESULTS=()

echo "=== SOC Test Split — $RUNS runs ==="
echo "  Prompt:  $WFM_PROMPT_VERSION"
echo "  Model:   $LLM_MODEL_NAME"
echo ""

for i in $(seq 1 "$RUNS"); do
    echo "--- Run $i/$RUNS  (prompt=$WFM_PROMPT_VERSION model=$LLM_MODEL_NAME) ---"
    OUTPUT=$(.venv/bin/python -m pytest -m eval_soc -v -s -k "test_eval" 2>&1)
    echo "$OUTPUT" | grep -E "(Total cases|Correct|Accuracy|WAITING_FOR_ME|NOT_WAITING|Results saved|FAIL.*socf)" || true
    echo ""

    # Extract accuracy
    ACC=$(echo "$OUTPUT" | grep "Accuracy" | grep -oE '[0-9]+\.[0-9]+%' | head -1 | tr -d '%')
    RESULTS+=("$ACC")
done

echo "=== Summary ==="
for i in $(seq 0 $((${#RESULTS[@]} - 1))); do
    echo "  Run $((i+1)): ${RESULTS[$i]}%"
done

# Compute average
SUM=0
for acc in "${RESULTS[@]}"; do
    SUM=$(echo "$SUM + $acc" | bc -l)
done
AVG=$(echo "scale=1; $SUM / ${#RESULTS[@]}" | bc -l)
echo ""
echo "  Average: ${AVG}%"
