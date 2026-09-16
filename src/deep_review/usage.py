from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from threading import Lock
from typing import Any, cast

from strands.hooks import AfterInvocationEvent, HookProvider, HookRegistry

from deep_review.configuration import TokenPricing

LOGGER = logging.getLogger(__name__)
_MILLION = Decimal("1000000")


@dataclass(frozen=True)
class UsageRecord:
    model_id: str
    pricing: TokenPricing | None
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_read_input_tokens: int


class UsageCollector:
    def __init__(self) -> None:
        self._lock = Lock()
        self._records: list[UsageRecord] = []

    def record(
        self,
        model_id: str,
        pricing: TokenPricing | None,
        usage: Mapping[str, Any],
    ) -> None:
        record = UsageRecord(
            model_id=model_id,
            pricing=pricing,
            input_tokens=_token_count(usage.get("inputTokens")),
            output_tokens=_token_count(usage.get("outputTokens")),
            total_tokens=_token_count(usage.get("totalTokens")),
            cache_read_input_tokens=_token_count(usage.get("cacheReadInputTokens")),
        )
        if not any(
            (
                record.input_tokens,
                record.output_tokens,
                record.total_tokens,
                record.cache_read_input_tokens,
            )
        ):
            return
        with self._lock:
            self._records.append(record)

    def log_summary(self) -> None:
        with self._lock:
            records = list(self._records)
        if not records:
            return

        grouped: dict[str, list[UsageRecord]] = defaultdict(list)
        for record in records:
            grouped[record.model_id].append(record)

        all_costs: list[Decimal | None] = []
        totals = _UsageTotals()
        for model_id in sorted(grouped):
            model_records = grouped[model_id]
            usage = _sum_usage(model_records)
            costs = [_cost(record) for record in model_records]
            all_costs.extend(costs)
            model_cost = (
                sum(cast(list[Decimal], costs), Decimal(0))
                if all(cost is not None for cost in costs)
                else None
            )
            LOGGER.info(_summary_line("LLM usage", model_id, usage, model_cost))
            totals.add(usage)

        total_cost = (
            sum(cast(list[Decimal], all_costs), Decimal(0))
            if all(cost is not None for cost in all_costs)
            else None
        )
        LOGGER.info(_summary_line("LLM usage total", None, totals, total_cost))


class UsageTrackingHooks(HookProvider):
    def __init__(
        self,
        collector: UsageCollector,
        model_id: str,
        pricing: TokenPricing | None,
    ) -> None:
        self._collector = collector
        self._model_id = model_id
        self._pricing = pricing

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(AfterInvocationEvent, self._after_invocation)

    def _after_invocation(self, event: AfterInvocationEvent) -> None:
        metrics = (
            event.result.metrics
            if event.result is not None
            else event.agent.event_loop_metrics
        )
        invocation = metrics.latest_agent_invocation
        if invocation is not None:
            self._collector.record(self._model_id, self._pricing, invocation.usage)


@dataclass
class _UsageTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_read_input_tokens: int = 0

    def add(self, usage: _UsageTotals) -> None:
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.total_tokens += usage.total_tokens
        self.cache_read_input_tokens += usage.cache_read_input_tokens


def _token_count(value: Any) -> int:
    return value if isinstance(value, int) and value > 0 else 0


def _sum_usage(records: list[UsageRecord]) -> _UsageTotals:
    return _UsageTotals(
        input_tokens=sum(record.input_tokens for record in records),
        output_tokens=sum(record.output_tokens for record in records),
        total_tokens=sum(record.total_tokens for record in records),
        cache_read_input_tokens=sum(record.cache_read_input_tokens for record in records),
    )


def _cost(record: UsageRecord) -> Decimal | None:
    pricing = record.pricing
    if pricing is None:
        return None
    cache_tokens = min(record.input_tokens, record.cache_read_input_tokens)
    if cache_tokens and pricing.cache_read_input_usd_per_million_tokens is None:
        return None
    uncached_input = record.input_tokens - cache_tokens
    cache_rate = pricing.cache_read_input_usd_per_million_tokens or Decimal(0)
    return (
        Decimal(uncached_input) * pricing.input_usd_per_million_tokens
        + Decimal(cache_tokens) * cache_rate
        + Decimal(record.output_tokens) * pricing.output_usd_per_million_tokens
    ) / _MILLION


def _summary_line(
    prefix: str,
    model_id: str | None,
    usage: _UsageTotals,
    cost: Decimal | None,
) -> str:
    parts = [prefix]
    if model_id is not None:
        parts.append(f"model={model_id}")
    parts.extend(
        (
            f"input={usage.input_tokens:,}",
            f"cache_read_input={usage.cache_read_input_tokens:,}",
            f"output={usage.output_tokens:,}",
            f"total={usage.total_tokens:,}",
        )
    )
    if cost is not None:
        parts.append(f"cost=${cost:.6f} USD")
    return ", ".join(parts)
