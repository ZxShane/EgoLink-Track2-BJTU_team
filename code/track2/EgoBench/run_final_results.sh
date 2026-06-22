#!/bin/bash

# ==============================================================================
# EgoBench Competition - Run Final Result Scenarios Only
# ==============================================================================
#
# Runs only the final evaluation scenario set:
#   retail6, retail10, kitchen4, restaurant5, order2
#
# Usage:
#   bash run_final_results.sh
#   bash run_final_results.sh --scenarios retail,kitchen
#   bash run_final_results.sh --scenarios restaurant --num_tasks 5
#   bash run_final_results.sh --scenarios retail,order --parallel 2
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

NUM_TASKS=0
START_TASK=1
MAX_PARALLEL_SCENARIOS=1
OUTPUT_DIR="${OUTPUT_DIR:-final_results}"
SCENARIOS="retail,kitchen,restaurant,order"

usage() {
    cat <<'USAGE'
Usage: bash run_final_results.sh [options]

Options:
  --scenarios LIST     Comma-separated scenario families to run.
                       Allowed: retail,kitchen,restaurant,order
                       Default: retail,kitchen,restaurant,order
  --num_tasks N        Number of tasks per scenario. 0 means all tasks. Default: 0
  --start_task N       Start task index for each scenario. Default: 1
  --parallel N         Max number of scenario jobs to run in parallel. Default: 1
  --output_dir DIR     Output directory. Default: final_results
  -h, --help           Show this help.

Examples:
  bash run_final_results.sh
  bash run_final_results.sh --scenarios retail
  bash run_final_results.sh --scenarios restaurant,order --num_tasks 5
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --scenarios)
            SCENARIOS="$2"
            shift 2
            ;;
        --num_tasks)
            NUM_TASKS="$2"
            shift 2
            ;;
        --start_task)
            START_TASK="$2"
            shift 2
            ;;
        --parallel)
            MAX_PARALLEL_SCENARIOS="$2"
            shift 2
            ;;
        --output_dir)
            OUTPUT_DIR="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            usage
            exit 1
            ;;
    esac
done

if [ -f ".env" ]; then
    source ".env"
fi

# Keep these defaults aligned with run_all_scenarios.sh. Environment variables
# from .env or the shell can still override them before this script is invoked.
export USER_MODEL_NAME="${USER_MODEL_NAME:-qwen3.7-plus}"
export SERVICE_MODEL_NAME="${SERVICE_MODEL_NAME:-qwen3.7-plus}"
export VALIDATOR_MODEL_NAME="${VALIDATOR_MODEL_NAME:-glm-5.2}"
export USER_API_BASE_URL="${USER_API_BASE_URL:-https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1}"
export SERVICE_API_BASE_URL="${SERVICE_API_BASE_URL:-https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1}"
export API_KEY="${API_KEY:}"
export SERVICE_API_KEY="${SERVICE_API_KEY:}"
export VIDEO_MODE="${VIDEO_MODE:-url}"

if [ -x "$SCRIPT_DIR/.venv/bin/python" ]; then
    PYTHON_BIN="$SCRIPT_DIR/.venv/bin/python"
else
    PYTHON_BIN="${PYTHON_BIN:-python3}"
fi

mkdir -p "$OUTPUT_DIR"

scenario_enabled() {
    local wanted="$1"
    local normalized
    normalized="$(echo "$SCENARIOS" | tr '[:upper:]' '[:lower:]' | tr ',' ' ')"
    for item in $normalized; do
        if [ "$item" = "$wanted" ]; then
            return 0
        fi
    done
    return 1
}

validate_scenarios() {
    local normalized
    normalized="$(echo "$SCENARIOS" | tr '[:upper:]' '[:lower:]' | tr ',' ' ')"
    for item in $normalized; do
        case "$item" in
            retail|kitchen|restaurant|order)
                ;;
            "")
                ;;
            *)
                echo "Invalid scenario in --scenarios: $item"
                echo "Allowed: retail,kitchen,restaurant,order"
                exit 1
                ;;
        esac
    done
}

run_scenario() {
    local scenario="$1"
    local scenario_number="$2"

    echo "Running final scenario: ${scenario}${scenario_number}"
    "$PYTHON_BIN" run/multi_agent.py \
        --scenario "$scenario" \
        --scenario_number "$scenario_number" \
        --service_model_name "$SERVICE_MODEL_NAME" \
        --multi_agent_user \
        --summary_user \
        --max_service_model_requests_per_task 30 \
        --output_dir "$OUTPUT_DIR" \
        --start_task "$START_TASK" \
        --num_tasks "$NUM_TASKS"
    echo "Completed final scenario: ${scenario}${scenario_number}"
    echo ""
}

run_scenario_bg() {
    if [ "$MAX_PARALLEL_SCENARIOS" -le 1 ]; then
        run_scenario "$1" "$2"
        return
    fi

    while [ "$(jobs -rp | wc -l | tr -d ' ')" -ge "$MAX_PARALLEL_SCENARIOS" ]; do
        sleep 5
    done
    run_scenario "$1" "$2" &
}

wait_for_scenarios() {
    if [ "$MAX_PARALLEL_SCENARIOS" -gt 1 ]; then
        wait
    fi
}

validate_scenarios

echo "=========================================="
echo "EgoBench Final Results Runner"
echo "=========================================="
echo "Configuration:"
echo "  User Model: $USER_MODEL_NAME"
echo "  Service Model: $SERVICE_MODEL_NAME"
echo "  Validator Model: $VALIDATOR_MODEL_NAME"
echo "  Video Mode: $VIDEO_MODE"
echo "  Output Dir: $OUTPUT_DIR"
echo "  Scenarios: $SCENARIOS"
echo "  Num tasks: $NUM_TASKS"
echo "  Start task: $START_TASK"
echo "  Parallel scenarios: $MAX_PARALLEL_SCENARIOS"
echo ""

if scenario_enabled "retail"; then
    run_scenario_bg "retail" 6
    run_scenario_bg "retail" 10
    wait_for_scenarios
fi

if scenario_enabled "kitchen"; then
    run_scenario_bg "kitchen" 4
    wait_for_scenarios
fi

if scenario_enabled "restaurant"; then
    run_scenario_bg "restaurant" 5
    wait_for_scenarios
fi

if scenario_enabled "order"; then
    run_scenario_bg "order" 2
    wait_for_scenarios
fi

echo "=========================================="
echo "Final result scenarios completed"
echo "Results saved under: $OUTPUT_DIR"
echo "=========================================="
