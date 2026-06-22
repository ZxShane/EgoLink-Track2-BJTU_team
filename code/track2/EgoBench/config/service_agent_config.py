"""
Service Agent Configuration Interface
======================================

This file configures the service agent model for the competition.
Participants should modify this file to use their preferred model for the service agent.

Default: Qwen3.5-397B-A17B (supports both API and local deployment)

Environment Variables Required:
- SERVICE_API_KEY: API key for the service model (or use API_KEY)
- SERVICE_API_BASE_URL: Base URL for the API endpoint (optional)
- VIDEO_MODE: "local" for local videos folder, "url" for public URLs (default: "local")

Environment Variables for Local Deployment:
- SERVICE_MODEL_NAME: Model name for local deployment
- SERVICE_API_BASE_URL: Your local model API endpoint

Environment Variables for Video URLs:
- VIDEO_URL_MAPPING: JSON string mapping video filenames to public URLs
                     Example: '{"retail1.mp4": "https://example.com/retail1.mp4"}'
"""

import os
import json
from openai import OpenAI

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _api_proxy_disabled():
    return os.environ.get("DISABLE_API_PROXY", "").lower() in {"1", "true", "yes", "on"}


def _openai_client(api_key, base_url):
    kwargs = {"api_key": api_key, "base_url": base_url}
    if _api_proxy_disabled():
        import httpx
        kwargs["http_client"] = httpx.Client(trust_env=False)
    return OpenAI(**kwargs)

# ==============================================================================
# SERVICE AGENT MODEL CONFIGURATION
# ==============================================================================

# Model name for service agent
# Default: Qwen3.5-397B-A17B
# You can change this to any model you prefer
SERVICE_MODEL_NAME = os.environ.get("SERVICE_MODEL_NAME", "Qwen3.5-397B-A17B")

# API Key for service model
# This should be set as an environment variable: export SERVICE_API_KEY="your-api-key"
# If not set, will fall back to API_KEY
SERVICE_API_KEY = os.environ.get("SERVICE_API_KEY", os.environ.get("API_KEY", ""))

# Base URL for the API endpoint
# Default: https://api.example.com/v1/chat/completions
# For local deployment, set this to your local API endpoint
SERVICE_API_BASE_URL = os.environ.get("SERVICE_API_BASE_URL", os.environ.get("LLM_API_BASE_URL", "https://api.example.com/v1"))

# Maximum tokens for service model responses
SERVICE_MAX_TOKENS = 32768

# Temperature for service model (0.0 - 2.0)
SERVICE_TEMPERATURE = 0.7

# Whether to enable thinking mode (if supported by the model)
SERVICE_ENABLE_THINKING = False

# Dedicated model for validating service drafts. This is harness-only; final
# result files still store only service outputs.
VALIDATOR_MODEL_NAME = os.environ.get("VALIDATOR_MODEL_NAME", "glm-5.2")


# ==============================================================================
# VISUAL GROUNDING MODEL CONFIGURATION
# ==============================================================================

# Optional dedicated visual grounding model for locating referenced objects.
# It uses an OpenAI-compatible API and falls back to the service API key when a
# dedicated grounding key is not provided.
GROUNDING_MODEL_NAME = os.environ.get("GROUNDING_MODEL_NAME", "qwen3-vl-plus")
GROUNDING_API_KEY = os.environ.get(
    "GROUNDING_API_KEY",
    os.environ.get("DASHSCOPE_API_KEY", os.environ.get("SERVICE_API_KEY", os.environ.get("API_KEY", "")))
)
GROUNDING_API_BASE_URL = os.environ.get("GROUNDING_API_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
GROUNDING_ENABLE_THINKING = os.environ.get("GROUNDING_ENABLE_THINKING", "false").lower() == "true"
GROUNDING_EXTRA_BODY_ENABLED = os.environ.get("GROUNDING_EXTRA_BODY_ENABLED", "false").lower() == "true"


# ==============================================================================
# VIDEO CONFIGURATION
# ==============================================================================

# Video storage mode: "local" or "url"
# "local" - Use videos from the local videos/ folder
# "url" - Use public URLs provided in VIDEO_URL_MAPPING
VIDEO_MODE = os.environ.get("VIDEO_MODE", "local")

# Base path for local videos
# Default: <EgoBench>/videos. Relative env values are resolved from project root
# rather than the shell's current working directory.
VIDEO_LOCAL_PATH = os.environ.get("VIDEO_LOCAL_PATH", "videos")
if not os.path.isabs(VIDEO_LOCAL_PATH):
    VIDEO_LOCAL_PATH = os.path.join(PROJECT_ROOT, VIDEO_LOCAL_PATH)

# Video URL mapping (for url mode)
# This is a dictionary mapping video filenames to their public URLs
# Example: {"retail1.mp4": "https://example.com/videos/retail1.mp4"}
def _load_video_url_mapping():
    """Load video URL mapping from environment variable."""
    video_mapping_env = os.environ.get("VIDEO_URL_MAPPING", "")
    if video_mapping_env:
        try:
            return json.loads(video_mapping_env)
        except json.JSONDecodeError:
            print("[Warning] Failed to parse VIDEO_URL_MAPPING, using empty mapping")
    return {}

VIDEO_URL_MAPPING = _load_video_url_mapping()

# Default video URL mapping (fallback for common scenarios)
# Participants should update these with their actual video URLs
DEFAULT_VIDEO_URL_MAPPING = {
    # Retail scenarios
    "retail1.mp4": "http://124.223.51.227:8081/retail1.mp4",
    "retail2.mp4": "http://124.223.51.227:8081/retail2.mp4",
    "retail3.mp4": "http://124.223.51.227:8081/retail3.mp4",
    "retail4.mp4": "http://124.223.51.227:8081/retail4.mp4",
    "retail5.mp4": "http://124.223.51.227:8081/retail5.mp4",
    "retail6.mp4": "http://124.223.51.227:8081/retail6.mp4",
    "retail7.mp4": "http://124.223.51.227:8081/retail7.mp4",
    "retail8.mp4": "http://124.223.51.227:8081/retail8.mp4",
    "retail9.mp4": "http://124.223.51.227:8081/retail9.mp4",
    "retail10.mp4": "http://124.223.51.227:8081/retail10.mp4",

    # Kitchen scenarios
    "kitchen1.mp4": "http://124.223.51.227:8081/kitchen1.mp4",
    "deep_fried.mp4": "http://124.223.51.227:8081/deep_fried.mp4",
    "Green Pepper Chicken.mp4": "http://124.223.51.227:8081/Green%20Pepper%20Chicken.mp4",
    "Green%20Pepper%20Chicken.mp4": "http://124.223.51.227:8081/Green%20Pepper%20Chicken.mp4",
    "dumplings.mp4": "http://124.223.51.227:8081/dumplings.mp4",

    # Restaurant scenarios
    "restaurant1.mp4": "http://124.223.51.227:8081/restaurant1.mp4",
    "restaurant2.mp4": "http://124.223.51.227:8081/restaurant2.mp4",
    "restaurant3.mp4": "http://124.223.51.227:8081/restaurant3.mp4",
    "restaurant4.mp4": "http://124.223.51.227:8081/restaurant4.mp4",
    "restaurant5.mp4": "http://124.223.51.227:8081/restaurant5.mp4",

    # Order scenarios
    "afrikana_annie_1.mp4": "http://124.223.51.227:8081/afrikana_annie_1.mp4",
    "annie_butcher_1.mp4": "http://124.223.51.227:8081/annie_butcher_1.mp4",
    "annie_meraki_1.mp4": "http://124.223.51.227:8081/annie_meraki_1.mp4",
    "annie_pauhana_1.mp4": "http://124.223.51.227:8081/annie_pauhana_1.mp4",
    "sunny_annie_1.mp4": "http://124.223.51.227:8081/sunny_annie_1.mp4",
    "afrikana_greek.mp4": "http://124.223.51.227:8081/afrikana_greek.mp4",
    "butcher_greek.mp4": "http://124.223.51.227:8081/butcher_greek.mp4",
    "greek_annie_1.mp4": "http://124.223.51.227:8081/greek_annie_1.mp4",
    "meraki_greek.mp4": "http://124.223.51.227:8081/meraki_greek.mp4",
    "pauhana_greek.mp4": "http://124.223.51.227:8081/pauhana_greek.mp4",
    "sunny_greek.mp4": "http://124.223.51.227:8081/sunny_greek.mp4",
}


def get_video_path(video_filename):
    """
    Get the video path or URL based on the configured VIDEO_MODE.

    Args:
        video_filename: The video filename (e.g., "retail1.mp4")

    Returns:
        str: The full path or URL to the video
    """
    if not video_filename:
        return video_filename

    if str(video_filename).startswith(("http://", "https://")):
        filename = os.path.basename(video_filename.split("?", 1)[0])
    else:
        filename = os.path.basename(str(video_filename))

    if VIDEO_MODE == "url":
        # Use public URLs
        return VIDEO_URL_MAPPING.get(
            filename,
            DEFAULT_VIDEO_URL_MAPPING.get(filename, video_filename)
        )
    else:
        # Use local videos folder
        if os.path.isabs(str(video_filename)):
            return str(video_filename)
        return os.path.join(VIDEO_LOCAL_PATH, filename)


# ==============================================================================
# SERVICE OUTPUT POST-PROCESSING
# ==============================================================================

def _contains_tool_call(value):
    """Return True when a decoded JSON value contains a tool-call object."""
    if isinstance(value, dict):
        if "tool_name" in value or "name" in value:
            return True
        return any(_contains_tool_call(v) for v in value.values())
    if isinstance(value, list):
        return any(_contains_tool_call(item) for item in value)
    return False


def _find_json_values(text):
    """Decode JSON values embedded in text without changing model semantics."""
    decoder = json.JSONDecoder()
    values = []
    for idx, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            value, end = decoder.raw_decode(text[idx:])
        except json.JSONDecodeError:
            continue
        values.append((idx, idx + end, value))
    return values


def _normalize_service_output(content):
    """
    Keep service output in exactly one format.

    If any JSON tool call is present, return only a compact JSON array/object and
    discard all surrounding natural language. Otherwise return concise text.
    """
    if content is None:
        return ""

    text = str(content).strip()
    if not text:
        return ""

    json_values = _find_json_values(text)
    if json_values:
        tool_values = [item for item in json_values if _contains_tool_call(item[2])]
        candidates = tool_values or json_values

        # Prefer the earliest, largest JSON value so a full array beats inner objects.
        start, end, value = sorted(candidates, key=lambda item: (item[0], -(item[1] - item[0])))[0]
        if isinstance(value, dict) and _contains_tool_call(value):
            value = [value]
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    # No JSON: normalize whitespace to keep the service answer short and clean.
    return " ".join(text.split())


# ==============================================================================
# API CALL FUNCTION
# ==============================================================================

def call_service_model(messages, max_retries=3, enable_thinking=None):
    """
    Call the service model with the given messages.

    Args:
        messages: List of message dictionaries with 'role' and 'content'
        max_retries: Maximum number of retry attempts
        enable_thinking: Whether to enable thinking mode (None = use default)

    Returns:
        tuple: (response_text, input_tokens, output_tokens)
    """
    import time
    import random
    import requests

    if enable_thinking is None:
        enable_thinking = SERVICE_ENABLE_THINKING

    BASE_DELAY = 10
    last_error = None

    for attempt in range(max_retries):
        try:
            client = _openai_client(SERVICE_API_KEY, SERVICE_API_BASE_URL)
            kwargs = {
                "model": SERVICE_MODEL_NAME,
                "messages": messages,
                "extra_body": {"enable_thinking": enable_thinking}
            }
            completion = client.chat.completions.create(**kwargs)
            content = _normalize_service_output(completion.choices[0].message.content)

            # Extract token information
            input_tokens = 0
            output_tokens = 0
            if hasattr(completion, 'usage') and completion.usage:
                input_tokens = getattr(completion.usage, 'prompt_tokens', 0) or 0
                output_tokens = getattr(completion.usage, 'completion_tokens', 0) or 0

            return content, input_tokens, output_tokens

        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                wait_time = (BASE_DELAY * (2 ** attempt)) + random.uniform(0, 1)
                print(f"[Service Model Retry] Attempt {attempt + 1}/{max_retries} failed: {str(e)}. Retrying in {wait_time:.2f}s...")
                time.sleep(wait_time)
            else:
                print(f"[Service Model Error] Failed after {max_retries} attempts: {str(e)}")
                return f"Error: {str(e)}", 0, 0


def call_grounding_model(messages, max_retries=3, enable_thinking=None):
    """
    Call the dedicated visual grounding model.

    This is intended for bbox/referring-expression grounding and is separate from
    the service agent model so it can use qwen3-vl through DashScope.
    """
    import time
    import random

    if enable_thinking is None:
        enable_thinking = GROUNDING_ENABLE_THINKING

    BASE_DELAY = 10
    for attempt in range(max_retries):
        try:
            client = _openai_client(GROUNDING_API_KEY, GROUNDING_API_BASE_URL)
            kwargs = {
                "model": GROUNDING_MODEL_NAME,
                "messages": messages,
            }
            if GROUNDING_EXTRA_BODY_ENABLED:
                kwargs["extra_body"] = {"enable_thinking": enable_thinking}
            completion = client.chat.completions.create(**kwargs)
            content = completion.choices[0].message.content or ""

            input_tokens = 0
            output_tokens = 0
            if hasattr(completion, 'usage') and completion.usage:
                input_tokens = getattr(completion.usage, 'prompt_tokens', 0) or 0
                output_tokens = getattr(completion.usage, 'completion_tokens', 0) or 0

            return content, input_tokens, output_tokens

        except Exception as e:
            if attempt < max_retries - 1:
                wait_time = (BASE_DELAY * (2 ** attempt)) + random.uniform(0, 1)
                print(f"[Grounding Model Retry] Attempt {attempt + 1}/{max_retries} failed: {str(e)}. Retrying in {wait_time:.2f}s...")
                time.sleep(wait_time)
            else:
                print(f"[Grounding Model Error] Failed after {max_retries} attempts: {str(e)}")
                return f"Error: {str(e)}", 0, 0


# ==============================================================================
# CONFIGURATION VALIDATION
# ==============================================================================

def validate_config():
    """
    Validate the service agent configuration.

    Returns:
        tuple: (is_valid, error_message)
    """
    if not SERVICE_API_KEY:
        return False, "SERVICE_API_KEY (or API_KEY) environment variable is not set. Please set it before running."

    if not SERVICE_API_BASE_URL:
        return False, "SERVICE_API_BASE_URL is not configured."

    if VIDEO_MODE not in ["local", "url"]:
        return False, f"Invalid VIDEO_MODE: {VIDEO_MODE}. Must be 'local' or 'url'."

    if VIDEO_MODE == "url" and not VIDEO_URL_MAPPING:
        return False, "VIDEO_MODE is 'url' but VIDEO_URL_MAPPING is not set."

    return True, None


def print_config():
    """Print current service agent configuration."""
    print(f"Service Agent Configuration:")
    print(f"  Model: {SERVICE_MODEL_NAME}")
    print(f"  API Base URL: {SERVICE_API_BASE_URL}")
    print(f"  Max Tokens: {SERVICE_MAX_TOKENS}")
    print(f"  Temperature: {SERVICE_TEMPERATURE}")
    print(f"  Thinking Mode: {SERVICE_ENABLE_THINKING}")
    print(f"  Grounding Model: {GROUNDING_MODEL_NAME}")
    print(f"  Grounding API Base URL: {GROUNDING_API_BASE_URL}")
    print(f"  Video Mode: {VIDEO_MODE}")
    if VIDEO_MODE == "local":
        print(f"  Video Local Path: {VIDEO_LOCAL_PATH}")
    else:
        print(f"  Video URL Mapping: {len(VIDEO_URL_MAPPING)} entries")


if __name__ == "__main__":
    # Test configuration
    is_valid, error_msg = validate_config()
    if is_valid:
        print_config()
    else:
        print(f"Configuration Error: {error_msg}")
