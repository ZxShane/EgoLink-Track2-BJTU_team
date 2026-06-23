import os
import json
import time
import argparse
import sys
import concurrent.futures
import re
from urllib.parse import urlparse, unquote

# Add the project root directory to Python's module search path
current_file_path = os.path.abspath(__file__)
run_dir = os.path.dirname(current_file_path)
project_root = os.path.dirname(run_dir)
sys.path.insert(0, os.path.abspath(project_root))

# 1. Import initialization data
from tools.retail.retail_db import RetailDB
from tools.retail.retail_init import retail_init_data1, retail_init_data2, retail_init_data3, retail_init_data4, retail_init_data5, retail_init_data6, retail_init_data7, retail_init_data8, retail_init_data9, retail_init_data10
from tools.kitchen.kitchen_db import KitchenDB
from tools.kitchen.kitchen_init import kitchen_init_data
from tools.restaurant.restaurant_db import RestaurantDB
from tools.restaurant.restaurant_init import restaurant_init_data, restaurant_init_data5
from tools.order.order_db import OrderDB
from tools.order.order_init import order_init_data
from run.prompts import (
    USER_TEXT_ONLY_PROMPT_EASY,
    SERVICE_AGENT_PROMPT_BASE,
    USER_TURN_SUMMARY_PROMPT,
    SERVICE_VISUAL_GROUNDING_PROMPT,
    SERVICE_KEYFRAME_SELECTION_PROMPT,
    SERVICE_TIMELINE_SELECTION_PROMPT,
    SERVICE_QWEN_VL_BBOX_PROMPT,
    SERVICE_CANDIDATE_RERANK_PROMPT,
)
from run.utils import (
    call_llm,
    execute_tool,
    check_tool_call,
    check_user_contradiction,
    build_message_with_image
)
from config.service_agent_config import (
    get_video_path,
    VIDEO_MODE,
    SERVICE_MODEL_NAME,
    VALIDATOR_MODEL_NAME,
    call_grounding_model,
    GROUNDING_MODEL_NAME,
)


SERVICE_VALIDATION_MAX_RETRIES = 4
DEFAULT_MAX_SERVICE_MODEL_REQUESTS_PER_TASK = 30


_RETAIL_VISUAL_STOPWORDS = {
    "the", "and", "for", "with", "from", "this", "that", "please", "product",
    "item", "bottle", "label", "price", "third", "second", "first", "left",
    "right", "middle", "front", "back", "wine", "red", "white", "black",
    "green", "gold", "silver", "customer", "user", "cart", "buy", "add",
    "shelf", "circular", "column", "vertical", "pointed", "looking", "alice",
    "area", "section", "dark", "label", "bottle", "specific", "finger",
    "confirm", "could", "please", "price", "tag",
}

_RETAIL_STABLE_TERMS = {
    "moscato", "cabernet", "sauvignon", "merlot", "chardonnay", "riesling",
    "pinot", "noir", "shiraz", "malbec", "marsala", "zinfandel", "brandy",
    "vodka", "whiskey", "bourbon", "rum", "gin", "tequila", "beer", "cider",
    "rose", "rosé", "reserve", "estate", "valley", "oyster", "brookside",
    "napa", "zonin", "olivier", "riumi", "mystere", "lerpin", "kressmann",
}


def _extract_json_object(text):
    """Best-effort JSON object extraction for visual grounding output."""
    if not text:
        return {}
    text = text.strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _extract_json_values(text):
    """Extract top-level JSON objects/arrays embedded in loose text."""
    values = []
    if not text:
        return values
    stack = []
    start = None
    in_string = False
    escape = False
    for idx, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch in "{[":
            if not stack:
                start = idx
            stack.append(ch)
        elif ch in "}]":
            if not stack:
                continue
            opener = stack[-1]
            if (opener, ch) not in (("{", "}"), ("[", "]")):
                stack = []
                start = None
                continue
            stack.pop()
            if not stack and start is not None:
                snippet = text[start:idx + 1]
                try:
                    values.append(json.loads(snippet))
                except json.JSONDecodeError:
                    pass
                start = None
    return values


def _norm_catalog_name(value):
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _catalog_name_tokens(value):
    return {
        token for token in re.findall(r"[a-z0-9']+", _norm_catalog_name(value))
        if len(token) > 2 and token not in _RETAIL_VISUAL_STOPWORDS
    }


def _catalog_name_matches(target_product, candidate_product):
    """Match OCR/short visual names against canonical catalog product names."""
    target_norm = _norm_catalog_name(target_product)
    candidate_norm = _norm_catalog_name(candidate_product)
    if not target_norm or not candidate_norm:
        return False
    if target_norm == candidate_norm:
        return True
    if target_norm in candidate_norm or candidate_norm in target_norm:
        return True

    target_tokens = _catalog_name_tokens(target_norm)
    candidate_tokens = _catalog_name_tokens(candidate_norm)
    if not target_tokens or not candidate_tokens:
        return False
    if target_tokens.issubset(candidate_tokens) and len(target_tokens) >= 2:
        return True
    if candidate_tokens.issubset(target_tokens) and len(candidate_tokens) >= 2:
        return True
    return len(target_tokens & candidate_tokens) >= 2


def _catalog_membership_matches(target_product, candidate_product):
    """
    Strict match for verifying that a specific target product appears in a
    returned product list. Shared generic terms such as "sauvignon blanc" are
    not enough to prove membership.
    """
    target_norm = _norm_catalog_name(target_product)
    candidate_norm = _norm_catalog_name(candidate_product)
    if not target_norm or not candidate_norm:
        return False
    if target_norm == candidate_norm:
        return True
    if target_norm in candidate_norm or candidate_norm in target_norm:
        return True

    target_tokens = _catalog_name_tokens(target_norm)
    candidate_tokens = _catalog_name_tokens(candidate_norm)
    if not target_tokens or not candidate_tokens:
        return False
    if target_tokens.issubset(candidate_tokens) and len(target_tokens) >= 2:
        return True
    if candidate_tokens.issubset(target_tokens) and len(candidate_tokens) >= 2:
        distinctive = candidate_tokens - {"sauvignon", "blanc", "merlot", "chardonnay", "pinot", "noir", "cabernet"}
        return bool(distinctive)
    return False


def _looks_like_clarification_request(text):
    """Allow only short user-intent clarifications as non-tool first replies."""
    normalized = re.sub(r"\s+", " ", str(text or "").strip())
    if not normalized:
        return False
    if len(normalized) > 260:
        return False
    lower = normalized.lower()
    clarification_markers = (
        "which", "what", "who", "where", "when", "could you clarify",
        "please clarify", "do you mean", "which one", "need your",
        "please specify", "can you confirm", "confirm which"
    )
    return "?" in normalized and any(marker in lower for marker in clarification_markers)


def _non_final_tool_use_rejection(agent_reply):
    return {
        "role": "user",
        "content": (
            "Internal format check rejected the previous draft before it was accepted.\n"
            "The user's current request is not yet grounded by a tool result in this service turn. "
            "If any available tool can progress the next unfinished step, output ONLY a strict JSON tool-call array now. "
            "Do not output natural-language progress reports, tentative conclusions, OCR summaries, or verification placeholders. "
            "Use natural language only for a short clarification question when no tool can progress because required user intent is missing.\n"
            f"Rejected draft:\n{agent_reply}"
        )
    }


def _tool_params(call):
    params = call.get("parameters", {})
    return params if isinstance(params, dict) else {}


def _canonical_tool_calls(content):
    is_tool, tool_call_obj = check_tool_call(content)
    if not is_tool:
        return None
    calls = tool_call_obj if isinstance(tool_call_obj, list) else ([tool_call_obj] if tool_call_obj else [])
    normalized_calls = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        normalized_calls.append({
            "tool_name": str(call.get("tool_name", "")),
            "parameters": _tool_params(call),
        })
    if not normalized_calls:
        return None
    normalized_calls.sort(key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True))
    return json.dumps(normalized_calls, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _duplicate_instruction_guard(local_service_history, agent_reply, current_target_product="", scenario=""):
    current_signature = _canonical_tool_calls(agent_reply)
    if not current_signature:
        current_text = re.sub(r"\s+", " ", str(agent_reply or "").strip())
        if not current_text:
            return None
        current_signature = f"text:{current_text}"

    for message in reversed(local_service_history):
        if message.get("role") != "assistant":
            continue
        previous_signature = _canonical_tool_calls(message.get("content", ""))
        if not previous_signature:
            previous_text = re.sub(r"\s+", " ", str(message.get("content", "")).strip())
            previous_signature = f"text:{previous_text}" if previous_text else None
        if previous_signature and previous_signature == current_signature:
            completed_search_context = _completed_country_price_search_context(
                local_service_history,
                current_target_product,
            )
            if completed_search_context:
                return {
                    "valid": False,
                    "reason": completed_search_context["reason"],
                    "suggestion": completed_search_context["suggestion"],
                    "rule_id": "duplicate_instruction_with_completed_context",
                    "scenario": _validator_scenario(scenario),
                }
            return {
                "valid": False,
                "reason": "Repeated service instruction detected; the same tool call or response has already been produced.",
                "suggestion": "必须更换指令，不能重复同一工具和参数；使用已有结果继续下一步。",
                "rule_id": "duplicate_instruction",
                "scenario": _validator_scenario(scenario),
            }
    return None


def _latest_price_range_products(local_service_history):
    for idx in range(len(local_service_history) - 2, -1, -1):
        current = local_service_history[idx]
        nxt = local_service_history[idx + 1]
        if current.get("role") != "assistant" or nxt.get("role") != "user":
            continue
        is_tool, tool_call_obj = check_tool_call(current.get("content", ""))
        if not is_tool:
            continue
        calls = tool_call_obj if isinstance(tool_call_obj, list) else ([tool_call_obj] if tool_call_obj else [])
        result_text = str(nxt.get("content", ""))
        if "Tool execution result:" not in result_text:
            continue
        result_values = _extract_json_values(result_text.split("Tool execution result:", 1)[1])
        for call, result in zip(calls, result_values):
            if not isinstance(call, dict) or not isinstance(result, dict):
                continue
            if str(call.get("tool_name", "")) != "find_products_by_price_range":
                continue
            product_names = result.get("product_names", [])
            if isinstance(product_names, list):
                return [str(name) for name in product_names]
    return []


def _completed_country_price_search_context(local_service_history, target_product=""):
    target_product = target_product or _latest_verified_target_from_history(local_service_history)
    matches = _country_lookup_matches(local_service_history, target_product)
    price_products = _latest_price_range_products(local_service_history)
    if not matches or not price_products:
        return None

    match = matches[-1]
    country_products = [str(name) for name in match.get("product_names", [])]
    country_norms = {_norm_catalog_name(name) for name in country_products}
    target_norm = _norm_catalog_name(target_product)
    cheaper_same_country = [
        name for name in price_products
        if _norm_catalog_name(name) in country_norms and _norm_catalog_name(name) != target_norm
    ]
    return {
        "reason": (
            f'{target_product} is verified as {match["country"]}. '
            f'Origin product list: [{", ".join(country_products[:12])}'
            f'{"..." if len(country_products) > 12 else ""}]. '
            f'Lower-price product list: [{", ".join(price_products)}].'
        ),
        "suggestion": (
            "已有产地商品列表和低价商品列表；不能重复同一工具和参数。"
            f"两个列表的交集为: [{', '.join(cheaper_same_country)}]. "
            "如果当前用户还要求继续操作，必须基于这个交集执行下一步工具调用；"
            "如果用户用常识纠正国家，必须用工具验证结果纠正该前提，并继续执行当前请求。"
        )
    }


def _retail_tool_state(local_service_history, grounding_context="", current_user_reply=""):
    target_product = _current_target_product(local_service_history, grounding_context, current_user_reply)
    if not target_product:
        return {}

    verified_matches = _country_lookup_matches(local_service_history, target_product)
    mismatches = _country_lookup_mismatches(local_service_history, target_product)
    price_products = _latest_price_range_products(local_service_history)
    state = {
        "target_product": target_product,
        "verified_origin": "",
        "verified_origin_product_names": [],
        "disproved_origins": [],
        "lower_price_product_names": price_products,
        "same_origin_lower_price_candidates": [],
    }

    seen_disproved = set()
    for mismatch in mismatches:
        country = str(mismatch.get("country") or "")
        norm = _norm_catalog_name(country)
        if country and norm not in seen_disproved:
            seen_disproved.add(norm)
            state["disproved_origins"].append(country)

    if verified_matches:
        match = verified_matches[-1]
        country_products = [str(name) for name in match.get("product_names", [])]
        country_norms = {_norm_catalog_name(name) for name in country_products}
        target_norm = _norm_catalog_name(target_product)
        state["verified_origin"] = str(match.get("country") or "")
        state["verified_origin_product_names"] = country_products
        state["same_origin_lower_price_candidates"] = [
            name for name in price_products
            if _norm_catalog_name(name) in country_norms and _norm_catalog_name(name) != target_norm
        ]

    return state


def _retail_tool_state_context(local_service_history, grounding_context="", current_user_reply=""):
    state = _retail_tool_state(local_service_history, grounding_context, current_user_reply)
    if not state:
        return ""
    useful = any(state.get(key) for key in (
        "verified_origin",
        "disproved_origins",
        "lower_price_product_names",
        "same_origin_lower_price_candidates",
    ))
    if not useful:
        return ""
    return (
        "Retail catalog state derived from accepted tool results:\n"
        + json.dumps(state, ensure_ascii=False, indent=2)
        + "\nThis state is authoritative for catalog facts. If the user or visual evidence conflicts with it, "
        "keep the user's requested action/intent but use this tool-verified state for facts. "
        "For same-country/origin requests, use verified_origin_product_names as the candidate set; "
        "do not switch back to disproved_origins. If same_origin_lower_price_candidates is non-empty "
        "and the user asks to add/select the cheaper same-origin item, continue with tool calls on those candidates."
    )


def _verified_origin_context_message(country, product_names, target_product):
    product_names = [str(name) for name in (product_names or [])]
    product_list = ", ".join(product_names)
    return (
        f"{target_product} has been verified as {country} because it appears in the "
        f"tool-returned product_names list. Tool-returned product_names for {country}: "
        f"[{product_list}]. You MUST treat this list as the only candidate set for "
        "same-country/origin reasoning. Continue the task using ONLY this tool-returned "
        "list plus later tool results; do not use common knowledge, brand knowledge, "
        "visual guesses, or any previously disproved country."
    )


_VALIDATOR_EXPERIENCE_CACHE = {}
_VALIDATOR_PROMPT_CACHE = {}
_SUPPORTED_VALIDATOR_SCENARIOS = {"retail", "kitchen", "restaurant", "order"}


def _validator_scenario(scenario):
    scenario = str(scenario or "").strip().lower()
    return scenario if scenario in _SUPPORTED_VALIDATOR_SCENARIOS else "retail"


def _validator_experience_path(scenario):
    scenario = _validator_scenario(scenario)
    env_key = f"{scenario.upper()}_EXPERIENCE_JSONL"
    default_path = os.path.join(project_root, "validator_experiences", f"{scenario}_experience.jsonl")
    if scenario == "retail" and not os.path.exists(default_path):
        default_path = os.path.join(project_root, "retail_experience.jsonl")
    return os.environ.get(env_key, default_path)


def _load_validator_experience(scenario):
    scenario = _validator_scenario(scenario)
    if scenario in _VALIDATOR_EXPERIENCE_CACHE:
        return _VALIDATOR_EXPERIENCE_CACHE[scenario]

    path = _validator_experience_path(scenario)
    experience = {}
    if not os.path.exists(path):
        _VALIDATOR_EXPERIENCE_CACHE[scenario] = experience
        return experience

    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                row = json.loads(line)
                row_id = row.get("id")
                if row_id:
                    experience[row_id] = row
    except Exception as exc:
        print(f"[Validator Experience] Failed to load {path}: {exc}")
        experience = {}

    print(f"[Validator Experience] Loaded {len(experience)} {scenario} rule(s) from {path}")
    _VALIDATOR_EXPERIENCE_CACHE[scenario] = experience
    return experience


def _validator_prompt_path(scenario):
    scenario = _validator_scenario(scenario)
    env_key = f"{scenario.upper()}_VALIDATOR_PROMPT"
    default_path = os.path.join(project_root, "validator_prompts", f"{scenario}_validator_prompt.md")
    return os.environ.get(env_key, default_path)


def _load_validator_prompt(scenario):
    scenario = _validator_scenario(scenario)
    if scenario in _VALIDATOR_PROMPT_CACHE:
        return _VALIDATOR_PROMPT_CACHE[scenario]

    path = _validator_prompt_path(scenario)
    prompt = ""
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                prompt = f.read().strip()
        except Exception as exc:
            print(f"[Validator Prompt] Failed to load {path}: {exc}")
            prompt = ""
    if prompt:
        print(f"[Validator Prompt] Loaded {scenario} prompt from {path}")
    _VALIDATOR_PROMPT_CACHE[scenario] = prompt
    return prompt


def _format_experience_decision(scenario, experience_id, **facts):
    experience = _load_validator_experience(scenario).get(experience_id, {})
    response = experience.get("validator_response", {}) if isinstance(experience, dict) else {}
    facts = {key: ("" if value is None else value) for key, value in facts.items()}

    def render(template, fallback=""):
        try:
            return str(template or fallback).format(**facts)
        except Exception:
            return fallback

    return {
        "valid": bool(response.get("valid", False)),
        "reason": facts.get("reason", "") or render(response.get("reason_template"), ""),
        "suggestion": facts.get("suggestion", "") or render(response.get("suggestion_template"), ""),
        "experience_id": experience_id,
        "scenario": _validator_scenario(scenario),
    }


def _truncate_for_validator(value, limit=1200):
    text = str(value or "")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        return text[:limit] + "...[truncated]"
    return text


def _compact_validator_history(local_service_history, limit=10):
    compact = []
    for message in (local_service_history or [])[-limit:]:
        compact.append({
            "role": message.get("role", ""),
            "content": _truncate_for_validator(message.get("content", ""), 1000),
        })
    return compact


def _extract_validator_json(text):
    values = _extract_json_values(text or "")
    for value in values:
        if isinstance(value, dict):
            return value
    return {}


def _call_llm_validator(
    scenario,
    validator_prompt,
    experience,
    user_instruction,
    current_user_reply,
    local_service_history,
    grounding_context,
    agent_reply,
    tool_descriptions="",
):
    """
    Use the dedicated validator model as an internal harness check.

    The model is allowed to apply only the loaded scenario experience rules. This
    keeps validator behavior narrow: if no experience rule clearly matches, the
    service draft must pass through unchanged.
    """
    if not validator_prompt or not experience:
        return {"valid": True, "reason": "no validator prompt/experience loaded", "suggestion": ""}, 0, 0

    experience_rows = list(experience.values())
    payload = {
        "scenario": _validator_scenario(scenario),
        "allowed_experience_ids": [row.get("id") for row in experience_rows if row.get("id")],
        "experience_rules": experience_rows,
        "user_instruction": _truncate_for_validator(user_instruction, 1800),
        "current_user_reply": _truncate_for_validator(current_user_reply, 1200),
        "grounding_context": _truncate_for_validator(grounding_context, 1600),
        "recent_service_history": _compact_validator_history(local_service_history, 10),
        "draft_service_output": _truncate_for_validator(agent_reply, 1800),
        "tool_descriptions": _truncate_for_validator(tool_descriptions, 1600),
    }
    messages = [
        {
            "role": "system",
            "content": (
                validator_prompt
                + "\n\nYou are an internal service-output validator. "
                "You must use the configured scenario experience rules only. "
                "If no listed experience rule clearly matches the draft, return valid=true. "
                "Do not solve the user task. Do not suggest concrete tool names unless the matched experience explicitly says so. "
                "Return ONLY compact JSON with keys: valid, reason, suggestion, experience_id."
            )
        },
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        }
    ]
    reply, input_tok, output_tok = call_llm(
        messages,
        agent_type="service",
        service_model_name=VALIDATOR_MODEL_NAME,
        enable_thinking=False,
    )
    decision = _extract_validator_json(reply)
    if not isinstance(decision, dict):
        decision = {}

    allowed_ids = set(payload["allowed_experience_ids"])
    experience_id = str(decision.get("experience_id") or "").strip()
    valid = bool(decision.get("valid", True))

    # Guard against validator-model hallucination: it may reject only by citing
    # a loaded experience rule. Otherwise the draft is treated as ordinary.
    if not valid and experience_id not in allowed_ids:
        return {
            "valid": True,
            "reason": "validator did not cite a loaded experience rule; ordinary step allowed",
            "suggestion": "",
            "validator_raw": _truncate_for_validator(reply, 500),
        }, input_tok, output_tok

    if not valid and experience_id == "country_origin_already_verified_repeat":
        is_tool, tool_call_obj = check_tool_call(agent_reply)
        calls = tool_call_obj if isinstance(tool_call_obj, list) else ([tool_call_obj] if tool_call_obj else [])
        has_country_lookup = any(
            isinstance(call, dict)
            and str(call.get("tool_name", "")) == "find_products_by_country_of_origin"
            for call in calls
        )
        if not has_country_lookup:
            return {
                "valid": True,
                "reason": "country-origin repeat rule applies only to further country-origin lookups; ordinary downstream tool step allowed",
                "suggestion": "",
                "validator_raw": _truncate_for_validator(reply, 500),
            }, input_tok, output_tok

    if not valid and experience_id in {
        "country_origin_absent_from_result",
        "overbroad_tool_enumeration_when_filter_result_available",
    }:
        target_product = _current_target_product(local_service_history, grounding_context, current_user_reply)
        verified_matches = _country_lookup_matches(local_service_history, target_product)
        if verified_matches:
            verified_match = verified_matches[-1]
            verified_names = {
                _norm_catalog_name(name)
                for name in verified_match.get("product_names", [])
                if _norm_catalog_name(name)
            }
            is_tool, tool_call_obj = check_tool_call(agent_reply)
            calls = tool_call_obj if isinstance(tool_call_obj, list) else ([tool_call_obj] if tool_call_obj else [])
            country_lookup_calls = [
                call for call in calls
                if isinstance(call, dict)
                and str(call.get("tool_name", "")) == "find_products_by_country_of_origin"
            ]
            product_params = [
                _norm_catalog_name(_tool_params(call).get("product_name"))
                for call in calls
                if isinstance(call, dict) and _tool_params(call).get("product_name")
            ]
            product_params_in_verified_list = (
                bool(product_params)
                and all(product_name in verified_names for product_name in product_params)
            )
            if not country_lookup_calls and (
                product_params_in_verified_list
                or any(
                    isinstance(call, dict)
                    and str(call.get("tool_name", "")) == "find_products_by_price_range"
                    for call in calls
                )
            ):
                return {
                    "valid": True,
                    "reason": (
                        f"{target_product} is already verified as {verified_match['country']}; "
                        "the draft is an ordinary downstream tool step based on verified tool evidence"
                    ),
                    "suggestion": "",
                    "validator_raw": _truncate_for_validator(reply, 500),
                }, input_tok, output_tok

    return {
        "valid": valid,
        "reason": _truncate_for_validator(decision.get("reason", ""), 500),
        "suggestion": _truncate_for_validator(decision.get("suggestion", ""), 300),
        "experience_id": experience_id,
        "scenario": _validator_scenario(scenario),
    }, input_tok, output_tok


def _latest_verified_target_from_history(local_service_history):
    """Find the original visual target product resolved before country filtering starts."""
    pre_country_target = ""
    seen_country_lookup = False
    for idx in range(len(local_service_history) - 1):
        current = local_service_history[idx]
        nxt = local_service_history[idx + 1]
        if current.get("role") != "assistant" or nxt.get("role") != "user":
            continue
        is_tool, tool_call_obj = check_tool_call(current.get("content", ""))
        if not is_tool:
            continue
        calls = tool_call_obj if isinstance(tool_call_obj, list) else ([tool_call_obj] if tool_call_obj else [])
        if any(isinstance(call, dict) and str(call.get("tool_name", "")) == "find_products_by_country_of_origin" for call in calls):
            seen_country_lookup = True
        if seen_country_lookup:
            continue
        result_text = str(nxt.get("content", ""))
        if "Tool execution result:" not in result_text:
            continue
        result_values = _extract_json_values(result_text.split("Tool execution result:", 1)[1])
        for call, result in zip(calls, result_values):
            if not isinstance(call, dict) or not isinstance(result, dict):
                continue
            if str(call.get("tool_name", "")) not in {"get_price", "get_category"}:
                continue
            products = result.get("products", [])
            if isinstance(products, list) and products:
                names = [
                    product.get("product_name")
                    for product in products
                    if isinstance(product, dict) and product.get("product_name")
                ]
                if names:
                    pre_country_target = max(names, key=len)
            product_name = _tool_params(call).get("product_name")
            if product_name:
                pre_country_target = str(product_name)
    if pre_country_target:
        return pre_country_target

    for idx in range(len(local_service_history) - 2, -1, -1):
        current = local_service_history[idx]
        nxt = local_service_history[idx + 1]
        if current.get("role") != "assistant" or nxt.get("role") != "user":
            continue
        is_tool, tool_call_obj = check_tool_call(current.get("content", ""))
        if not is_tool:
            continue
        calls = tool_call_obj if isinstance(tool_call_obj, list) else ([tool_call_obj] if tool_call_obj else [])
        result_text = str(nxt.get("content", ""))
        if "Tool execution result:" not in result_text:
            continue
        result_values = _extract_json_values(result_text.split("Tool execution result:", 1)[1])
        for call, result in zip(calls, result_values):
            if not isinstance(call, dict) or not isinstance(result, dict):
                continue
            if str(call.get("tool_name", "")) not in {"get_price", "get_category"}:
                continue
            products = result.get("products", [])
            if isinstance(products, list) and products:
                names = [
                    product.get("product_name")
                    for product in products
                    if isinstance(product, dict) and product.get("product_name")
                ]
                if names:
                    return max(names, key=len)
            product_name = _tool_params(call).get("product_name")
            if product_name:
                return str(product_name)
    return ""


def _target_product_from_grounding_context(grounding_context, current_user_reply=""):
    """Infer the current visual target from prepared text-only evidence."""
    grounding = _extract_json_object(grounding_context or "")
    if not grounding:
        return ""

    summary = str(grounding.get("visual_target_summary") or "")
    reply_norm = _norm_catalog_name(current_user_reply)
    summary_norm = _norm_catalog_name(summary)

    candidates = []
    for item in grounding.get("ocr_content", []):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        target = str(item.get("target") or "").strip()
        if not text:
            continue
        candidate = re.sub(r"\s+", " ", text.replace(";", " ").replace(",", " ")).strip()
        if candidate:
            candidates.append({
                "name": candidate,
                "target": target,
                "source": f"{target} {candidate}",
            })

    if not candidates:
        return ""

    best_name = ""
    best_score = -1
    for candidate in candidates:
        name = candidate["name"]
        source_norm = _norm_catalog_name(candidate["source"])
        name_tokens = [
            token for token in re.findall(r"[a-z0-9']+", _norm_catalog_name(name))
            if len(token) > 2 and token not in _RETAIL_VISUAL_STOPWORDS
        ]
        target_tokens = [
            token for token in re.findall(r"[a-z0-9']+", _norm_catalog_name(candidate["target"]))
            if len(token) > 2 and token not in _RETAIL_VISUAL_STOPWORDS
        ]

        score = 0
        score += sum(3 for token in name_tokens if token in summary_norm)
        score += sum(2 for token in target_tokens if token in reply_norm)
        score += sum(1 for token in target_tokens if token in summary_norm)
        if "gold cap" in reply_norm and ("gold" in source_norm or "gold" in summary_norm and any(token in summary_norm for token in name_tokens)):
            score += 5
        if "left" in reply_norm and "left" in source_norm:
            score += 4
        if "right" in reply_norm and "right" in source_norm:
            score += 4

        if score > best_score:
            best_score = score
            best_name = name

    return best_name if best_score > 0 else ""


def _catalog_product_names_from_history(local_service_history):
    names = []
    for idx in range(len(local_service_history) - 1):
        current = local_service_history[idx]
        nxt = local_service_history[idx + 1]
        if current.get("role") != "assistant" or nxt.get("role") != "user":
            continue
        is_tool, tool_call_obj = check_tool_call(current.get("content", ""))
        if not is_tool:
            continue
        calls = tool_call_obj if isinstance(tool_call_obj, list) else ([tool_call_obj] if tool_call_obj else [])
        result_text = str(nxt.get("content", ""))
        if "Tool execution result:" not in result_text:
            continue
        result_values = _extract_json_values(result_text.split("Tool execution result:", 1)[1])
        for call, result in zip(calls, result_values):
            if not isinstance(call, dict) or not isinstance(result, dict):
                continue
            if str(call.get("tool_name", "")) not in {"get_price", "get_category"}:
                continue
            for product in result.get("products", []):
                if isinstance(product, dict) and product.get("product_name"):
                    names.append(str(product["product_name"]))
    return names


def _canonicalize_target_product(target_product, local_service_history):
    target_norm = _norm_catalog_name(target_product)
    if not target_norm:
        return target_product

    target_tokens = {
        token for token in re.findall(r"[a-z0-9']+", target_norm)
        if len(token) > 2 and token not in _RETAIL_VISUAL_STOPWORDS
    }
    best_name = ""
    best_score = 0
    for name in _catalog_product_names_from_history(local_service_history):
        name_norm = _norm_catalog_name(name)
        if not name_norm:
            continue
        name_tokens = {
            token for token in re.findall(r"[a-z0-9']+", name_norm)
            if len(token) > 2 and token not in _RETAIL_VISUAL_STOPWORDS
        }
        score = 0
        if name_norm == target_norm:
            score += 100
        if name_norm in target_norm or target_norm in name_norm:
            score += 20
        score += 3 * len(target_tokens & name_tokens)
        if score > best_score:
            best_score = score
            best_name = name

    return best_name if best_score >= 6 else target_product


def _current_target_product(local_service_history, grounding_context="", current_user_reply=""):
    target_product = (
        _target_product_from_grounding_context(grounding_context, current_user_reply)
        or _latest_verified_target_from_history(local_service_history)
    )
    return _canonicalize_target_product(target_product, local_service_history)


def _country_lookup_mismatches(local_service_history, target_product):
    """Return countries whose reverse lookup results exclude the target product."""
    target_norm = _norm_catalog_name(target_product)
    if not target_norm:
        return []

    mismatches = []
    for idx in range(len(local_service_history) - 1):
        current = local_service_history[idx]
        nxt = local_service_history[idx + 1]
        if current.get("role") != "assistant" or nxt.get("role") != "user":
            continue
        is_tool, tool_call_obj = check_tool_call(current.get("content", ""))
        if not is_tool:
            continue
        calls = tool_call_obj if isinstance(tool_call_obj, list) else ([tool_call_obj] if tool_call_obj else [])
        result_text = str(nxt.get("content", ""))
        if "Tool execution result:" not in result_text:
            continue
        result_values = _extract_json_values(result_text.split("Tool execution result:", 1)[1])
        for call, result in zip(calls, result_values):
            if not isinstance(call, dict) or not isinstance(result, dict):
                continue
            if str(call.get("tool_name", "")) != "find_products_by_country_of_origin":
                continue
            country = _tool_params(call).get("country")
            product_names = result.get("product_names", [])
            if not isinstance(product_names, list):
                continue
            if not any(_catalog_membership_matches(target_product, name) for name in product_names):
                mismatches.append({
                    "country": str(country or ""),
                    "product_names": [str(name) for name in product_names],
                    "target_product": target_product,
                })
    return mismatches


def _latest_country_lookup_decision(local_service_history, target_product):
    """Return the latest country-origin lookup outcome for the target product."""
    target_norm = _norm_catalog_name(target_product)
    if not target_norm:
        return None

    latest = None
    for idx in range(len(local_service_history) - 1):
        current = local_service_history[idx]
        nxt = local_service_history[idx + 1]
        if current.get("role") != "assistant" or nxt.get("role") != "user":
            continue
        is_tool, tool_call_obj = check_tool_call(current.get("content", ""))
        if not is_tool:
            continue
        calls = tool_call_obj if isinstance(tool_call_obj, list) else ([tool_call_obj] if tool_call_obj else [])
        result_text = str(nxt.get("content", ""))
        if "Tool execution result:" not in result_text:
            continue
        result_values = _extract_json_values(result_text.split("Tool execution result:", 1)[1])
        for call, result in zip(calls, result_values):
            if not isinstance(call, dict) or not isinstance(result, dict):
                continue
            if str(call.get("tool_name", "")) != "find_products_by_country_of_origin":
                continue
            product_names = result.get("product_names", [])
            if not isinstance(product_names, list):
                continue
            country = str(_tool_params(call).get("country") or "")
            product_names = [str(name) for name in product_names]
            latest = {
                "country": country,
                "product_names": product_names,
                "target_product": target_product,
                "verified": any(_catalog_membership_matches(target_product, name) for name in product_names),
            }
    return latest


def _is_current_origin_verification_context(local_service_history, current_user_reply):
    """
    Limit strict country-membership checks to tasks that are verifying the
    current visual target wine's origin or using "same country/origin" from it.
    Generic searches like "find products from France" should not require the
    current visual target to appear in the returned country list.
    """
    parts = [current_user_reply or ""]
    for message in reversed(local_service_history or []):
        if message.get("role") == "user":
            parts.append(str(message.get("content", "")))
        if len(parts) >= 4:
            break
    text = _norm_catalog_name(" ".join(parts))

    target_reference = any(term in text for term in (
        "this wine", "this bottle", "same country", "same origin",
        "same country of origin", "country of origin", "origin of this",
        "origin of the wine", "origin of that wine", "where is this wine from",
        "where this wine is from", "current wine", "original bottle",
        "the wine you pointed", "wine i pointed", "bottle i pointed",
    ))
    country_search_only = any(term in text for term in (
        "products from france", "wines from france", "items from france",
        "originating from france", "produced in france",
        "products from italy", "wines from italy", "items from italy",
        "originating from italy", "produced in italy",
    ))
    return target_reference and not country_search_only


def _country_lookup_matches(local_service_history, target_product):
    """Return countries whose reverse lookup results include the target product."""
    target_norm = _norm_catalog_name(target_product)
    if not target_norm:
        return []

    matches = []
    for idx in range(len(local_service_history) - 1):
        current = local_service_history[idx]
        nxt = local_service_history[idx + 1]
        if current.get("role") != "assistant" or nxt.get("role") != "user":
            continue
        is_tool, tool_call_obj = check_tool_call(current.get("content", ""))
        if not is_tool:
            continue
        calls = tool_call_obj if isinstance(tool_call_obj, list) else ([tool_call_obj] if tool_call_obj else [])
        result_text = str(nxt.get("content", ""))
        if "Tool execution result:" not in result_text:
            continue
        result_values = _extract_json_values(result_text.split("Tool execution result:", 1)[1])
        for call, result in zip(calls, result_values):
            if not isinstance(call, dict) or not isinstance(result, dict):
                continue
            if str(call.get("tool_name", "")) != "find_products_by_country_of_origin":
                continue
            country = _tool_params(call).get("country")
            product_names = result.get("product_names", [])
            if not isinstance(product_names, list):
                continue
            if any(_catalog_membership_matches(target_product, name) for name in product_names):
                matches.append({
                    "country": str(country or ""),
                    "product_names": [str(name) for name in product_names],
                    "target_product": target_product,
                })
    return matches


def _country_mismatch_guard(local_service_history, agent_reply, grounding_context="", current_user_reply="", scenario="retail"):
    if _validator_scenario(scenario) != "retail":
        return None
    if not _is_current_origin_verification_context(local_service_history, current_user_reply):
        return None
    target_product = _current_target_product(local_service_history, grounding_context, current_user_reply)
    latest_lookup = _latest_country_lookup_decision(local_service_history, target_product)
    mismatches = _country_lookup_mismatches(local_service_history, target_product)
    if not mismatches:
        return None
    verified_matches = _country_lookup_matches(local_service_history, target_product)
    verified_match = verified_matches[-1] if verified_matches else None
    has_verified_country = bool(verified_match)

    is_tool, tool_call_obj = check_tool_call(agent_reply)
    calls = tool_call_obj if isinstance(tool_call_obj, list) else ([tool_call_obj] if tool_call_obj else [])
    reply_norm = _norm_catalog_name(agent_reply)

    if latest_lookup and not latest_lookup.get("verified") and not has_verified_country:
        lookup_country_norm = _norm_catalog_name(latest_lookup.get("country"))
        new_country_lookup = False
        repeated_latest_country = False
        for call in calls:
            if not isinstance(call, dict):
                continue
            if str(call.get("tool_name", "")) != "find_products_by_country_of_origin":
                continue
            country_norm = _norm_catalog_name(_tool_params(call).get("country"))
            if country_norm and country_norm != lookup_country_norm:
                new_country_lookup = True
            if country_norm and country_norm == lookup_country_norm:
                repeated_latest_country = True
        if not new_country_lookup or repeated_latest_country:
            returned = ", ".join(latest_lookup["product_names"][:8])
            if len(latest_lookup["product_names"]) > 8:
                returned += ", ..."
            return _format_experience_decision(
                scenario,
                "country_origin_absent_from_result",
                country=latest_lookup["country"],
                returned_products=returned,
                target_product=target_product,
                reason=(
                    f'find_products_by_country_of_origin("{latest_lookup["country"]}") returned '
                    f'[{returned}], which does not include {target_product}; '
                    "the current origin is unverified, so the service must validate another country before "
                    "any downstream price/filter/cart/final-answer action."
                ),
                suggestion=f"{target_product} is not verified as {latest_lookup['country']}; validate another country.",
            )

    for mismatch in reversed(mismatches):
        country = mismatch["country"]
        country_norm = _norm_catalog_name(country)
        country_reused = country_norm and country_norm in reply_norm
        downstream_tool = False
        repeated_lookup = False
        for call in calls:
            if not isinstance(call, dict):
                continue
            tool_name = str(call.get("tool_name", ""))
            params = _tool_params(call)
            if tool_name == "find_products_by_country_of_origin" and _norm_catalog_name(params.get("country")) == country_norm:
                repeated_lookup = True
            elif tool_name != "find_products_by_country_of_origin":
                downstream_tool = True
        listed_product_used = any(
            _norm_catalog_name(name) and _norm_catalog_name(name) in reply_norm
            for name in mismatch["product_names"]
        )
        if repeated_lookup or (downstream_tool and not has_verified_country) or country_reused or listed_product_used:
            returned = ", ".join(mismatch["product_names"][:8])
            if len(mismatch["product_names"]) > 8:
                returned += ", ..."
            suggestion = f"{target_product} is not verified as {country}; try another country."
            if verified_match:
                suggestion = _verified_origin_context_message(
                    verified_match["country"],
                    verified_match["product_names"],
                    target_product,
                )
            return _format_experience_decision(
                scenario,
                "country_origin_absent_from_result",
                country=country,
                returned_products=returned,
                target_product=target_product,
                reason=(
                    f'find_products_by_country_of_origin("{country}") returned '
                    f'[{returned}], which does not include {target_product}; '
                    f"therefore {target_product} is not verified as {country}."
                ),
                suggestion=suggestion,
            )
    return None


def _country_verified_repeat_guard(local_service_history, agent_reply, grounding_context="", current_user_reply="", scenario="retail"):
    if _validator_scenario(scenario) != "retail":
        return None
    target_product = _current_target_product(local_service_history, grounding_context, current_user_reply)
    matches = _country_lookup_matches(local_service_history, target_product)
    if not matches:
        return None

    is_tool, tool_call_obj = check_tool_call(agent_reply)
    if not is_tool:
        return None
    calls = tool_call_obj if isinstance(tool_call_obj, list) else ([tool_call_obj] if tool_call_obj else [])

    for match in reversed(matches):
        returned = ", ".join(match["product_names"][:12])
        if len(match["product_names"]) > 12:
            returned += ", ..."
        for call in calls:
            if not isinstance(call, dict):
                continue
            if str(call.get("tool_name", "")) != "find_products_by_country_of_origin":
                continue
            return _format_experience_decision(
                scenario,
                "country_origin_already_verified_repeat",
                country=match["country"],
                returned_products=returned,
                target_product=target_product,
                reason=(
                    f'{target_product} is already verified as {match["country"]} because it appears '
                    f'in the returned product_names list: [{returned}].'
                ),
                suggestion=_verified_origin_context_message(
                    match["country"],
                    match["product_names"],
                    target_product,
                ),
            )
    return None


def _as_text_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    return [str(value).strip()]


def _extract_retail_keywords(grounding, user_reply, limit=6):
    """Extract short stable retrieval tokens from visual grounding and user text."""
    source_parts = []
    for key in ("keyword_candidates", "label_ocr_content", "ocr_content", "target_position"):
        source_parts.extend(_as_text_list(grounding.get(key)))
    visual_terms = []
    for value in _as_text_list(grounding.get("visual_attributes")):
        visual_terms.extend(
            token for token in re.findall(r"[a-zA-Z][a-zA-Z'’éÉ-]{2,}", value.lower())
            if token in _RETAIL_STABLE_TERMS
        )
    source_parts.extend(visual_terms)
    source_parts.append(user_reply or "")
    text = " ".join(source_parts).lower()

    tokens = []
    for token in re.findall(r"[a-zA-Z][a-zA-Z'’éÉ-]{2,}", text):
        normalized = token.strip("'’-.").replace("’", "'")
        if not normalized or normalized in _RETAIL_VISUAL_STOPWORDS:
            continue
        tokens.append(normalized)

    scored = []
    seen = set()
    for token in tokens:
        if token in seen:
            continue
        seen.add(token)
        score = 2 if token in _RETAIL_STABLE_TERMS else 1
        if len(token) <= 4 and token not in _RETAIL_STABLE_TERMS:
            score -= 1
        if score > 0:
            scored.append((score, token))

    scored.sort(key=lambda item: (-item[0], tokens.index(item[1])))
    return [token for _, token in scored[:limit]]


def _extract_price_ranges(grounding, window=10):
    prices = []
    for raw in _as_text_list(grounding.get("visible_price_candidates")):
        for number in re.findall(r"\d+(?:\.\d+)?", raw):
            try:
                price = float(number)
            except ValueError:
                continue
            if 1 <= price <= 10000:
                prices.append(price)

    ranges = []
    seen = set()
    for price in prices[:3]:
        lower = max(0, price - window)
        upper = price + window
        key = (round(lower, 2), round(upper, 2))
        if key not in seen:
            seen.add(key)
            ranges.append((lower, upper))
    return ranges


def _parse_time_range_end(time_range):
    if not time_range:
        return None
    numbers = re.findall(r"\d+(?:\.\d+)?", str(time_range))
    if not numbers:
        return None
    try:
        return float(numbers[-1])
    except ValueError:
        return None


def _normalize_float01(value):
    if value is None:
        return None
    try:
        if isinstance(value, str):
            value = value.strip().rstrip("%")
            number = float(value)
            if number > 1:
                number = number / 100.0
        else:
            number = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, number))


def _normalize_point(value):
    if not isinstance(value, dict):
        return None
    x = _normalize_float01(value.get("x"))
    y = _normalize_float01(value.get("y"))
    if x is None or y is None:
        return None
    return {"x": x, "y": y}


def _normalize_bbox(value):
    if not isinstance(value, dict):
        return None
    x1 = _normalize_float01(value.get("x1"))
    y1 = _normalize_float01(value.get("y1"))
    x2 = _normalize_float01(value.get("x2"))
    y2 = _normalize_float01(value.get("y2"))
    if None in (x1, y1, x2, y2):
        return None
    left, right = sorted([x1, x2])
    top, bottom = sorted([y1, y2])
    if right - left < 0.01 or bottom - top < 0.01:
        return None
    return {"x1": left, "y1": top, "x2": right, "y2": bottom}


def _looks_like_catalog_retail_request(user_reply):
    text = (user_reply or "").lower()
    catalog_terms = (
        "price", "priced", "cost", "country", "origin", "discount", "tax",
        "nutrition", "nutritional", "calories", "allergen", "add", "cart",
        "same country", "lower than", "cheaper"
    )
    visible_only_terms = (
        "visible", "printed", "shelf tag", "price tag", "label says",
        "what does it say", "ocr"
    )
    if any(term in text for term in visible_only_terms):
        return False
    return any(term in text for term in catalog_terms)


def _safe_json_loads(text):
    try:
        return json.loads(text)
    except Exception:
        return {}


def _load_grounding_skill(skill_name):
    skill_path = os.path.join(project_root, "skills", skill_name, "SKILL.md")
    try:
        with open(skill_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError as exc:
        print(f"[Grounding Skill] Failed to load {skill_path}: {exc}")
        return ""


def _resolve_local_video_path(media_path):
    if not media_path:
        return None
    parsed_path = urlparse(media_path).path if "://" in media_path else media_path
    filename = os.path.basename(unquote(parsed_path))
    local_path = os.path.join(project_root, "videos", filename)
    return local_path if os.path.exists(local_path) else None


def _extract_video_contact_sheet(media_path, task_id, interval_seconds=0.5, max_frames=20):
    local_video_path = _resolve_local_video_path(media_path)
    if not local_video_path:
        print(f"[Keyframe] Local video not found for {media_path}")
        return None, []

    try:
        import imageio
        from PIL import Image, ImageDraw, ImageFont
    except Exception as exc:
        print(f"[Keyframe] Missing video/image dependency: {exc}")
        return None, []

    output_dir = os.path.join(project_root, ".cache", "keyframes", f"task_{task_id}")
    os.makedirs(output_dir, exist_ok=True)

    reader = None
    try:
        reader = imageio.get_reader(local_video_path, "ffmpeg")
        meta = reader.get_meta_data()
        fps = float(meta.get("fps") or 0)
        duration = float(meta.get("duration") or 0)
        if fps <= 0 or duration <= 0:
            print(f"[Keyframe] Invalid video metadata: fps={fps}, duration={duration}")
            return None, []

        sample_times = []
        t = 0.0
        while t <= duration + 1e-6 and len(sample_times) < max_frames:
            sample_times.append(round(t, 2))
            t += interval_seconds

        entries = []
        for idx, timestamp in enumerate(sample_times, start=1):
            frame_index = max(0, int(round(timestamp * fps)))
            try:
                frame = reader.get_data(frame_index)
            except Exception:
                continue

            image = Image.fromarray(frame).convert("RGB")
            frame_id = f"frame_{idx:02d}"
            frame_path = os.path.join(output_dir, f"{frame_id}_{timestamp:.2f}s.jpg")
            image.save(frame_path, quality=92)
            entries.append({
                "frame_id": frame_id,
                "timestamp": timestamp,
                "path": frame_path
            })

        if not entries:
            return None, []

        thumb_w = 320
        thumb_h = 200
        label_h = 28
        cols = 4
        rows = (len(entries) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * thumb_w, rows * (thumb_h + label_h)), "white")
        draw = ImageDraw.Draw(sheet)
        try:
            font = ImageFont.truetype("Arial.ttf", 16)
        except Exception:
            font = ImageFont.load_default()

        for i, entry in enumerate(entries):
            img = Image.open(entry["path"]).convert("RGB")
            img.thumbnail((thumb_w, thumb_h))
            x = (i % cols) * thumb_w
            y = (i // cols) * (thumb_h + label_h)
            sheet.paste(img, (x + (thumb_w - img.width) // 2, y))
            label = f"{entry['frame_id']}  {entry['timestamp']:.2f}s"
            draw.rectangle([x, y + thumb_h, x + thumb_w, y + thumb_h + label_h], fill=(0, 0, 0))
            draw.text((x + 8, y + thumb_h + 6), label, fill=(255, 255, 255), font=font)

        sheet_path = os.path.join(output_dir, "contact_sheet.jpg")
        sheet.save(sheet_path, quality=92)
        print(f"[Keyframe] Extracted {len(entries)} frames every {interval_seconds}s -> {sheet_path}")
        return sheet_path, entries
    except Exception as exc:
        print(f"[Keyframe] Failed to extract frames: {exc}")
        return None, []
    finally:
        if reader is not None:
            try:
                reader.close()
            except Exception:
                pass


def _select_keyframe_with_model(contact_sheet_path, frame_entries, user_reply, args):
    if not contact_sheet_path or not frame_entries or args.service_model_name == "manual":
        return None, {}, 0, 0

    frame_manifest = [
        {"frame_id": entry["frame_id"], "timestamp": entry["timestamp"]}
        for entry in frame_entries
    ]
    prompt = (
        f"Latest user message:\n{user_reply}\n\n"
        f"Frame manifest:\n{json.dumps(frame_manifest, ensure_ascii=False, indent=2)}\n\n"
        "Select the frame that best captures the user's referenced target."
    )
    messages = [
        {"role": "system", "content": SERVICE_KEYFRAME_SELECTION_PROMPT},
        {
            "role": "user",
            "content": build_message_with_image(
                prompt,
                contact_sheet_path,
                use_vision=True,
                service_model_name=args.service_model_name
            )
        }
    ]
    reply, input_tok, output_tok = call_llm(
        messages,
        agent_type="service",
        service_model_name=args.service_model_name
    )
    decision = _extract_json_object(reply)
    selected_id = str(decision.get("selected_frame_id") or "").strip()
    selected_entry = next((entry for entry in frame_entries if entry["frame_id"] == selected_id), None)
    if not selected_entry:
        selected_entry = frame_entries[min(2, len(frame_entries) - 1)]
        decision["selected_frame_id"] = selected_entry["frame_id"]
        decision["timestamp"] = selected_entry["timestamp"]
        decision.setdefault("reason", "Fallback to an early frame because model selection was invalid.")
    print(f"[Keyframe Selection] {json.dumps(decision, ensure_ascii=False)}")
    return selected_entry["path"], decision, input_tok, output_tok


def _select_timeline_with_model(media_path, user_reply, args):
    if not media_path or args.service_model_name == "manual":
        return {}, 0, 0

    prompt = (
        f"Latest user message:\n{user_reply}\n\n"
        "Generate only the pointing-event timeline and selected event range for the user's referenced target. "
        "Do not output coordinates, finger points, or bounding boxes."
    )
    messages = [
        {"role": "system", "content": SERVICE_TIMELINE_SELECTION_PROMPT},
        {
            "role": "user",
            "content": build_message_with_image(
                prompt,
                media_path,
                use_vision=True,
                service_model_name=args.service_model_name
            )
        }
    ]
    reply, input_tok, output_tok = call_llm(
        messages,
        agent_type="service",
        service_model_name=args.service_model_name
    )
    decision = _extract_json_object(reply)
    end_timestamp = _parse_time_range_end(decision.get("selected_time_range"))
    if end_timestamp is not None:
        decision["selected_timestamp"] = end_timestamp
        decision["timestamp_policy"] = "event_end"
    print(f"[Timeline Selection] {json.dumps(decision, ensure_ascii=False)}")
    return decision, input_tok, output_tok


def _locate_bbox_with_qwen_vl(media_path, user_reply, timeline_decision):
    if not media_path:
        return {}, 0, 0

    prompt = (
        f"Latest user message:\n{user_reply}\n\n"
        f"Selected timeline:\n{json.dumps(timeline_decision, ensure_ascii=False, indent=2)}\n\n"
        "Use the latest user message plus the selected event range to locate the exact referenced object, "
        "finger tip, and aligned price tag. The timeline intentionally contains no coordinates; produce the "
        "coordinates yourself from the video. Return JSON only."
    )
    messages = [
        {"role": "system", "content": SERVICE_QWEN_VL_BBOX_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "video_url", "video_url": {"url": media_path}},
                {"type": "text", "text": prompt}
            ]
        }
    ]
    reply, input_tok, output_tok = call_grounding_model(messages)
    decision = _extract_json_object(reply)
    if not isinstance(decision, dict):
        decision = {}

    normalized = {
        "selected_timestamp": timeline_decision.get("selected_timestamp"),
        "finger_tip_point": _normalize_point(decision.get("finger_tip_point")),
        "target_bbox": _normalize_bbox(decision.get("target_bbox")),
        "label_bbox": _normalize_bbox(decision.get("label_bbox")),
        "below_price_tag_bbox": _normalize_bbox(decision.get("below_price_tag_bbox")),
        "spatial_confidence": decision.get("spatial_confidence", "low"),
        "price_tag_alignment": decision.get("price_tag_alignment", "unknown"),
        "reason": decision.get("reason", "")
    }
    if decision.get("selected_timestamp") is not None:
        try:
            normalized["selected_timestamp"] = float(decision.get("selected_timestamp"))
        except (TypeError, ValueError):
            pass
    print(f"[Qwen3-VL BBox] model={GROUNDING_MODEL_NAME} {json.dumps(normalized, ensure_ascii=False)}")
    return normalized, input_tok, output_tok


def _merge_bbox_into_timeline(timeline_decision, bbox_decision):
    if not isinstance(timeline_decision, dict):
        timeline_decision = {}
    if not isinstance(bbox_decision, dict):
        return timeline_decision

    timeline_decision["qwen3_vl_bbox"] = bbox_decision
    timeline_decision["spatial_source"] = GROUNDING_MODEL_NAME
    if bbox_decision.get("finger_tip_point") is not None:
        timeline_decision["selected_finger_tip_point"] = bbox_decision.get("finger_tip_point")
    if bbox_decision.get("target_bbox") is not None:
        timeline_decision["selected_target_bbox"] = bbox_decision.get("target_bbox")
    if bbox_decision.get("label_bbox") is not None:
        timeline_decision["selected_label_bbox"] = bbox_decision.get("label_bbox")
    if bbox_decision.get("below_price_tag_bbox") is not None:
        timeline_decision["selected_below_price_tag_bbox"] = bbox_decision.get("below_price_tag_bbox")
    if bbox_decision.get("selected_timestamp") is not None:
        timeline_decision["selected_timestamp"] = bbox_decision.get("selected_timestamp")
    timeline_decision["bbox_provider"] = GROUNDING_MODEL_NAME
    return timeline_decision


def _collect_retail_candidates(db, grounding, user_reply):
    """
    Service-side read-only candidate retrieval.
    It uses official tools, but keeps these calls as grounding context rather than final agent actions.
    """
    retrieval_calls = []
    for keyword in _extract_retail_keywords(grounding, user_reply):
        retrieval_calls.append({
            "tool_name": "get_price",
            "parameters": {"product_name": keyword}
        })

    for lower, upper in _extract_price_ranges(grounding):
        retrieval_calls.append({
            "tool_name": "find_products_by_price_range",
            "parameters": {"min_price": lower, "max_price": upper}
        })

    if not retrieval_calls:
        return {"calls": [], "results": [], "candidates": []}

    tool_results = execute_tool(db, retrieval_calls[:8])
    candidates = {}
    failed_terms = []
    for result in tool_results:
        data = _safe_json_loads(result.get("content", "{}"))
        params = result.get("parameters") or {}
        query_term = params.get("product_name")
        if query_term and data.get("status") == "error":
            failed_terms.append(query_term)
        products = data.get("products")
        if isinstance(products, list):
            for product in products:
                name = product.get("product_name")
                if name:
                    entry = candidates.setdefault(name, {"product_name": name, "evidence": []})
                    entry["evidence"].append({
                        "tool": result.get("tool_name"),
                        "parameters": result.get("parameters"),
                        "result": product
                    })
        product_names = data.get("product_names")
        if isinstance(product_names, list):
            for name in product_names:
                entry = candidates.setdefault(name, {"product_name": name, "evidence": []})
                entry["evidence"].append({
                    "tool": result.get("tool_name"),
                    "parameters": result.get("parameters")
                })

    return {
        "calls": retrieval_calls[:8],
        "results": tool_results,
        "candidates": list(candidates.values())[:20],
        "failed_terms": failed_terms
    }


def _rerank_visual_candidate(grounding, retrieval, user_reply, recent_context, args, grounding_skill=""):
    candidates = retrieval.get("candidates", [])
    if not candidates or args.service_model_name == "manual":
        needs_clarification = _looks_like_catalog_retail_request(user_reply)
        return {
            "selected_product_name": None,
            "confidence": "low",
            "selection_mode": "no_candidates",
            "matched_evidence": {"ocr_terms": [], "price": None, "visual": []},
            "needs_clarification": needs_clarification,
            "clarification_question": (
                "I need the product label or name to confirm the catalog item before checking its details."
                if needs_clarification else ""
            )
        }, 0, 0

    payload = {
        "latest_user_message": user_reply,
        "visual_grounding": grounding,
        "candidate_products": candidates[:20],
        "failed_retrieval_terms": retrieval.get("failed_terms", []),
        "recent_service_context": recent_context[-6:],
        "grounding_skill": grounding_skill,
    }
    messages = [
        {"role": "system", "content": SERVICE_CANDIDATE_RERANK_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)}
    ]
    reply, input_tok, output_tok = call_llm(
        messages,
        agent_type="service",
        service_model_name=args.service_model_name
    )
    decision = _extract_json_object(reply)
    valid_names = {item.get("product_name") for item in candidates if item.get("product_name")}
    selected = decision.get("selected_product_name")
    selection_mode = decision.get("selection_mode")
    if selected is None and selection_mode == "visual_only":
        decision["needs_clarification"] = _looks_like_catalog_retail_request(user_reply)
        if decision["needs_clarification"] and not decision.get("clarification_question"):
            decision["clarification_question"] = (
                "I can read some visual evidence, but I need the product label or name to confirm the catalog item."
            )
    elif selected not in valid_names:
        decision["selected_product_name"] = None
        if not decision.get("needs_clarification"):
            decision["needs_clarification"] = True
    decision.setdefault("confidence", "low")
    decision.setdefault("selection_mode", "no_confident_match")
    decision.setdefault("matched_evidence", {"ocr_terms": [], "price": None, "visual": []})
    decision.setdefault("clarification_question", "")
    return decision, input_tok, output_tok


def _format_grounding_context(grounding, retrieval, rerank_decision=None, grounding_skill_name=""):
    compact = {
        "grounding_skill": grounding_skill_name,
        "visual_grounding": grounding,
        "retrieval_calls": retrieval.get("calls", []),
        "failed_retrieval_terms": retrieval.get("failed_terms", []),
        "candidate_products": retrieval.get("candidates", []),
        "resolved_visual_target": rerank_decision or {}
    }
    return (
        "Service-side visual grounding and candidate retrieval:\n"
        + json.dumps(compact, ensure_ascii=False, indent=2)
        + "\nUse these candidates as catalog grounding evidence. Prefer candidates over raw OCR strings. "
        + "Do not call product lookup tools with failed_retrieval_terms or longer raw-OCR phrases built from them. "
        + "If resolved_visual_target has selected_product_name, use it as the canonical product name. "
        + "For retail product price/country/discount/tax/nutrition requests, resolve a canonical product and use catalog tools; do not answer from shelf-price OCR alone. "
        + "If resolved_visual_target.selection_mode is visual_only, answer only explicitly requested visible text, otherwise ask the clarification question. "
        + "If resolved_visual_target.needs_clarification is true, ask only its clarification_question."
    )


def _validate_service_output(
    user_instruction,
    current_user_reply,
    local_service_history,
    grounding_context,
    agent_reply,
    args,
    tool_descriptions="",
):
    if args.service_model_name == "manual":
        return {"valid": True, "reason": "", "suggestion": ""}, 0, 0

    scenario = _validator_scenario(getattr(args, "scenario", "retail"))
    validator_prompt = _load_validator_prompt(scenario)
    experience = _load_validator_experience(scenario)

    is_tool, tool_call_obj = check_tool_call(agent_reply)
    calls = tool_call_obj if isinstance(tool_call_obj, list) else ([tool_call_obj] if tool_call_obj else [])

    current_target_product = _current_target_product(
        local_service_history,
        grounding_context,
        current_user_reply,
    )

    duplicate_decision = _duplicate_instruction_guard(
        local_service_history,
        agent_reply,
        current_target_product,
        scenario,
    )
    if duplicate_decision:
        return duplicate_decision, 0, 0

    mismatch_decision = _country_mismatch_guard(
        local_service_history,
        agent_reply,
        grounding_context,
        current_user_reply,
        scenario,
    )
    if mismatch_decision:
        return mismatch_decision, 0, 0

    verified_repeat_decision = _country_verified_repeat_guard(
        local_service_history,
        agent_reply,
        grounding_context,
        current_user_reply,
        scenario,
    )
    if verified_repeat_decision:
        return verified_repeat_decision, 0, 0

    if scenario == "retail" and is_tool and len(calls) == 1:
        tool_name = str(calls[0].get("tool_name", ""))
        if tool_name == "find_products_by_country_of_origin":
            return {
                "valid": True,
                "reason": "single new country reverse lookup allowed for attribute verification",
                "suggestion": ""
            }, 0, 0

    llm_decision, validator_input_tok, validator_output_tok = _call_llm_validator(
        scenario,
        validator_prompt,
        experience,
        user_instruction,
        current_user_reply,
        local_service_history,
        grounding_context,
        agent_reply,
        tool_descriptions,
    )
    if not llm_decision.get("valid", True):
        return llm_decision, validator_input_tok, validator_output_tok

    return {
        "valid": True,
        "reason": llm_decision.get("reason") or f"no {scenario} validator rule matched; ordinary step allowed",
        "suggestion": ""
    }, validator_input_tok, validator_output_tok


_MANUAL_GROUNDING_CACHE = None


def _load_manual_grounding_index():
    """Load task-level manual keyframe/bbox annotations, if available."""
    global _MANUAL_GROUNDING_CACHE
    if _MANUAL_GROUNDING_CACHE is not None:
        return _MANUAL_GROUNDING_CACHE

    index = {}
    env_path = os.environ.get("MANUAL_GROUNDING_JSONL", "")
    if env_path:
        paths = [path.strip() for path in env_path.split(os.pathsep) if path.strip()]
    else:
        paths = [
            os.path.join(project_root, "annotations", "manual_task_video_grounding_from_instruction.jsonl"),
            os.path.join(project_root, "annotations", "final_grounding.jsonl"),
        ]

    loaded = []
    for path in paths:
        if not os.path.exists(path):
            continue
        before = len(index)
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    key = (row.get("scenario"), int(row.get("scenario_number")), int(row.get("task_id")))
                    index[key] = row
            loaded.append(f"{path} (+{len(index) - before})")
        except Exception as exc:
            print(f"[Manual Grounding] Failed to load {path}: {exc}")
    if loaded:
        print(f"[Manual Grounding] Loaded {len(index)} records from {', '.join(loaded)}")
    else:
        print(f"[Manual Grounding] No grounding files found in: {paths}")

    _MANUAL_GROUNDING_CACHE = index
    return index


def _manual_grounding_for_task(scenario, scenario_number, task_id):
    return _load_manual_grounding_index().get((scenario, int(scenario_number), int(task_id)))


def _abs_project_path(path):
    if not path:
        return None
    return path if os.path.isabs(path) else os.path.join(project_root, path)


def _draw_manual_grounding_images(record, scenario, scenario_number, task_id):
    """
    Create visual prompt images with bboxes drawn over the annotated keyframes.
    Red = target_bbox, blue = label_bbox, yellow = price_tag_bbox.
    """
    if not record:
        return []
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception as exc:
        print(f"[Manual Grounding] PIL unavailable, using text-only grounding: {exc}")
        return []

    output_dir = os.path.join(project_root, ".cache", "manual_grounding", f"{scenario}{scenario_number}_task_{task_id}")
    os.makedirs(output_dir, exist_ok=True)
    image_paths = []

    frames = record.get("annotation", {}).get("frames", [])
    for frame_index, frame in enumerate(frames[:4], start=1):
        src = _abs_project_path(frame.get("frame_path"))
        if not src or not os.path.exists(src):
            continue
        try:
            image = Image.open(src).convert("RGB")
        except Exception:
            continue

        draw = ImageDraw.Draw(image)
        try:
            font = ImageFont.truetype("Arial.ttf", 22)
        except Exception:
            font = ImageFont.load_default()
        w, h = image.size

        def draw_box(bbox, color, label):
            if not isinstance(bbox, dict):
                return
            vals = [bbox.get(k) for k in ("x1", "y1", "x2", "y2")]
            if any(v is None for v in vals):
                return
            x1, y1, x2, y2 = vals
            x1, x2 = sorted([max(0, min(1, float(x1))), max(0, min(1, float(x2)))])
            y1, y2 = sorted([max(0, min(1, float(y1))), max(0, min(1, float(y2)))])
            xy = [int(x1 * w), int(y1 * h), int(x2 * w), int(y2 * h)]
            draw.rectangle(xy, outline=color, width=max(3, w // 300))
            draw.rectangle([xy[0], max(0, xy[1] - 28), xy[0] + min(w - xy[0], 260), xy[1]], fill=color)
            draw.text((xy[0] + 4, max(0, xy[1] - 25)), label, fill=(0, 0, 0), font=font)

        for target_index, target in enumerate(frame.get("targets", [])[:6], start=1):
            prefix = f"T{target_index}"
            draw_box(target.get("target_bbox"), (255, 64, 64), f"{prefix} target")
            draw_box(target.get("label_bbox"), (80, 160, 255), f"{prefix} label/OCR")
            draw_box(target.get("price_tag_bbox"), (255, 220, 0), f"{prefix} price")

        title = f"{scenario}{scenario_number} task {task_id} frame {frame_index} @ {frame.get('timestamp_sec')}s"
        draw.rectangle([0, 0, min(w, 900), 34], fill=(255, 255, 255))
        draw.text((8, 6), title, fill=(0, 0, 0), font=font)

        out = os.path.join(output_dir, f"manual_frame_{frame_index}.jpg")
        image.save(out, quality=92)
        image_paths.append(out)

    if image_paths:
        print(f"[Manual Grounding] Created {len(image_paths)} bbox keyframe image(s) for {scenario}{scenario_number} task {task_id}")
    return image_paths


def _format_manual_grounding_context(record):
    if not record:
        return ""

    compact_frames = []
    for frame in record.get("annotation", {}).get("frames", []):
        targets = []
        for target in frame.get("targets", []):
            targets.append({
                "name": target.get("name"),
                "target_bbox": target.get("target_bbox"),
                "label_bbox": target.get("label_bbox"),
                "price_tag_bbox": target.get("price_tag_bbox"),
                "visible_ocr": target.get("visible_ocr", target.get("ocr_content", [])),
                "visual_basis": target.get("visual_basis")
            })
        compact_frames.append({
            "timestamp_sec": frame.get("timestamp_sec"),
            "selection_reason": frame.get("selection_reason"),
            "targets": targets
        })

    compact = {
        "source": record.get("annotation", {}).get("annotation_version") or "manual_task_video_grounding_from_instruction",
        "used_hidden_fields": record.get("annotation", {}).get("used_hidden_fields", False),
        "video": record.get("video") or record.get("video_path") or record.get("image_path"),
        "frames": compact_frames,
        "needs_human_review": record.get("annotation", {}).get("needs_human_review", False)
    }
    return (
        "Manual task-relevant keyframes and bounding boxes:\n"
        + json.dumps(compact, ensure_ascii=False, indent=2)
        + "\nUse the timestamps and normalized bounding boxes only as internal visual guidance. "
        + "First create concise keyframe captions, extract OCR content from target/label regions, "
        + "and build a task plan. Do not expose local frame paths or media URLs."
    )


def _strip_media_and_bbox_fields(value):
    """Remove local paths, URLs, and raw bbox coordinates before service-agent use."""
    blocked_fragments = ("path", "url", "bbox", "coordinate", "coordinates")
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if any(fragment in str(key).lower() for fragment in blocked_fragments):
                continue
            cleaned[key] = _strip_media_and_bbox_fields(item)
        return cleaned
    if isinstance(value, list):
        return [_strip_media_and_bbox_fields(item) for item in value]
    if isinstance(value, str):
        text = re.sub(r"https?://\S+", "[media-url-removed]", value)
        text = re.sub(r"\S*frame_path\S*", "[frame-path-removed]", text)
        return text
    return value


def _fallback_manual_text_evidence(record, user_reply):
    """Build text-only evidence from annotations without exposing paths or bboxes."""
    captions = []
    ocr_content = []
    for frame in record.get("annotation", {}).get("frames", []):
        timestamp = frame.get("timestamp_sec")
        target_notes = []
        for target in frame.get("targets", []):
            name = target.get("name") or "target"
            visual_basis = target.get("visual_basis") or ""
            if visual_basis:
                target_notes.append(f"{name}: {visual_basis}")
            visible_ocr = target.get("visible_ocr", target.get("ocr_content", []))
            if visible_ocr:
                ocr_content.append({
                    "timestamp_sec": timestamp,
                    "target": name,
                    "text": "; ".join(_as_text_list(visible_ocr))
                })
        captions.append({
            "timestamp_sec": timestamp,
            "caption": "; ".join(target_notes) or frame.get("selection_reason") or "task-relevant frame"
        })
    return {
        "keyframe_captions": captions,
        "ocr_content": ocr_content,
        "visual_target_summary": f"Use the task-relevant visual evidence for: {user_reply}",
        "task_plan": [
            "Use keyframe captions and OCR content to identify the referenced target.",
            "Use catalog/order tools to verify task facts and complete the user request."
        ]
    }


def _manual_grounding_action_instruction():
    return (
        "\nFormal service-agent output rule for this turn: the caption/OCR/plan above is internal evidence only, "
        "not a user-facing answer. The first accepted service-agent response after this context must be a strict JSON "
        "tool-call array whenever any tool can progress the task. Do not output a natural-language OCR/plan summary as "
        "the first formal response. Only use natural language if no available tool can progress the request or a required "
        "parameter is genuinely missing. Use prepared evidence only to identify the referenced object; all catalog facts "
        "such as country, price, category, calories, discounts, availability, cart contents, and order state must come "
        "from tool results. If prepared evidence or user corrections conflict with tool results, tool results win."
    )


_GROUNDING_CATALOG_FACT_PATTERNS = (
    "country of origin", "country (", "origin is", "origin for", "reference country",
    "establishes the country", "establishes the reference country", "from new zealand",
    "from france", "from italy", "from australia", "from spain", "from usa",
    "from united states", "catalog price", "price ceiling", "priced lower",
    "discount factor", "tax rate", "nutrition", "calorie", "allergen", "category",
)


def _scrub_grounding_text(value, replacement=""):
    text = str(value or "").strip()
    if not text:
        return text
    pieces = re.split(r"(?<=[.!?。！？])\s+|\n+", text)
    kept = []
    for piece in pieces:
        item = piece.strip()
        if not item:
            continue
        lower = item.lower()
        if any(pattern in lower for pattern in _GROUNDING_CATALOG_FACT_PATTERNS):
            continue
        if re.search(r"\b(new zealand|france|italy|australia|spain|usa|united states)\b", lower) and re.search(r"\b(country|origin|from|produced|indicates)\b", lower):
            continue
        kept.append(item)
    if kept:
        return " ".join(kept)
    return replacement


def _sanitize_prepared_grounding_evidence(evidence):
    """Keep prepared grounding visual-only; catalog facts must come from tools."""
    if not isinstance(evidence, dict):
        return evidence

    sanitized = dict(evidence)
    sanitized["visual_target_summary"] = _scrub_grounding_text(
        sanitized.get("visual_target_summary", ""),
        "The user-referenced target is identified only by visible OCR, position, pointing order, and visual attributes.",
    )

    plans = sanitized.get("task_plan")
    if isinstance(plans, list):
        new_plan = []
        added_tool_step = False
        for step in plans:
            clean = _scrub_grounding_text(step, "")
            if clean:
                new_plan.append(clean)
            elif not added_tool_step:
                new_plan.append("Verify any required catalog fact using the provided tools before using it.")
                added_tool_step = True
        sanitized["task_plan"] = new_plan

    sanitized["catalog_fact_warning"] = (
        "Prepared visual evidence may identify objects and OCR only. Catalog facts such as country, final price, "
        "discount, tax, nutrition, allergens, category, inventory, cart/order state, or availability must be obtained "
        "from accepted tool results, not from this grounding text or user/common knowledge."
    )
    return sanitized


def _prepare_manual_grounding_evidence(record, user_reply, media_path, args):
    """Turn manual timestamps/bboxes plus video into text-only evidence for later service steps."""
    manual_context = _format_manual_grounding_context(record)
    if not manual_context:
        return "", 0, 0

    if not media_path or args.service_model_name == "manual":
        evidence = _sanitize_prepared_grounding_evidence(_fallback_manual_text_evidence(record, user_reply))
        context = (
            "Prepared text-only visual evidence from manual keyframes:\n"
            + json.dumps(evidence, ensure_ascii=False, indent=2)
            + "\nUse only this text evidence, conversation history, and tool results for later steps. "
            + "Do not request or rely on image/video URLs in subsequent service-agent calls."
            + _manual_grounding_action_instruction()
        )
        return context, 0, 0

    prompt = (
        f"Latest user message:\n{user_reply}\n\n"
        f"{manual_context}\n\n"
        "You are doing an internal service-side visual preprocessing step. "
        "Use the video together with the provided task-relevant timestamps and bboxes. "
        "Return ONLY compact JSON with this schema:\n"
        "{\n"
        '  "keyframe_captions": [{"timestamp_sec": number, "caption": "what is visible in the relevant bbox/region"}],\n'
        '  "ocr_content": [{"timestamp_sec": number, "target": "short target description", "text": "visible OCR fragments"}],\n'
        '  "visual_target_summary": "which object/item the user is referring to and why",\n'
        '  "task_plan": ["step 1", "step 2"]\n'
        "}\n"
        "Do not include frame_path, local file paths, image URLs, video URLs, raw bbox coordinates, markdown, or tool calls. "
        "Do not answer the user yet. The next service step will use only your text evidence. "
        "Do NOT infer or write catalog facts such as country of origin, final/catalog price, discount, tax, nutrition, allergens, category, inventory, cart/order state, or availability. "
        "Visible shelf-price OCR may appear only inside ocr_content as weak visual text, never as the final price or price threshold. "
        "If a plan needs a catalog fact, write that it must be verified with tools; do not fill in the value from world knowledge."
    )
    messages = [
        {
            "role": "system",
            "content": (
                "You extract text-only visual evidence for a service agent. "
                "Focus on task-relevant keyframe captions, OCR content, and a minimal plan. "
                "Do not infer catalog facts such as country, final price, discount, tax, nutrition, category, inventory, cart, order, or availability. "
                "Output strict JSON only."
            )
        },
        {
            "role": "user",
            "content": build_message_with_image(
                prompt,
                media_path,
                use_vision=True,
                service_model_name=args.service_model_name
            )
        }
    ]

    try:
        reply, input_tok, output_tok = call_llm(
            messages,
            agent_type="service",
            service_model_name=args.service_model_name
        )
    except Exception as exc:
        print(f"[Manual Grounding Prep] Failed, using annotation-derived text evidence: {exc}")
        evidence = _sanitize_prepared_grounding_evidence(_fallback_manual_text_evidence(record, user_reply))
        context = (
            "Prepared text-only visual evidence from manual keyframes:\n"
            + json.dumps(evidence, ensure_ascii=False, indent=2)
            + "\nUse only this text evidence, conversation history, and tool results for later steps. "
            + "Do not request or rely on image/video URLs in subsequent service-agent calls."
            + _manual_grounding_action_instruction()
        )
        return context, 0, 0

    evidence = _extract_json_object(reply)
    if not evidence:
        evidence = {"raw_visual_evidence": reply}
    evidence = _strip_media_and_bbox_fields(evidence)
    evidence = _sanitize_prepared_grounding_evidence(evidence)

    context = (
        "Prepared text-only visual evidence from manual keyframes:\n"
        + json.dumps(evidence, ensure_ascii=False, indent=2)
        + "\nUse only this text evidence, conversation history, and tool results for later steps. "
        + "Do not request or rely on image/video URLs in subsequent service-agent calls."
        + _manual_grounding_action_instruction()
    )
    print(f"[Manual Grounding Prep] {json.dumps(evidence, ensure_ascii=False)}")
    return context, input_tok, output_tok


def _build_service_user_content(text, media_path, extra_image_paths, args):
    """Attach media only when the current service step is allowed to see it."""
    content = build_message_with_image(
        text,
        media_path,
        use_vision=True,
        service_model_name=args.service_model_name
    )
    for image_path in extra_image_paths or []:
        if not str(image_path).startswith(("http://", "https://")):
            continue
        image_content = build_message_with_image(
            "",
            image_path,
            use_vision=True,
            service_model_name=args.service_model_name
        )
        for part in image_content:
            if part.get("type") != "text":
                content.append(part)
    return content


def get_video_url_for_model(video_url, model_name):
    """Return corresponding video URL based on model name and VIDEO_MODE"""
    if not video_url:
        return video_url

    parsed_path = urlparse(str(video_url)).path if "://" in str(video_url) else str(video_url)
    video_filename = os.path.basename(unquote(parsed_path))

    if VIDEO_MODE == "local":
        return get_video_path(video_filename)
    else:
        return get_video_path(video_filename)


def _scenario_media_value(sc):
    """Read the scenario media field while keeping compatibility with old JSONs."""
    for key in ("image_path", "video_path", "video"):
        value = sc.get(key)
        if value:
            return value, key
    image_name = sc.get("image_name")
    if image_name:
        return f"{image_name}.mp4", "image_name"
    return None, None


def run_simulation(input_path, tool_info_path, output_path, args=None, service_model_name="qwen3-vl-225b"):
    """
    Interactive Mode: Multi-round conversation (Easy mode only)
    """
    use_vision = False

    with open(tool_info_path, 'r', encoding='utf-8') as f:
        tools_list = json.load(f)
        tool_descriptions = json.dumps(tools_list, indent=2, ensure_ascii=False)

    if not os.path.exists(input_path):
        print(f"Can't find the file {input_path}.")
        return

    with open(input_path, 'r', encoding='utf-8') as f:
        scenarios = json.load(f)

    start_task = max(1, getattr(args, "start_task", 1))
    total_tasks = len(scenarios)
    if args.num_tasks > 0:
        end_task = min(total_tasks, start_task + args.num_tasks - 1)
    else:
        end_task = total_tasks
    indexed_scenarios = list(enumerate(scenarios[start_task - 1:end_task], start=start_task))

    all_results = []
    if start_task > 1 and os.path.exists(output_path):
        try:
            with open(output_path, 'r', encoding='utf-8') as f:
                existing_results = json.load(f)
            all_results.extend([
                result for result in existing_results
                if int(result.get("task_id", 0)) < start_task
            ])
            if all_results:
                print(f"[Resume] Preserved {len(all_results)} existing result(s) before task {start_task}.")
        except Exception as exc:
            print(f"[Resume] Could not preserve existing results from {output_path}: {exc}")

    for task_id, sc in indexed_scenarios:

        print(f"\n{'='*20} Scenario {args.scenario}{args.scenario_number}: {task_id} {'='*20} ")
        if args.scenario == "retail":
            db = RetailDB()
            if args.scenario_number == 1:
                db.init_from_json(retail_init_data1)
            elif args.scenario_number == 2:
                db.init_from_json(retail_init_data2)
            elif args.scenario_number == 3:
                db.init_from_json(retail_init_data3)
            elif args.scenario_number == 4:
                db.init_from_json(retail_init_data4)
            elif args.scenario_number == 5:
                db.init_from_json(retail_init_data5)
            elif args.scenario_number == 6:
                db.init_from_json(retail_init_data6)
            elif args.scenario_number == 7:
                db.init_from_json(retail_init_data7)
            elif args.scenario_number == 8:
                db.init_from_json(retail_init_data8)
            elif args.scenario_number == 9:
                db.init_from_json(retail_init_data9)
            elif args.scenario_number == 10:
                db.init_from_json(retail_init_data10)
        elif args.scenario == "kitchen":
            db = KitchenDB()
            db.init_from_json(kitchen_init_data)
        elif args.scenario == "restaurant":
            db = RestaurantDB()
            if args.scenario_number == 5:
                db.init_from_json(restaurant_init_data5)
            else:
                db.init_from_json(restaurant_init_data)
        elif args.scenario == "order":
            db = OrderDB()
            db.init_from_json(order_init_data)

        user_instruction = sc.get("Instruction", "")
        raw_media_path, media_source_field = _scenario_media_value(sc)
        image_path = get_video_url_for_model(raw_media_path, args.service_model_name)
        image_description = sc.get("image_description", "")
        print(
            "[Media] "
            f"source_field={media_source_field} raw={raw_media_path} "
            f"resolved={image_path} video_mode={VIDEO_MODE}"
        )

        start_time = time.time()

        history_log = {
            "task_id": task_id,
            "mode": "text",
            "instruction": user_instruction,
            "image_description": image_description,
            "media_source_field": media_source_field,
            "raw_image_path": raw_media_path,
            "resolved_image_path": image_path,
            "dialogue": [],
            "tool_calls": [],
            "rounds_count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "tool_calls_count": 0,
            "user_response_time_seconds": 0.0,
            "agent_response_time_seconds": 0.0,
            "start_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_time))
        }

        user_agent_sys_prompt = USER_TEXT_ONLY_PROMPT_EASY.format(
            user_instruction=user_instruction,
            image_description=image_description,
            original_user_response="",
            evaluation_feedback="",
            history_summary="",
            service_agent_response="Dear customer, how can I help you?"
        )

        user_messages = [
            {"role": "system", "content": user_agent_sys_prompt},
            {"role": "user", "content": "You are a customer in the environment shown in the video, and you need to complete the instructions in **Task**. I am your AI customer service representative; please interact with me in the first person. Let's begin the conversation.\nDear customer, how can I help you?"}
        ]

        service_agent_sys_prompt = SERVICE_AGENT_PROMPT_BASE.format(tool_descriptions=tool_descriptions)
        service_history = []

        max_turns = 10
        rounds_count = 0
        input_tokens_total = 0
        output_tokens_total = 0
        tool_calls_count = 0
        service_model_requests_count = 0

        accumulated_original_scores = {}
        accumulated_final_scores = {}
        valid_evaluation_count = 0

        last_agent_response_for_check = "Dear customer, how can I help you?"
        summarized_history_str = ""

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)

        for turn in range(max_turns):
            user_start_time = time.time()
            user_reply, user_input_tok, user_output_tok = call_llm(user_messages, agent_type="user", service_model_name=args.service_model_name)
            user_gen_time = time.time() - user_start_time
            print(f"[Time] User response generation (Turn {turn}): {user_gen_time:.3f} seconds")
            history_log["user_response_time_seconds"] += user_gen_time

            evaluation_info = None
            check_start_time = time.time()
            if args.multi_agent_user:
                original_user_reply = user_reply
                user_reply, evaluation_info = check_user_contradiction(
                    user_response=original_user_reply,
                    user_instruction=user_instruction,
                    image_description=image_description if not use_vision else "",
                    multi_agent_user=args.multi_agent_user,
                    last_agent_response=last_agent_response_for_check,
                    history=history_log["dialogue"],
                    summarized_history=summarized_history_str if getattr(args, "summary_user", False) else None,
                    user_mode="easy"
                )

                if evaluation_info:
                    print(f"\n[User Response Evaluation]")
                    if "scores" in evaluation_info:
                        print(f"  Original Scores: {json.dumps(evaluation_info['scores'], ensure_ascii=False)} (Average: {evaluation_info.get('average_score', 'N/A')})")
                    if "corrected_scores" in evaluation_info:
                        print(f"  Corrected Scores: {json.dumps(evaluation_info['corrected_scores'], ensure_ascii=False)} (Average: {evaluation_info.get('corrected_average_score', 'N/A')})")
                    if "reasoning" in evaluation_info:
                        print(f"  Original Reasoning: {json.dumps(evaluation_info['reasoning'], ensure_ascii=False, indent=2)}")
                    if "corrected_reasoning" in evaluation_info:
                        print(f"  Corrected Reasoning: {json.dumps(evaluation_info['corrected_reasoning'], ensure_ascii=False, indent=2)}")

                    if "scores" in evaluation_info:
                        valid_evaluation_count += 1
                        original_scores_dict = evaluation_info["scores"]
                        final_scores_dict = evaluation_info.get("corrected_scores", original_scores_dict)

                        for k, v in original_scores_dict.items():
                            try:
                                accumulated_original_scores[k] = accumulated_original_scores.get(k, 0.0) + float(v)
                            except ValueError:
                                pass

                        for k, v in final_scores_dict.items():
                            try:
                                accumulated_final_scores[k] = accumulated_final_scores.get(k, 0.0) + float(v)
                            except ValueError:
                                pass
                if user_reply != original_user_reply:
                    print(f"User Response Corrected: {user_reply}")

            check_time = time.time() - check_start_time
            if args.multi_agent_user:
                print(f"[Time] Check phase (Turn {turn}): {check_time:.3f} seconds")
                history_log["user_response_time_seconds"] += check_time

            print(f"Final User Response: {user_reply}")

            log_entry = {"role": "user", "turn": turn, "content": user_reply}
            if evaluation_info:
                log_entry["evaluation"] = evaluation_info

            history_log["dialogue"].append(log_entry)

            if "STOP" in user_reply:
                print("Stop signal detected")
                break

            service_history.append({"role": "user", "content": user_reply})
            user_messages.append({"role": "assistant", "content": user_reply})

            current_user_reply_for_task = user_reply
            current_agent_response_for_task = last_agent_response_for_check
            current_service_history = [msg for msg in service_history]
            current_summarized_history = summarized_history_str

            def generate_summary_task():
                if not getattr(args, "summary_user", False):
                    return None

                sum_start_time = time.time()
                sum_prompt = USER_TURN_SUMMARY_PROMPT.format(
                    user_instruction=user_instruction,
                    agent_response=current_agent_response_for_task,
                    user_response=current_user_reply_for_task,
                    previous_summary=current_summarized_history if current_summarized_history else "None"
                )
                print(f"Generating dialogue summary (Turn {turn})...")
                sum_msgs = [{"role": "user", "content": sum_prompt}]
                turn_summary, _, _ = call_llm(sum_msgs, agent_type="user", service_model_name=args.service_model_name)
                sum_time = time.time() - sum_start_time
                print(f"[Time] Summary generation (Turn {turn}): {sum_time:.3f} seconds")
                print(f"Turn {turn} Summary: {turn_summary}")
                return turn_summary

            def process_agent_task():
                nonlocal service_model_requests_count
                agent_start = time.time()
                inner_input_tokens = 0
                inner_output_tokens = 0
                inner_calls = 0
                inner_rounds = 0
                agent_final_reply = ""
                local_tool_logs = []
                local_dialogue_logs = []
                local_service_history = [msg for msg in current_service_history]
                total_tool_calls_so_far = tool_calls_count
                grounding_context = ""
                service_media_path = image_path
                service_extra_image_paths = []

                manual_grounding = _manual_grounding_for_task(args.scenario, args.scenario_number, task_id)
                if manual_grounding:
                    grounding_context, prep_input_tok, prep_output_tok = _prepare_manual_grounding_evidence(
                        manual_grounding,
                        current_user_reply_for_task,
                        image_path,
                        args
                    )
                    inner_input_tokens += prep_input_tok
                    inner_output_tokens += prep_output_tok
                    service_media_path = None
                    service_extra_image_paths = []
                    print(
                        "[Manual Grounding] "
                        f"Using current-turn text-only evidence for {args.scenario}{args.scenario_number} task {task_id}"
                    )

                if image_path and args.service_model_name != "manual" and not manual_grounding:
                    timeline_decision, timeline_input_tok, timeline_output_tok = _select_timeline_with_model(
                        image_path,
                        current_user_reply_for_task,
                        args
                    )
                    inner_input_tokens += timeline_input_tok
                    inner_output_tokens += timeline_output_tok

                    bbox_decision, bbox_input_tok, bbox_output_tok = _locate_bbox_with_qwen_vl(
                        image_path,
                        current_user_reply_for_task,
                        timeline_decision
                    )
                    inner_input_tokens += bbox_input_tok
                    inner_output_tokens += bbox_output_tok
                    timeline_decision = _merge_bbox_into_timeline(timeline_decision, bbox_decision)

                    grounding_user_text = (
                        f"Latest user message:\n{current_user_reply_for_task}\n\n"
                        f"Selected pointing-event timeline:\n{json.dumps(timeline_decision, ensure_ascii=False, indent=2)}\n\n"
                        "Extract visual grounding evidence for the item(s) the user is referring to. "
                        "Focus only on the selected event above, especially the final frame near selected_timestamp. "
                        "Use selected_finger_tip_point to locate the intended target. "
                        "Perform OCR first inside selected_label_bbox and the target product/package label; this label OCR is the primary catalog-matching evidence. "
                        "Use selected_below_price_tag_bbox only as weak price-alignment evidence, never as the final catalog price. "
                        "Do not use text from neighboring bottles or price tags outside those boxes as the target identity. "
                        "Ignore other pointing events and earlier frames in the same event unless needed for context."
                    )
                    grounding_messages = [
                        {"role": "system", "content": SERVICE_VISUAL_GROUNDING_PROMPT},
                        {
                            "role": "user",
                            "content": build_message_with_image(
                                grounding_user_text,
                                service_media_path,
                                use_vision=True,
                                service_model_name=args.service_model_name
                            )
                        }
                    ]
                    grounding_reply, grounding_input_tok, grounding_output_tok = call_llm(
                        grounding_messages,
                        agent_type="service",
                        service_model_name=args.service_model_name
                    )
                    inner_input_tokens += grounding_input_tok
                    inner_output_tokens += grounding_output_tok
                    grounding = _extract_json_object(grounding_reply)
                    print(f"[Visual Grounding] {json.dumps(grounding, ensure_ascii=False)}")

                    retrieval = {"calls": [], "results": [], "candidates": []}
                    grounding_skill = ""
                    grounding_skill_name = ""
                    if args.scenario == "retail":
                        grounding_skill_name = "retail_product_grounding"
                        grounding_skill = _load_grounding_skill(grounding_skill_name)
                        if grounding_skill:
                            print(f"[Grounding Skill] Loaded {grounding_skill_name}")
                        retrieval = _collect_retail_candidates(db, grounding, current_user_reply_for_task)
                        print(
                            "[Candidate Retrieval] "
                            f"{len(retrieval.get('calls', []))} calls, "
                            f"{len(retrieval.get('candidates', []))} candidates"
                        )

                    rerank_decision, rerank_input_tok, rerank_output_tok = _rerank_visual_candidate(
                        grounding,
                        retrieval,
                        current_user_reply_for_task,
                        local_service_history,
                        args,
                        grounding_skill
                    )
                    inner_input_tokens += rerank_input_tok
                    inner_output_tokens += rerank_output_tok
                    print(f"[Candidate Rerank] {json.dumps(rerank_decision, ensure_ascii=False)}")

                    grounding_context = _format_grounding_context(
                        grounding,
                        retrieval,
                        rerank_decision,
                        grounding_skill_name
                    )

                service_model_calls_this_turn = 0
                validation_retries_this_turn = 0
                validator_feedback_history = []
                force_first_tool_call = bool(manual_grounding)
                while True:
                    current_service_msgs = [{"role": "system", "content": service_agent_sys_prompt}]
                    if grounding_context:
                        current_service_msgs.append({"role": "system", "content": grounding_context})
                    retail_state_context = ""
                    if args.scenario == "retail":
                        retail_state_context = _retail_tool_state_context(
                            local_service_history,
                            grounding_context,
                            current_user_reply_for_task,
                        )
                    if retail_state_context:
                        current_service_msgs.append({"role": "system", "content": retail_state_context})
                    for i, msg in enumerate(local_service_history):
                        if i == 0 and msg["role"] == "user":
                            if service_model_calls_this_turn == 0:
                                content = _build_service_user_content(
                                    msg["content"],
                                    service_media_path,
                                    service_extra_image_paths,
                                    args
                                )
                            else:
                                content = msg["content"]
                            current_service_msgs.append({"role": "user", "content": content})
                        else:
                            current_service_msgs.append(msg)
                    current_service_msgs.extend(validator_feedback_history)

                    if args.service_model_name == "manual":
                        print(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] --- Manual Service Agent Turn ---")
                        print("Latest User Input:")
                        if service_history and service_history[-1]["role"] == "user":
                            print(service_history[-1]["content"])
                        print("Enter your response (text or JSON tool calls). Type 'END' on a new line to finish:")
                        ml_input = []
                        while True:
                            try:
                                line = input()
                                if line.strip() == "END":
                                    break
                                ml_input.append(line)
                            except EOFError:
                                break
                        agent_reply = "\n".join(ml_input)
                        agent_input_tokens = 0
                        agent_output_tokens = 0
                    else:
                        max_service_requests = getattr(
                            args,
                            "max_service_model_requests_per_task",
                            DEFAULT_MAX_SERVICE_MODEL_REQUESTS_PER_TASK
                        )
                        if max_service_requests > 0 and service_model_requests_count >= max_service_requests:
                            agent_final_reply = (
                                f"[Task stopped: service model requests reached {max_service_requests}]"
                            )
                            local_dialogue_logs.append({"role": "agent", "turn": turn, "content": agent_final_reply})
                            local_service_history.append({"role": "assistant", "content": agent_final_reply})
                            break
                        agent_reply, agent_input_tokens, agent_output_tokens = call_llm(current_service_msgs, agent_type="service", service_model_name=args.service_model_name)
                        inner_input_tokens += agent_input_tokens
                        inner_output_tokens += agent_output_tokens
                        service_model_calls_this_turn += 1
                        service_model_requests_count += 1
                    print(f"Tested Agent: {agent_reply}")

                    is_tool, tool_call_obj = check_tool_call(agent_reply)
                    if (
                        not is_tool
                        and inner_calls == 0
                        and not _looks_like_clarification_request(agent_reply)
                        and validation_retries_this_turn < SERVICE_VALIDATION_MAX_RETRIES
                    ):
                        validation_retries_this_turn += 1
                        print("[Format Guard] Rejected non-final natural language before any tool use.")
                        validator_feedback_history.append({
                            "role": "assistant",
                            "content": f"[Rejected draft, do not rely on this]\n{agent_reply}"
                        })
                        validator_feedback_history.append(_non_final_tool_use_rejection(agent_reply))
                        continue

                    if (
                        force_first_tool_call
                        and service_model_calls_this_turn == 1
                        and not is_tool
                        and validation_retries_this_turn < SERVICE_VALIDATION_MAX_RETRIES
                    ):
                        validation_retries_this_turn += 1
                        validator_feedback_history.append({
                            "role": "assistant",
                            "content": f"[Rejected draft, do not rely on this]\n{agent_reply}"
                        })
                        validator_feedback_history.append({
                            "role": "user",
                            "content": (
                                "Internal format check rejected the previous draft before it was accepted.\n"
                                "The caption/OCR/plan evidence has already been prepared internally and must not be emitted as the first formal answer.\n"
                                "If any available tool can progress this task, output ONLY a strict JSON tool-call array now. "
                                "Use pure natural language only if this is already the final answer or no tool can progress the request."
                            )
                        })
                        continue

                    validator_decision, validator_input_tok, validator_output_tok = _validate_service_output(
                        user_instruction,
                        current_user_reply_for_task,
                        local_service_history,
                        grounding_context,
                        agent_reply,
                        args,
                        tool_descriptions
                    )
                    inner_input_tokens += validator_input_tok
                    inner_output_tokens += validator_output_tok
                    print(f"[Service Validator] {json.dumps(validator_decision, ensure_ascii=False)}")
                    if not validator_decision.get("valid", True):
                        validation_retries_this_turn += 1
                        observed_fact = validator_decision.get("reason") or "The draft is not supported by available evidence."
                        high_level_suggestion = validator_decision.get("suggestion") or "Revise using another evidence-supported path."
                        validator_feedback_history.append({
                            "role": "assistant",
                            "content": f"[Rejected draft, do not rely on this]\n{agent_reply}"
                        })
                        validator_feedback_history.append({
                            "role": "user",
                            "content": (
                                "Internal evidence check rejected the previous draft before execution.\n"
                                f"Observed fact: {observed_fact}\n"
                                f"High-level suggestion: {high_level_suggestion}\n"
                                "Revise the response using only the user's intent, visual/OCR object identity, conversation history, and tool results. "
                                "Do not accept user/common-knowledge corrections about catalog facts when tool results already verified a conflicting value. "
                                + (
                                    "The relevant catalog facts are already tool-verified. If the current user request still requires an action, continue with the next required tool call using those verified facts; do not repeat rejected lookup/probing calls and do not switch back to user/common-knowledge facts."
                                    if validator_decision.get("rule_id") == "duplicate_instruction_with_completed_context"
                                    else ""
                                )
                            )
                        })
                        if validation_retries_this_turn >= SERVICE_VALIDATION_MAX_RETRIES:
                            agent_final_reply = ""
                            print(
                                "[Service Validator] Max retries reached; stopping this turn without "
                                "writing validator feedback as the final service answer."
                            )
                            break
                        continue

                    if is_tool:
                        if isinstance(tool_call_obj, list):
                            inner_calls += len(tool_call_obj)
                        else:
                            inner_calls += 1

                        tool_results = execute_tool(db, tool_call_obj)

                        local_tool_logs.append({
                            "turn": turn,
                            "calls": tool_call_obj if isinstance(tool_call_obj, list) else [tool_call_obj],
                            "results": tool_results
                        })

                        result_strings = []
                        for res in tool_results:
                            result_strings.append(res.get("content", str(res)))
                        combined_result = "; ".join(result_strings)

                        local_service_history.append({"role": "assistant", "content": agent_reply})
                        local_service_history.append({"role": "user", "content": f"Tool execution result: {combined_result}"})
                        validation_retries_this_turn = 0

                        if total_tool_calls_so_far + inner_calls > 200:
                            print(f"Tool calls count ({total_tool_calls_so_far + inner_calls}) exceeded 200, stopping interaction.")
                            agent_final_reply = "[Interaction stopped: tool calls exceeded 200]"
                            break

                        continue
                    else:
                        inner_rounds += 1
                        local_dialogue_logs.append({"role": "agent", "turn": turn, "content": agent_reply})
                        local_service_history.append({"role": "assistant", "content": agent_reply})
                        agent_final_reply = agent_reply
                        break

                agent_time = time.time() - agent_start
                print(f"[Time] Agent response generation (Turn {turn}): {agent_time:.3f} seconds")
                return {
                    "reply": agent_final_reply,
                    "input_tokens": inner_input_tokens,
                    "output_tokens": inner_output_tokens,
                    "calls": inner_calls,
                    "rounds": inner_rounds,
                    "tool_logs": local_tool_logs,
                    "dialogue_logs": local_dialogue_logs,
                    "time": agent_time,
                    "updated_history": local_service_history
                }

            future_summary = executor.submit(generate_summary_task)
            future_agent = executor.submit(process_agent_task)

            try:
                turn_summary = future_summary.result()
                agent_res = future_agent.result()
            except Exception as exc:
                print(f"[Task Error] Task {task_id} failed during turn {turn}: {exc}")
                history_log.setdefault("errors", []).append({
                    "turn": turn,
                    "stage": "agent_or_summary_future",
                    "error": repr(exc)
                })
                break

            input_tokens_total += agent_res["input_tokens"]
            output_tokens_total += agent_res["output_tokens"]
            tool_calls_count += agent_res["calls"]
            rounds_count += agent_res["rounds"]
            history_log["agent_response_time_seconds"] += agent_res["time"]
            history_log["tool_calls"].extend(agent_res["tool_logs"])
            history_log["dialogue"].extend(agent_res["dialogue_logs"])
            service_history = agent_res["updated_history"]

            last_agent_response_for_check = agent_res["reply"]

            if getattr(args, "summary_user", False) and turn_summary:
                summarized_history_str = f"Turn {turn} Dialogue Summary of completed steps: {turn_summary}\n"

            user_agent_sys_prompt = USER_TEXT_ONLY_PROMPT_EASY.format(
                user_instruction=user_instruction,
                image_description=image_description,
                original_user_response="",
                evaluation_feedback="",
                history_summary=summarized_history_str,
                service_agent_response=last_agent_response_for_check
            )

            user_messages[0]["content"] = user_agent_sys_prompt

            if getattr(args, "summary_user", False) and turn_summary:
                next_content = f"Please continue the conversation in the first person according to the original settings based on the summary and latest response."
                user_messages = [
                    {"role": "system", "content": user_agent_sys_prompt},
                    {"role": "user", "content": build_message_with_image(next_content, image_path, use_vision)}
                ]
            else:
                user_messages.append({"role": "user", "content": last_agent_response_for_check})

        executor.shutdown(wait=True)

        history_log["rounds_count"] = rounds_count
        history_log["input_tokens"] = input_tokens_total
        history_log["output_tokens"] = output_tokens_total
        history_log["tool_calls_count"] = tool_calls_count
        history_log["service_model_requests_count"] = service_model_requests_count

        user_performance = {}
        if valid_evaluation_count > 0:
            for k, v in accumulated_original_scores.items():
                user_performance[f"original_{k}_avg"] = round(v / valid_evaluation_count, 2)
            for k, v in accumulated_final_scores.items():
                user_performance[f"final_{k}_avg"] = round(v / valid_evaluation_count, 2)
        history_log["user_performance"] = user_performance

        end_time = time.time()
        execution_time = round(end_time - start_time, 3)
        history_log["execution_time_seconds"] = execution_time
        all_results.append(history_log)
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(all_results, f, ensure_ascii=False, indent=2)
        print(f"[Checkpoint] Saved {len(all_results)} result(s) to: {output_path}")

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    print(f"\nCompleted! Results saved to: {output_path}")
    print(f"Statistics Summary: ")
    for result in all_results:
        print(f"  Task {result.get('task_id')}: {result['rounds_count']} dialogue rounds, {result['input_tokens']} input tokens, {result['output_tokens']} output tokens, {result['tool_calls_count']} tool calls, {result['execution_time_seconds']} seconds")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run dialogue simulation in easy mode")
    parser.add_argument(
        "--service_model_name",
        default=SERVICE_MODEL_NAME,
        help="Tested agent model name (default: configured in service_agent_config.py)"
    )

    parser.add_argument(
        "--scenario",
        choices=["retail", "kitchen", "restaurant", "order"],
        default="retail",
        help="Task scenario"
    )

    parser.add_argument(
        "--scenario_number",
        type=int,
        default=1,
        help="Scenario number"
    )

    parser.add_argument(
        "--multi_agent_user",
        action="store_true",
        help="When True, use LLM to check if user response contradicts the task and correct if contradictory"
    )

    parser.add_argument(
        "--summary_user",
        action="store_true",
        help="When True, add a summary module after the user answers to avoid lengthy history information"
    )

    parser.add_argument(
        "--num_tasks",
        type=int,
        default=0,
        help="Number of tasks to test from the beginning of the scenario. 0 means test all tasks."
    )

    parser.add_argument(
        "--start_task",
        type=int,
        default=1,
        help="1-based task id to start from. Existing earlier results are preserved when the output file exists."
    )

    parser.add_argument(
        "--max_service_model_requests_per_task",
        type=int,
        default=DEFAULT_MAX_SERVICE_MODEL_REQUESTS_PER_TASK,
        help="Maximum service-agent model calls per task. 0 means unlimited."
    )

    parser.add_argument(
        "--output_dir",
        default="./results",
        help="Directory for result JSON files. Defaults to ./results."
    )

    args = parser.parse_args()

    INPUT_JSON = f"./scenarios/final/{args.scenario}{args.scenario_number}.json"
    TOOL_INFO_JSON = f"./tools/{args.scenario}/{args.scenario}_tools.json"
    OUTPUT_JSON = os.path.join(
        args.output_dir,
        args.service_model_name,
        f"{args.scenario}{args.scenario_number}_easy.json"
    )
    os.makedirs(os.path.dirname(OUTPUT_JSON), exist_ok=True)

    run_simulation(INPUT_JSON, TOOL_INFO_JSON, OUTPUT_JSON, args=args, service_model_name=args.service_model_name)
