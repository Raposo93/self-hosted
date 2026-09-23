"""Domain types and constructors for collected and aggregated RouterOS data."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional, TypedDict

SOURCES = ("local", "crowdsec")
METRICS = ("packets", "bytes")
PeriodKind = Literal["day", "week", "month"]


class Snapshot(TypedDict):
    counters: dict[str, dict[str, int]]
    sizes: dict[str, int]
    uptime: int


class Aggregate(TypedDict):
    totals: dict[str, dict[str, int]]
    max_sizes: dict[str, int]
    last_sizes: dict[str, int]
    samples: int
    router_reboots: int
    counter_resets: int
    rule_rebaselines: int


class Period(Aggregate):
    """Persisted aggregate whose start also identifies its calendar window."""

    start: str


class State(TypedDict):
    version: int
    period: Period
    pending: list[Period]
    counters: dict[str, dict[str, int]]
    last_sample_at: Optional[str]
    last_uptime: Optional[int]


@dataclass(frozen=True)
class PeriodWindow:
    """Concrete local-calendar interval used for coverage calculations."""

    start: str
    end: str
    kind: PeriodKind


def empty_period(start: str) -> Period:
    return {
        "start": start,
        "totals": {source: {metric: 0 for metric in METRICS} for source in SOURCES},
        "max_sizes": {source: 0 for source in SOURCES},
        "last_sizes": {source: 0 for source in SOURCES},
        "samples": 0,
        "router_reboots": 0,
        "counter_resets": 0,
        "rule_rebaselines": 0,
    }


def initial_state(start: str) -> State:
    return {
        "version": 1,
        "period": empty_period(start),
        "pending": [],
        "counters": {},
        "last_sample_at": None,
        "last_uptime": None,
    }
