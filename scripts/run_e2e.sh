#!/usr/bin/env bash
# Run e2e tests for different essay sizes.
# Usage:
#   ./scripts/run_e2e.sh              # run all scenarios
#   ./scripts/run_e2e.sh e2e_short    # run one scenario
set -euo pipefail

SCENARIOS=(e2e_short e2e_medium e2e_long)

if [[ $# -gt 0 ]]; then
    SCENARIOS=("$@")
fi

PASS=()
FAIL=()

for scenario in "${SCENARIOS[@]}"; do
    dir="examples/${scenario}"
    if [[ ! -d "$dir" ]]; then
        echo "SKIP $scenario — directory not found"
        continue
    fi

    outdir=".output/e2e_${scenario}"
    rm -rf "$outdir"
    echo ""
    echo "================================================================"
    echo "  E2E: $scenario"
    echo "================================================================"

    if ESSAY_WRITER_WRITING__INTERACTIVE_VALIDATION=false \
       uv run python -m src.runner "$dir" --dump-vfs 2>&1 | tee "$outdir.log"; then
        # Move timestamped output to predictable path
        latest=$(ls -dt .output/run_* 2>/dev/null | head -1)
        if [[ -n "$latest" ]]; then
            mv "$latest" "$outdir"
        fi
        PASS+=("$scenario")
        echo ""
        echo "PASS: $scenario -> $outdir"
    else
        FAIL+=("$scenario")
        echo ""
        echo "FAIL: $scenario"
    fi
done

echo ""
echo "================================================================"
echo "  Results: ${#PASS[@]} passed, ${#FAIL[@]} failed"
echo "================================================================"
for s in "${PASS[@]}"; do echo "  PASS  $s"; done

if (( ${#FAIL[@]} > 0 )); then
    for s in "${FAIL[@]}"; do echo "  FAIL  $s"; done
fi

[[ ${#FAIL[@]} -eq 0 ]]
