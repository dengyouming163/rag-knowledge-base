"""模型价格配置解析与 Token 成本估算的纯函数。"""

from __future__ import annotations

import json
import math
import os
from decimal import Decimal, DecimalException, InvalidOperation

_PRICE_KEYS = ("prompt_per_1m", "completion_per_1m")
_TOKENS_PER_PRICE_UNIT = Decimal(1_000_000)


def parse_pricing(raw: str) -> dict:
    """解析并校验 OPS_MODEL_PRICING_JSON，价格统一转换为 Decimal。"""
    if raw == "":
        return {}
    try:
        data = json.loads(raw, parse_float=Decimal, parse_constant=Decimal)
    except (json.JSONDecodeError, InvalidOperation) as exc:
        raise ValueError("模型价格配置必须是合法 JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("模型价格配置顶层必须是 object")

    pricing = {}
    for model, config in data.items():
        if not isinstance(config, dict) or set(config) != set(_PRICE_KEYS):
            raise ValueError(f"模型 {model!r} 的价格配置必须且只能包含两个价格字段")
        normalized = {}
        for key in _PRICE_KEYS:
            value = config[key]
            if isinstance(value, bool):
                raise ValueError(f"模型 {model!r} 的 {key} 必须是有限非负数")
            try:
                price = Decimal(str(value))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ValueError(f"模型 {model!r} 的 {key} 必须是有限非负数") from exc
            if not price.is_finite() or price < 0:
                raise ValueError(f"模型 {model!r} 的 {key} 必须是有限非负数")
            normalized[key] = price
        pricing[model] = normalized
    return pricing


def load_pricing_from_env() -> dict:
    """只从 OPS_MODEL_PRICING_JSON 加载价格，非法配置向上抛出 ValueError。"""
    return parse_pricing(os.environ.get("OPS_MODEL_PRICING_JSON", ""))


def estimate_cost(rows: list[dict], pricing: dict) -> dict:
    """按模型价格估算成本，并分别统计已定价与未定价 Token。"""
    pricing_configured = bool(pricing)
    pricing_complete = pricing_configured
    priced_tokens = 0
    unpriced_tokens = 0
    cost = Decimal(0)

    for row in rows:
        prompt_tokens = int(row.get("prompt_tokens") or 0)
        completion_tokens = int(row.get("completion_tokens") or 0)
        token_count = prompt_tokens + completion_tokens
        if token_count == 0:
            continue
        model_pricing = pricing.get(row.get("model"))
        if model_pricing is None:
            pricing_complete = False
            unpriced_tokens += token_count
            continue

        priced_tokens += token_count
        try:
            cost += (
                Decimal(prompt_tokens) * model_pricing["prompt_per_1m"]
                + Decimal(completion_tokens) * model_pricing["completion_per_1m"]
            ) / _TOKENS_PER_PRICE_UNIT
        except DecimalException as exc:
            raise ValueError("模型成本超出可表示范围") from exc

    estimated_cost = None
    if pricing_configured:
        try:
            estimated_cost = round(float(cost), 6)
        except (OverflowError, ValueError) as exc:
            raise ValueError("模型成本超出可表示范围") from exc
        if not math.isfinite(estimated_cost):
            raise ValueError("模型成本超出可表示范围")

    return {
        "pricing_configured": pricing_configured,
        "pricing_complete": pricing_complete,
        "estimated_cost_usd": estimated_cost,
        "priced_tokens": priced_tokens,
        "unpriced_tokens": unpriced_tokens,
    }
