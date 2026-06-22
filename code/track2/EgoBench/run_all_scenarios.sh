#!/bin/bash

# ==============================================================================
# EgoBench Competition - Run All Scenarios
# ==============================================================================
#
# This script runs all scenarios for the competition.
# Participants should configure their models in:
#   - config/user_agent_config.py (for user simulation)
#   - config/service_agent_config.py (for service agent)
#
# Usage:
#   # Run offline testing scenarios (default)
#   bash run_all_scenarios.sh
#
#   # Run final evaluation scenarios (retail6, retail10, kitchen4, restaurant5, order2)
#   bash run_all_scenarios.sh --final_eval
#
# Optional: Specify number of tasks per scenario
#   bash run_all_scenarios.sh --num_tasks 10
#   bash run_all_scenarios.sh --final_eval --num_tasks 10
#
# Optional: Run scenarios in parallel
#   bash run_all_scenarios.sh --parallel 3
#
# Optional: Run only one scenario family
#   bash run_all_scenarios.sh --scenario kitchen --num_tasks 5
#
# Optional: Run only one scenario number within a family
#   bash run_all_scenarios.sh --scenario retail --scenario_number 1 --num_tasks 1
# ==============================================================================

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

# Default number of tasks (0 = all tasks)
NUM_tasks=0
# Default mode: offline testing
FINAL_EVAL=false
# Default scenario-level parallelism. 1 means sequential.
MAX_PARALLEL_SCENARIOS=1
# Output directory can be overridden for smoke tests.
OUTPUT_DIR="${OUTPUT_DIR:-results}"
# Optional scenario filter: retail, kitchen, restaurant, or order.
SCENARIO_FILTER=""
# Optional scenario number filter.
SCENARIO_NUMBER_FILTER=""

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --num_tasks)
            NUM_tasks="$2"
            shift 2
            ;;
        --final_eval)
            FINAL_EVAL=true
            shift
            ;;
        --parallel)
            MAX_PARALLEL_SCENARIOS="$2"
            shift 2
            ;;
        --scenario)
            SCENARIO_FILTER="$2"
            shift 2
            ;;
        --scenario_number)
            SCENARIO_NUMBER_FILTER="$2"
            shift 2
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# Source environment variables if .env exists
if [ -f ".env" ]; then
    source ".env"
fi

# Your settings here
export USER_MODEL_NAME="qwen3.7-plus"
export SERVICE_MODEL_NAME="qwen3.7-plus"
export VALIDATOR_MODEL_NAME="glm-5.2"
export USER_API_BASE_URL="https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
export SERVICE_API_BASE_URL="https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
export API_KEY=""
export SERVICE_API_KEY=""
export GROUNDING_MODEL_NAME="gpt-5.5"
export GROUNDING_API_BASE_URL="${GROUNDING_API_BASE_URL:-https://api.openai.com/v1}"
export GROUNDING_API_KEY="${GROUNDING_API_KEY:-${OPENAI_API_KEY:-your-gpt-5.5-api-key}}"
export GROUNDING_EXTRA_BODY_ENABLED="false"
export VIDEO_MODE="url"


# Print configuration
echo "=========================================="
echo "EgoBench Competition - Running All Scenarios"
echo "=========================================="
echo ""
echo "Configuration:"
echo "  User Model: $USER_MODEL_NAME"
echo "  Service Model: $SERVICE_MODEL_NAME"
echo "  Validator Model: $VALIDATOR_MODEL_NAME"
echo "  Runtime Grounding Model: $GROUNDING_MODEL_NAME"
echo "  User API URL: $USER_API_BASE_URL"
echo "  Service API URL: $SERVICE_API_BASE_URL"
echo "  Runtime Grounding API URL: $GROUNDING_API_BASE_URL"
echo "  Video Mode: $VIDEO_MODE"
echo "  Final Eval: $FINAL_EVAL"
echo "  Num tasks: $NUM_tasks"
echo "  Parallel scenarios: $MAX_PARALLEL_SCENARIOS"
if [ -n "$SCENARIO_FILTER" ]; then
    echo "  Scenario filter: $SCENARIO_FILTER"
fi
if [ -n "$SCENARIO_NUMBER_FILTER" ]; then
    echo "  Scenario number filter: $SCENARIO_NUMBER_FILTER"
fi
echo ""



# Check if required environment variables are set
# if [ -z "$API_KEY" ] && [ -z "$SERVICE_API_KEY" ]; then
#     echo "Error: API_KEY or SERVICE_API_KEY environment variable is not set."
#     echo "Please set your API key in .env file or as environment variable."
#     exit 1
# fi

# Create output directory if it doesn't exist
mkdir -p "$OUTPUT_DIR"

if [ -x "$SCRIPT_DIR/.venv/bin/python" ]; then
    PYTHON_BIN="$SCRIPT_DIR/.venv/bin/python"
else
    PYTHON_BIN="${PYTHON_BIN:-python3}"
fi

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

# Function to run a scenario
run_scenario() {
    local scenario=$1
    local scenario_number=$2

    echo "Running: $scenario$scenario_number (easy mode)"

    "$PYTHON_BIN" run/multi_agent.py \
        --scenario "$scenario" \
        --scenario_number "$scenario_number" \
        --service_model_name "$SERVICE_MODEL_NAME" \
        --multi_agent_user \
        --summary_user \
        --max_service_model_requests_per_task 30 \
        --output_dir "$OUTPUT_DIR" \
        --num_tasks "$NUM_tasks"

    echo "Completed: $scenario$scenario_number (easy mode)"
    echo ""
}

if [ "$FINAL_EVAL" = true ]; then
    # ==============================================================================
    # Final Evaluation Phase (2026.06.18 - 2026.06.25)
    # Only run the final evaluation scenarios: retail6, retail10, kitchen4, restaurant5, order2
    # ==============================================================================
    echo "=========================================="
    echo "Running Final Evaluation Scenarios"
    echo "=========================================="

    echo "=========================================="
    echo "Running Retail Scenarios (6, 10)"
    echo "=========================================="
    run_scenario_bg "retail" 6
    run_scenario_bg "retail" 10
    wait_for_scenarios

    echo "=========================================="
    echo "Running Kitchen Scenario (4)"
    echo "=========================================="
    run_scenario_bg "kitchen" 4
    wait_for_scenarios

    echo "=========================================="
    echo "Running Restaurant Scenario (5)"
    echo "=========================================="
    run_scenario_bg "restaurant" 5
    wait_for_scenarios

    echo "=========================================="
    echo "Running Order Scenario (2)"
    echo "=========================================="
    run_scenario_bg "order" 2
    wait_for_scenarios

else
    # ==============================================================================
    # Offline Testing Phase (2026.05.18 - 2026.06.25)
    # Run all offline testing scenarios
    # ==============================================================================

    if [ -z "$SCENARIO_FILTER" ] || [ "$SCENARIO_FILTER" = "retail" ]; then
        # Retail scenarios (1-10)
        echo "=========================================="
        echo "Running Retail Scenarios (1-10)"
        echo "=========================================="
        if [ -n "$SCENARIO_NUMBER_FILTER" ]; then
            run_scenario_bg "retail" "$SCENARIO_NUMBER_FILTER"
        else
            for i in $(seq 1 10); do
                run_scenario_bg "retail" $i
            done
        fi
        wait_for_scenarios
    fi

    if [ -z "$SCENARIO_FILTER" ] || [ "$SCENARIO_FILTER" = "kitchen" ]; then
        # Kitchen scenarios (1-4)
        echo "=========================================="
        echo "Running Kitchen Scenarios (1-4)"
        echo "=========================================="
        if [ -n "$SCENARIO_NUMBER_FILTER" ]; then
            run_scenario_bg "kitchen" "$SCENARIO_NUMBER_FILTER"
        else
            for i in $(seq 1 4); do
                run_scenario_bg "kitchen" $i
            done
        fi
        wait_for_scenarios
    fi

    if [ -z "$SCENARIO_FILTER" ] || [ "$SCENARIO_FILTER" = "restaurant" ]; then
        # Restaurant scenarios (1-5)
        echo "=========================================="
        echo "Running Restaurant Scenarios (1-5)"
        echo "=========================================="
        if [ -n "$SCENARIO_NUMBER_FILTER" ]; then
            run_scenario_bg "restaurant" "$SCENARIO_NUMBER_FILTER"
        else
            for i in $(seq 1 5); do
                run_scenario_bg "restaurant" $i
            done
        fi
        wait_for_scenarios
    fi

    if [ -z "$SCENARIO_FILTER" ] || [ "$SCENARIO_FILTER" = "order" ]; then
        # Order scenarios (1-2)
        echo "=========================================="
        echo "Running Order Scenarios (1-2)"
        echo "=========================================="
        if [ -n "$SCENARIO_NUMBER_FILTER" ]; then
            run_scenario_bg "order" "$SCENARIO_NUMBER_FILTER"
        else
            for i in $(seq 1 2); do
                run_scenario_bg "order" $i
            done
        fi
        wait_for_scenarios
    fi
fi

echo "=========================================="
echo "All scenarios completed!"
echo "=========================================="
echo ""
echo "Results saved to: results/$SERVICE_MODEL_NAME/"
echo ""
echo "To evaluate results, run:"
echo "  bash analysis_scripts/run_eval.sh"
