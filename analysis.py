"""Kaplan–Meier failure-free probability from the pre-aggregated counts.

Only ``summary_counts`` is read; ``battery_ledger`` is not touched by queries.

Counting rules
--------------
* Age is whole days from ``activation_date`` to the follow-up date.
* A subject enters at its follow-up start age and is exposed from that age.
* The follow-up window is clipped at the chosen cutoff date.  A failure after
  the cutoff is treated as censoring at the cutoff; a follow-up end after the
  cutoff becomes the cutoff day itself.
* Subjects whose follow-up start is after the cutoff are not yet enrolled and
  contribute nothing.
* At a given age, subjects entering that day are included in the risk set and
  subjects whose follow-up ends that day remain in the risk set, so failures
  are counted after entry and after the end-of-day censoring rule.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date
from typing import Iterable

Row = tuple[str, int, int, int, int]  # activation_date, entry, end, event, count


@dataclass(frozen=True)
class SurvivalPoint:
    age_days: int
    at_risk: int
    failures: int
    censored: int
    survival: float


@dataclass(frozen=True)
class SurvivalResult:
    cutoff: date
    points: list[SurvivalPoint]
    enrolled_subjects: int
    risk_set_at_cutoff: int

    @property
    def has_failures(self) -> bool:
        return bool(self.points)


def summarize(rows: Iterable[Row], cutoff: date) -> SurvivalResult:
    """Return KM statistics at every age where a failure was observed."""

    failures_by_age: dict[int, int] = {}
    censored_by_age: dict[int, int] = {}
    # Compressed risk-set contributions: one (entry, end, count) interval per
    # summary row, with the follow-up window clipped to the cutoff.
    intervals: list[tuple[int, int, int]] = []
    enrolled = 0
    risk_at_cutoff = 0

    for activation_text, entry_age, end_age, event, count in rows:
        if count <= 0:
            continue
        activation = date.fromisoformat(activation_text)
        cutoff_age = (cutoff - activation).days

        # Not yet enrolled at the cutoff.
        if entry_age > cutoff_age:
            continue

        enrolled += count
        if entry_age <= cutoff_age <= end_age:
            risk_at_cutoff += count

        if end_age > cutoff_age:
            end_age = cutoff_age
            event = 0  # a failure after the cutoff is censored at the cutoff
        if end_age < entry_age:
            end_age = entry_age

        intervals.append((entry_age, end_age, count))
        if event:
            failures_by_age[end_age] = failures_by_age.get(end_age, 0) + count
        else:
            censored_by_age[end_age] = censored_by_age.get(end_age, 0) + count

    failure_ages = sorted(failures_by_age)

    # Difference array over failure ages: each subject is at risk on every
    # failure age from its entry age through its end age (inclusive).
    size = len(failure_ages)
    diff = [0] * (size + 1)
    for entry_age, end_age, count in intervals:
        left = bisect_left(failure_ages, entry_age)
        right = bisect_right(failure_ages, end_age)
        if left < right:
            diff[left] += count
            diff[right] -= count

    points: list[SurvivalPoint] = []
    at_risk = 0
    survival = 1.0
    for index, age in enumerate(failure_ages):
        at_risk += diff[index]
        failures = failures_by_age[age]
        censored = censored_by_age.get(age, 0)
        survival *= 1.0 - failures / at_risk if at_risk > 0 else 1.0
        points.append(
            SurvivalPoint(
                age_days=age,
                at_risk=at_risk,
                failures=failures,
                censored=censored,
                survival=survival,
            )
        )

    return SurvivalResult(
        cutoff=cutoff,
        points=points,
        enrolled_subjects=enrolled,
        risk_set_at_cutoff=risk_at_cutoff,
    )
