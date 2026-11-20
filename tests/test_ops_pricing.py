"""模型价格解析与 Token 成本估算的纯函数测试。"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from src.ops_pricing import estimate_cost, load_pricing_from_env, parse_pricing


def test_parse_pricing_returns_normalized_decimal_prices() -> None:
    raw = json.dumps({
        "m1": {"prompt_per_1m": 1.25, "completion_per_1m": 2},
        "m2": {"prompt_per_1m": 0, "completion_per_1m": "0.5"},
    })

    assert parse_pricing(raw) == {
        "m1": {
            "prompt_per_1m": Decimal("1.25"),
            "completion_per_1m": Decimal("2"),
        },
        "m2": {
            "prompt_per_1m": Decimal("0"),
            "completion_per_1m": Decimal("0.5"),
        },
    }


@pytest.mark.parametrize("raw", ["", "{}"])
def test_parse_pricing_accepts_empty_configuration(raw: str) -> None:
    assert parse_pricing(raw) == {}


@pytest.mark.parametrize(
    "raw",
    [
        "{",
        "[]",
        "null",
        '{"m": 1}',
        '{"m": []}',
        '{"m": {"prompt_per_1m": 1}}',
        '{"m": {"completion_per_1m": 1}}',
        '{"m": {"prompt_per_1m": 1, "completion_per_1m": 2, "extra": 3}}',
        '{"m": {"prompt_per_1m": -1, "completion_per_1m": 2}}',
        '{"m": {"prompt_per_1m": 1, "completion_per_1m": -2}}',
        '{"m": {"prompt_per_1m": NaN, "completion_per_1m": 2}}',
        '{"m": {"prompt_per_1m": 1, "completion_per_1m": Infinity}}',
        '{"m": {"prompt_per_1m": true, "completion_per_1m": 2}}',
        '{"m": {"prompt_per_1m": "expensive", "completion_per_1m": 2}}',
        '{"m": {"prompt_per_1m": [], "completion_per_1m": 2}}',
    ],
)
def test_parse_pricing_rejects_invalid_configuration(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_pricing(raw)


def test_load_pricing_reads_only_ops_model_pricing_json(monkeypatch) -> None:
    monkeypatch.delenv("OPS_MODEL_PRICING_JSON", raising=False)
    monkeypatch.setenv("LLM_INPUT_PRICE_PER_1K", "100")
    monkeypatch.setenv("LLM_OUTPUT_PRICE_PER_1K", "200")
    assert load_pricing_from_env() == {}

    monkeypatch.setenv("OPS_MODEL_PRICING_JSON", "")
    assert load_pricing_from_env() == {}

    raw = '{"m": {"prompt_per_1m": 1, "completion_per_1m": 2}}'
    monkeypatch.setenv("OPS_MODEL_PRICING_JSON", raw)
    assert load_pricing_from_env() == parse_pricing(raw)


def test_load_pricing_exposes_invalid_configuration(monkeypatch) -> None:
    monkeypatch.setenv("OPS_MODEL_PRICING_JSON", "{")
    with pytest.raises(ValueError):
        load_pricing_from_env()


def test_estimate_cost_with_complete_pricing() -> None:
    pricing = parse_pricing(json.dumps({
        "m1": {"prompt_per_1m": 1.0, "completion_per_1m": 2.0},
        "m2": {"prompt_per_1m": 0.333333333, "completion_per_1m": 0},
    }))

    result = estimate_cost([
        {"model": "m1", "prompt_tokens": 1000, "completion_tokens": 500},
        {"model": "m2", "prompt_tokens": 1000, "completion_tokens": 0},
    ], pricing)

    assert result == {
        "pricing_configured": True,
        "pricing_complete": True,
        "estimated_cost_usd": 0.002333,
        "priced_tokens": 2500,
        "unpriced_tokens": 0,
    }


def test_cost_marks_partial_coverage() -> None:
    pricing = parse_pricing(json.dumps({
        "m1": {"prompt_per_1m": 1.0, "completion_per_1m": 2.0}
    }))
    result = estimate_cost([
        {"model": "m1", "prompt_tokens": 1000, "completion_tokens": 500},
        {"model": "unknown", "prompt_tokens": 100, "completion_tokens": 50},
    ], pricing)
    assert result["estimated_cost_usd"] == 0.002
    assert result["pricing_configured"] is True
    assert result["pricing_complete"] is False
    assert result["priced_tokens"] == 1500
    assert result["unpriced_tokens"] == 150


def test_cost_with_only_unknown_models_is_zero_but_incomplete() -> None:
    pricing = parse_pricing(json.dumps({
        "m1": {"prompt_per_1m": 1.0, "completion_per_1m": 2.0}
    }))

    result = estimate_cost([
        {"model": "unknown", "prompt_tokens": 100, "completion_tokens": 50},
    ], pricing)

    assert result == {
        "pricing_configured": True,
        "pricing_complete": False,
        "estimated_cost_usd": 0.0,
        "priced_tokens": 0,
        "unpriced_tokens": 150,
    }


def test_zero_prices_are_distinct_from_unconfigured_pricing() -> None:
    pricing = parse_pricing(json.dumps({
        "free": {"prompt_per_1m": 0, "completion_per_1m": 0}
    }))
    rows = [{"model": "free", "prompt_tokens": 1, "completion_tokens": 1}]

    assert estimate_cost(rows, pricing) == {
        "pricing_configured": True,
        "pricing_complete": True,
        "estimated_cost_usd": 0.0,
        "priced_tokens": 2,
        "unpriced_tokens": 0,
    }


def test_no_pricing_returns_null_cost() -> None:
    result = estimate_cost([
        {"model": "m", "prompt_tokens": 1, "completion_tokens": 1}
    ], {})

    assert result == {
        "pricing_configured": False,
        "pricing_complete": False,
        "estimated_cost_usd": None,
        "priced_tokens": 0,
        "unpriced_tokens": 2,
    }
