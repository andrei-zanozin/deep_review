from __future__ import annotations

import logging
from decimal import Decimal
from types import SimpleNamespace
from typing import cast

from strands import Agent
from strands.hooks import AfterInvocationEvent
from strands.telemetry.metrics import EventLoopMetrics

from deep_review.configuration import TokenPricing
from deep_review.usage import UsageCollector, UsageTrackingHooks


def pricing() -> TokenPricing:
    return TokenPricing(
        input_usd_per_million_tokens=Decimal("1"),
        output_usd_per_million_tokens=Decimal("4"),
        cache_read_input_usd_per_million_tokens=Decimal("0.25"),
    )


def test_summary_groups_models_and_prices_cached_input(caplog) -> None:
    collector = UsageCollector()
    collector.record(
        "review-model",
        pricing(),
        {
            "inputTokens": 1_000_000,
            "cacheReadInputTokens": 200_000,
            "outputTokens": 500_000,
            "totalTokens": 1_500_000,
        },
    )
    collector.record(
        "review-model",
        pricing(),
        {"inputTokens": 100, "outputTokens": 100, "totalTokens": 200},
    )

    with caplog.at_level(logging.INFO, logger="deep_review.usage"):
        collector.log_summary()

    messages = [record.getMessage() for record in caplog.records]
    assert messages == [
        "LLM usage, model=review-model, input=1,000,100, cache_read_input=200,000, "
        "output=500,100, total=1,500,200, cost=$2.850500 USD",
        "LLM usage total, input=1,000,100, cache_read_input=200,000, output=500,100, "
        "total=1,500,200, cost=$2.850500 USD",
    ]


def test_summary_omits_total_cost_when_any_model_is_unpriced(caplog) -> None:
    collector = UsageCollector()
    collector.record("priced", pricing(), {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2})
    collector.record("unpriced", None, {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2})

    with caplog.at_level(logging.INFO, logger="deep_review.usage"):
        collector.log_summary()

    messages = [record.getMessage() for record in caplog.records]
    assert messages[0].endswith("cost=$0.000005 USD")
    assert "cost=" not in messages[1]
    assert "cost=" not in messages[2]


def test_summary_is_silent_without_reported_usage(caplog) -> None:
    collector = UsageCollector()
    collector.record(
        "review-model",
        pricing(),
        {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
    )

    with caplog.at_level(logging.INFO, logger="deep_review.usage"):
        collector.log_summary()

    assert not caplog.records


def test_after_invocation_hook_records_usage_when_result_is_unavailable(caplog) -> None:
    metrics = EventLoopMetrics()
    metrics.reset_usage_metrics()
    metrics.update_usage({"inputTokens": 10, "outputTokens": 5, "totalTokens": 15})
    collector = UsageCollector()
    hook = UsageTrackingHooks(collector, "review-model", pricing())
    hook._after_invocation(
        AfterInvocationEvent(agent=cast(Agent, SimpleNamespace(event_loop_metrics=metrics)))
    )

    with caplog.at_level(logging.INFO, logger="deep_review.usage"):
        collector.log_summary()

    assert caplog.records[-1].getMessage().endswith("cost=$0.000030 USD")
