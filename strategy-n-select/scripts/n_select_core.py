#!/usr/bin/env python3
"""Deterministic core logic for the strategy-n-select skill."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import random
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


DEFAULT_N_VALUES = [1, 3, 5, 10, 20, 100, 300, 1000]
DEFAULT_MATCHES = 100
DEFAULT_SEED = 2026
SCHEMA_VERSION = 3


class ContractError(ValueError):
    """Raised when an input or frozen artifact violates the study contract."""


def parse_iso_date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{field} must be an ISO date (YYYY-MM-DD), got {value!r}") from exc


def load_study_config(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"Cannot read valid study config {path}: {exc}") from exc

    for section in ("development", "holdout"):
        if not isinstance(data.get(section), dict):
            raise ContractError(f"study config requires object {section!r}")
        for boundary in ("start", "end"):
            if boundary not in data[section]:
                raise ContractError(f"study config requires {section}.{boundary}")

    development_start = parse_iso_date(data["development"]["start"], "development.start")
    development_end = parse_iso_date(data["development"]["end"], "development.end")
    holdout_start = parse_iso_date(data["holdout"]["start"], "holdout.start")
    holdout_end = parse_iso_date(data["holdout"]["end"], "holdout.end")
    if development_start > development_end:
        raise ContractError("development.start must be on or before development.end")
    if holdout_start > holdout_end:
        raise ContractError("holdout.start must be on or before holdout.end")
    if development_end >= holdout_start:
        raise ContractError("development must end before holdout starts; overlap is forbidden")

    consumed = data["holdout"].get("consumed_for_selection")
    if not isinstance(consumed, bool):
        raise ContractError("holdout.consumed_for_selection must be explicitly true or false")
    return data


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def object_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def prepare_output_dir(path: Path) -> None:
    if path.exists() and any(path.iterdir()):
        raise ContractError(f"Output directory is not empty: {path}")
    path.mkdir(parents=True, exist_ok=True)


def write_csv(path: Path, fieldnames: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _required_columns(reader: csv.DictReader, required: set[str], path: Path) -> None:
    found = set(reader.fieldnames or [])
    missing = sorted(required - found)
    if missing:
        raise ContractError(f"{path} is missing required columns: {', '.join(missing)}")


def read_scores(path: Path, metric: str) -> dict[str, float]:
    scores: dict[str, float] = {}
    seen: set[tuple[str, str]] = set()
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        _required_columns(reader, {"candidate_id", "metric", "score"}, path)
        for line_number, row in enumerate(reader, start=2):
            candidate_id = (row.get("candidate_id") or "").strip()
            row_metric = (row.get("metric") or "").strip()
            if not candidate_id or not row_metric:
                raise ContractError(f"{path}:{line_number} has blank candidate_id or metric")
            key = (candidate_id, row_metric)
            if key in seen:
                raise ContractError(f"{path}:{line_number} duplicates candidate_id + metric {key!r}")
            seen.add(key)
            if row_metric != metric:
                continue
            raw_score = (row.get("score") or "").strip()
            if not raw_score:
                score = math.nan
            else:
                try:
                    score = float(raw_score)
                except ValueError as exc:
                    raise ContractError(f"{path}:{line_number} has non-numeric score {raw_score!r}") from exc
            scores[candidate_id] = score
    if not scores:
        raise ContractError(f"No rows found for metric {metric!r} in {path}")
    return scores


def parse_positive_ints(raw: str, field: str) -> list[int]:
    values: list[int] = []
    seen: set[int] = set()
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            value = int(token)
        except ValueError as exc:
            raise ContractError(f"{field} must contain comma-separated integers") from exc
        if value <= 0:
            raise ContractError(f"{field} values must be positive, got {value}")
        if value in seen:
            raise ContractError(f"{field} contains duplicate value {value}")
        seen.add(value)
        values.append(value)
    if not values:
        raise ContractError(f"{field} must contain at least one value")
    return values


def run_selection(
    scores: Mapping[str, float],
    n_values: Sequence[int],
    matches: int,
    seed: int,
    direction: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    if direction not in {"max", "min"}:
        raise ContractError("direction must be 'max' or 'min'")
    if matches <= 0:
        raise ContractError("matches must be positive")
    candidate_ids = sorted(scores)
    if not candidate_ids:
        raise ContractError("candidate pool is empty")
    for n in n_values:
        if n > len(candidate_ids):
            raise ContractError(
                f"N={n} exceeds candidate count {len(candidate_ids)}; explicitly override the N list"
            )

    rng = random.Random(seed)
    groups: list[dict[str, Any]] = []
    winners: list[dict[str, Any]] = []
    counts_by_n: dict[int, Counter[str]] = {}
    for n in n_values:
        counts: Counter[str] = Counter()
        counts_by_n[n] = counts
        for round_number in range(1, matches + 1):
            group = rng.sample(candidate_ids, n)
            for position, candidate_id in enumerate(group, start=1):
                groups.append(
                    {
                        "n": n,
                        "round": round_number,
                        "position": position,
                        "candidate_id": candidate_id,
                    }
                )
            valid = [candidate_id for candidate_id in group if math.isfinite(scores[candidate_id])]
            if not valid:
                winners.append(
                    {
                        "n": n,
                        "round": round_number,
                        "status": "invalid_group",
                        "winner_candidate_id": "",
                        "winner_score": "",
                    }
                )
                continue
            if direction == "max":
                best_score = max(scores[candidate_id] for candidate_id in valid)
            else:
                best_score = min(scores[candidate_id] for candidate_id in valid)
            tied = [candidate_id for candidate_id in valid if scores[candidate_id] == best_score]
            winner = min(tied)
            counts[winner] += 1
            winners.append(
                {
                    "n": n,
                    "round": round_number,
                    "status": "winner",
                    "winner_candidate_id": winner,
                    "winner_score": format(best_score, ".17g"),
                }
            )

    members: list[dict[str, Any]] = []
    for n in n_values:
        valid_matches = sum(counts_by_n[n].values())
        for candidate_id, count in sorted(counts_by_n[n].items(), key=lambda item: (-item[1], item[0])):
            members.append(
                {
                    "n": n,
                    "candidate_id": candidate_id,
                    "metric_score": format(scores[candidate_id], ".17g"),
                    "win_count": count,
                    "slot_weight": format(count / valid_matches, ".17g") if valid_matches else "",
                }
            )
    return groups, winners, members


def read_member_records(path: Path) -> dict[int, list[dict[str, Any]]]:
    result: dict[int, list[dict[str, Any]]] = defaultdict(list)
    seen: set[tuple[int, str]] = set()
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        _required_columns(reader, {"n", "candidate_id", "metric_score", "win_count"}, path)
        for line_number, row in enumerate(reader, start=2):
            try:
                n = int(row["n"])
                metric_score = float(row["metric_score"])
                win_count = int(row["win_count"])
            except ValueError as exc:
                raise ContractError(f"{path}:{line_number} has invalid n, metric_score, or win_count") from exc
            candidate_id = row["candidate_id"].strip()
            if not candidate_id or not math.isfinite(metric_score) or win_count <= 0:
                raise ContractError(f"{path}:{line_number} has invalid member data")
            key = (n, candidate_id)
            if key in seen:
                raise ContractError(f"{path}:{line_number} duplicates N + candidate_id")
            seen.add(key)
            result[n].append(
                {
                    "n": n,
                    "candidate_id": candidate_id,
                    "metric_score": metric_score,
                    "win_count": win_count,
                }
            )
    if not result:
        raise ContractError(f"No valid members found in {path}")
    return dict(result)


def read_members(path: Path) -> dict[int, dict[str, int]]:
    result: dict[int, dict[str, int]] = defaultdict(dict)
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        _required_columns(reader, {"n", "candidate_id", "win_count"}, path)
        for line_number, row in enumerate(reader, start=2):
            try:
                n = int(row["n"])
                win_count = int(row["win_count"])
            except ValueError as exc:
                raise ContractError(f"{path}:{line_number} has invalid n or win_count") from exc
            candidate_id = row["candidate_id"].strip()
            if not candidate_id or win_count <= 0:
                raise ContractError(f"{path}:{line_number} has invalid member data")
            if candidate_id in result[n]:
                raise ContractError(f"{path}:{line_number} duplicates N + candidate_id")
            result[n][candidate_id] = win_count
    if not result:
        raise ContractError(f"No valid members found in {path}")
    return dict(result)


def read_development_signals(
    path: Path,
    development_start: date,
    development_end: date,
    required_candidates: set[str],
) -> dict[str, dict[str, frozenset[str]]]:
    if not required_candidates:
        raise ContractError("No members supplied for development-signal validation")
    raw: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    seen: set[tuple[str, str, str]] = set()
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        _required_columns(reader, {"rebalance_date", "candidate_id", "asset_id"}, path)
        for line_number, row in enumerate(reader, start=2):
            raw_date = (row.get("rebalance_date") or "").strip()
            signal_date = parse_iso_date(raw_date, f"{path}:{line_number} rebalance_date")
            if not development_start <= signal_date <= development_end:
                raise ContractError(
                    f"{path}:{line_number} is outside the frozen development period; "
                    "decorrelation input must contain development signals only"
                )
            candidate_id = (row.get("candidate_id") or "").strip()
            asset_id = (row.get("asset_id") or "").strip()
            if not candidate_id or not asset_id:
                raise ContractError(f"{path}:{line_number} has blank candidate_id or asset_id")
            key = (raw_date, candidate_id, asset_id)
            if key in seen:
                raise ContractError(
                    f"{path}:{line_number} duplicates rebalance_date + candidate_id + asset_id"
                )
            seen.add(key)
            if candidate_id in required_candidates:
                raw[candidate_id][raw_date].add(asset_id)

    missing_candidates = sorted(required_candidates - set(raw))
    if missing_candidates:
        raise ContractError(
            "Development signals are missing required members: " + ", ".join(missing_candidates)
        )
    reference_candidate = min(required_candidates)
    reference_dates = set(raw[reference_candidate])
    if not reference_dates:
        raise ContractError("Development signal calendar is empty")
    for candidate_id in sorted(required_candidates):
        candidate_dates = set(raw[candidate_id])
        if candidate_dates != reference_dates:
            missing_dates = sorted(reference_dates - candidate_dates)
            extra_dates = sorted(candidate_dates - reference_dates)
            raise ContractError(
                f"Development rebalance calendar mismatch for {candidate_id}; "
                f"missing={missing_dates[:5]}, extra={extra_dates[:5]}"
            )
    for rebalance_date in sorted(reference_dates):
        sizes = {candidate_id: len(raw[candidate_id][rebalance_date]) for candidate_id in required_candidates}
        if any(size <= 0 for size in sizes.values()):
            raise ContractError(f"Empty asset set on {rebalance_date}")
        if len(set(sizes.values())) != 1:
            raise ContractError(
                f"All strategies must select the same asset count on {rebalance_date}; sizes={sizes}"
            )
    return {
        candidate_id: {
            rebalance_date: frozenset(asset_ids)
            for rebalance_date, asset_ids in sorted(dates.items())
        }
        for candidate_id, dates in sorted(raw.items())
    }


def signal_overlap_similarity(
    left: Mapping[str, frozenset[str]], right: Mapping[str, frozenset[str]]
) -> tuple[float, int]:
    left_dates = set(left)
    right_dates = set(right)
    if left_dates != right_dates or not left_dates:
        raise ContractError("Strategies must share the same non-empty development rebalance calendar")
    daily_similarities: list[float] = []
    for rebalance_date in sorted(left_dates):
        left_assets = left[rebalance_date]
        right_assets = right[rebalance_date]
        if len(left_assets) != len(right_assets) or not left_assets:
            raise ContractError(
                f"Strategies must select the same positive asset count on {rebalance_date}"
            )
        daily_similarities.append(len(left_assets & right_assets) / len(left_assets))
    return sum(daily_similarities) / len(daily_similarities), len(daily_similarities)


def run_decorrelation(
    members_by_n: Mapping[int, Sequence[Mapping[str, Any]]],
    signals_by_candidate: Mapping[str, Mapping[str, frozenset[str]]],
    direction: str,
    threshold: float,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    if direction not in {"max", "min"}:
        raise ContractError("direction must be 'max' or 'min'")
    if not 0 <= threshold <= 1:
        raise ContractError("similarity threshold must be between 0 and 1")
    similarities: list[dict[str, Any]] = []
    trace: list[dict[str, Any]] = []
    decorrelated_members: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []

    for n in sorted(members_by_n):
        records = [dict(record) for record in members_by_n[n]]
        if direction == "max":
            ordered = sorted(records, key=lambda row: (-row["metric_score"], row["candidate_id"]))
        else:
            ordered = sorted(records, key=lambda row: (row["metric_score"], row["candidate_id"]))

        similarity_map: dict[tuple[str, str], float] = {}
        for left_index, left in enumerate(ordered):
            for right in ordered[left_index + 1 :]:
                similarity, common_dates = signal_overlap_similarity(
                    signals_by_candidate[left["candidate_id"]],
                    signals_by_candidate[right["candidate_id"]],
                )
                pair = tuple(sorted((left["candidate_id"], right["candidate_id"])))
                similarity_map[pair] = similarity
                similarities.append(
                    {
                        "n": n,
                        "candidate_id_a": pair[0],
                        "candidate_id_b": pair[1],
                        "common_rebalance_dates": common_dates,
                        "similarity": format(similarity, ".17g"),
                    }
                )

        kept: list[dict[str, Any]] = []
        removed_slot_count = 0
        for order, candidate in enumerate(ordered, start=1):
            comparisons: list[tuple[float, str]] = []
            for retained in kept:
                pair = tuple(sorted((candidate["candidate_id"], retained["candidate_id"])))
                comparisons.append((similarity_map[pair], retained["candidate_id"]))
            max_similarity = max((value for value, _ in comparisons), default=None)
            blockers = sorted(
                candidate_id for value, candidate_id in comparisons if value > threshold
            )
            if blockers:
                decision = "removed"
                blocking_candidate_id = min(
                    blockers,
                    key=lambda candidate_id: (
                        -similarity_map[tuple(sorted((candidate["candidate_id"], candidate_id)))],
                        candidate_id,
                    ),
                )
                removed_slot_count += candidate["win_count"]
            else:
                decision = "kept"
                blocking_candidate_id = ""
                kept.append(candidate)
            trace.append(
                {
                    "n": n,
                    "order": order,
                    "candidate_id": candidate["candidate_id"],
                    "metric_score": format(candidate["metric_score"], ".17g"),
                    "win_count": candidate["win_count"],
                    "max_similarity_to_kept": (
                        "" if max_similarity is None else format(max_similarity, ".17g")
                    ),
                    "blocking_candidate_id": blocking_candidate_id,
                    "decision": decision,
                }
            )

        retained_slot_count = sum(record["win_count"] for record in kept)
        for record in kept:
            decorrelated_members.append(
                {
                    "n": n,
                    "candidate_id": record["candidate_id"],
                    "metric_score": format(record["metric_score"], ".17g"),
                    "win_count": record["win_count"],
                    "slot_weight": format(record["win_count"] / retained_slot_count, ".17g"),
                }
            )
        max_retained_similarity = 0.0
        for left_index, left in enumerate(kept):
            for right in kept[left_index + 1 :]:
                pair = tuple(sorted((left["candidate_id"], right["candidate_id"])))
                max_retained_similarity = max(max_retained_similarity, similarity_map[pair])
        summaries.append(
            {
                "n": n,
                "members_before": len(ordered),
                "members_after": len(kept),
                "members_removed": len(ordered) - len(kept),
                "slots_before": sum(record["win_count"] for record in ordered),
                "slots_after": retained_slot_count,
                "slots_removed": removed_slot_count,
                "max_retained_similarity": format(max_retained_similarity, ".17g"),
            }
        )
    return similarities, trace, decorrelated_members, summaries


def read_signals(
    path: Path, holdout_start: date, holdout_end: date
) -> tuple[dict[str, dict[str, list[str]]], int]:
    signals: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    seen: set[tuple[str, str, str]] = set()
    ignored_outside_holdout = 0
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        _required_columns(reader, {"rebalance_date", "candidate_id", "asset_id"}, path)
        for line_number, row in enumerate(reader, start=2):
            raw_date = (row.get("rebalance_date") or "").strip()
            signal_date = parse_iso_date(raw_date, f"{path}:{line_number} rebalance_date")
            if not holdout_start <= signal_date <= holdout_end:
                ignored_outside_holdout += 1
                continue
            candidate_id = (row.get("candidate_id") or "").strip()
            asset_id = (row.get("asset_id") or "").strip()
            if not candidate_id or not asset_id:
                raise ContractError(f"{path}:{line_number} has blank candidate_id or asset_id")
            key = (raw_date, candidate_id, asset_id)
            if key in seen:
                raise ContractError(
                    f"{path}:{line_number} duplicates rebalance_date + candidate_id + asset_id"
                )
            seen.add(key)
            signals[raw_date][candidate_id].append(asset_id)
    if not signals:
        raise ContractError(f"No signals within the holdout period in {path}")
    normalized = {
        rebalance_date: {
            candidate_id: sorted(asset_ids)
            for candidate_id, asset_ids in candidates.items()
        }
        for rebalance_date, candidates in signals.items()
    }
    return normalized, ignored_outside_holdout


def run_voting(
    members_by_n: Mapping[int, Mapping[str, int]],
    signals_by_date: Mapping[str, Mapping[str, Sequence[str]]],
    top_k_values: Sequence[int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    member_votes: list[dict[str, Any]] = []
    missing_signals: list[dict[str, Any]] = []
    asset_votes: list[dict[str, Any]] = []
    target_weights: list[dict[str, Any]] = []

    for n in sorted(members_by_n):
        members = members_by_n[n]
        for rebalance_date in sorted(signals_by_date):
            day_signals = signals_by_date[rebalance_date]
            for vote_mode in ("slot_weighted", "unique_equal"):
                votes_by_asset: Counter[str] = Counter()
                for candidate_id in sorted(members):
                    if candidate_id not in day_signals:
                        missing_signals.append(
                            {
                                "rebalance_date": rebalance_date,
                                "n": n,
                                "member_vote_mode": vote_mode,
                                "candidate_id": candidate_id,
                            }
                        )
                        continue
                    vote_weight = members[candidate_id] if vote_mode == "slot_weighted" else 1
                    asset_ids = day_signals[candidate_id]
                    if not asset_ids:
                        missing_signals.append(
                            {
                                "rebalance_date": rebalance_date,
                                "n": n,
                                "member_vote_mode": vote_mode,
                                "candidate_id": candidate_id,
                            }
                        )
                        continue
                    for asset_id in sorted(asset_ids):
                        votes_by_asset[asset_id] += vote_weight
                        member_votes.append(
                            {
                                "rebalance_date": rebalance_date,
                                "n": n,
                                "member_vote_mode": vote_mode,
                                "candidate_id": candidate_id,
                                "asset_id": asset_id,
                                "member_asset_count": len(asset_ids),
                                "vote_weight": vote_weight,
                            }
                        )
                ranked = sorted(votes_by_asset.items(), key=lambda item: (-item[1], item[0]))
                for rank, (asset_id, votes) in enumerate(ranked, start=1):
                    asset_votes.append(
                        {
                            "rebalance_date": rebalance_date,
                            "n": n,
                            "member_vote_mode": vote_mode,
                            "asset_id": asset_id,
                            "votes": votes,
                            "rank": rank,
                        }
                    )
                selections: list[tuple[str, int | None, list[tuple[str, int]]]] = [("all", None, ranked)]
                selections.extend(("top_k", k, ranked[:k]) for k in top_k_values)
                for selection_mode, top_k, selected in selections:
                    total_votes = sum(votes for _, votes in selected)
                    if total_votes <= 0:
                        continue
                    selection_label = "all" if top_k is None else f"top_{top_k}"
                    for asset_id, votes in selected:
                        target_weights.append(
                            {
                                "rebalance_date": rebalance_date,
                                "n": n,
                                "member_vote_mode": vote_mode,
                                "asset_selection_mode": selection_mode,
                                "top_k": "" if top_k is None else top_k,
                                "selection_label": selection_label,
                                "asset_id": asset_id,
                                "votes": votes,
                                "target_weight": format(votes / total_votes, ".17g"),
                            }
                        )
    return member_votes, missing_signals, asset_votes, target_weights


def verify_weight_sums(rows: Sequence[Mapping[str, Any]], tolerance: float = 1e-12) -> None:
    sums: dict[tuple[Any, ...], float] = defaultdict(float)
    for row in rows:
        key = (
            row["rebalance_date"],
            row["n"],
            row["member_vote_mode"],
            row["selection_label"],
        )
        sums[key] += float(row["target_weight"])
    bad = {key: value for key, value in sums.items() if not math.isclose(value, 1.0, abs_tol=tolerance)}
    if bad:
        key, value = next(iter(bad.items()))
        raise ContractError(f"Target weights do not sum to 1 for {key}: {value}")
