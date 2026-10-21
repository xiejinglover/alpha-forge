from __future__ import annotations

import csv
import hashlib
import json
import math
from pathlib import Path
from statistics import NormalDist
from typing import Any, Iterable

import numpy as np
import pandas as pd


class ContractError(ValueError):
    pass


STANDARD_PROFILE = {
    "fdr_alpha": 0.05,
    "confidence_level": 0.95,
    "factor_non_null_ratio": 0.80,
    "valid_decision_coverage": 0.70,
    "minimum_group_size": 10,
    "minimum_valid_decisions": 30,
    "positive_period_share": 0.60,
    "monotonicity_threshold": 0.60,
    "preferred_group_count": 5,
    "fallback_group_count": 3,
    "require_consistent_direction": True,
}


def read_json(path: str | Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ContractError(f"JSON root must be an object: {path}")
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def stable_id(prefix: str, value: Any, length: int = 16) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()[:length]
    return f"{prefix}_{digest}"


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def prepare_output_dir(path: str | Path) -> Path:
    target = Path(path)
    if target.exists() and any(target.iterdir()):
        raise ContractError(f"output directory must not exist or must be empty: {target}")
    target.mkdir(parents=True, exist_ok=True)
    return target


def require_columns(frame: pd.DataFrame, required: Iterable[str], name: str) -> None:
    missing = sorted(set(required) - set(frame.columns))
    if missing:
        raise ContractError(f"{name} missing columns: {', '.join(missing)}")


def parse_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    raise ContractError(f"{field} must be boolean, got {value!r}")


def finite_or_nan(series: pd.Series, name: str) -> pd.Series:
    result = pd.to_numeric(series, errors="coerce")
    bad = result.notna() & ~np.isfinite(result)
    if bad.any():
        raise ContractError(f"{name} contains infinity")
    return result


def parse_time(series: pd.Series, name: str) -> pd.Series:
    try:
        result = pd.to_datetime(series, utc=True, errors="raise")
    except Exception as exc:
        raise ContractError(f"invalid timestamp in {name}: {exc}") from exc
    return result


def load_inputs(
    observations_path: str | Path,
    factors_path: str | Path,
    metadata_path: str | Path,
    labels_path: str | Path,
    exposures_path: str | Path | None = None,
) -> dict[str, pd.DataFrame]:
    observations = pd.read_csv(observations_path)
    factors = pd.read_csv(factors_path)
    metadata = pd.read_csv(metadata_path)
    labels = pd.read_csv(labels_path)
    exposures = pd.read_csv(exposures_path) if exposures_path else pd.DataFrame()

    require_columns(
        observations,
        ["observation_id", "pool_id", "decision_time", "instrument_id", "label_id", "label_value", "label_exit_time"],
        "observations",
    )
    require_columns(factors, ["observation_id", "factor_id", "factor_value", "factor_known_at"], "factor_values")
    require_columns(metadata, ["factor_id", "factor_type"], "factor_metadata")
    require_columns(
        labels,
        [
            "label_id",
            "decision_schedule",
            "decision_interval_observations",
            "entry_rule",
            "exit_rule",
            "holding_horizon_observations",
            "execution_delay_observations",
            "cost_model",
            "label_is_net_of_costs",
            "overlapping_positions",
        ],
        "label_metadata",
    )
    if not exposures.empty:
        require_columns(
            exposures,
            ["observation_id", "exposure_id", "exposure_type", "exposure_value", "exposure_known_at"],
            "exposures",
        )

    for column in ["observation_id", "pool_id", "instrument_id", "label_id"]:
        observations[column] = observations[column].astype(str)
    observations["decision_time"] = parse_time(observations["decision_time"], "observations.decision_time")
    observations["label_exit_time"] = parse_time(observations["label_exit_time"], "observations.label_exit_time")
    observations["label_value"] = finite_or_nan(observations["label_value"], "observations.label_value")
    if observations.duplicated(["observation_id", "label_id"]).any():
        raise ContractError("observations contains duplicate observation_id × label_id")
    base_identity = observations.groupby("observation_id")[["pool_id", "decision_time", "instrument_id"]].nunique(dropna=False)
    if (base_identity > 1).any(axis=None):
        raise ContractError("one observation_id maps to multiple pool/time/instrument identities")
    if (observations["label_exit_time"] < observations["decision_time"]).any():
        raise ContractError("label_exit_time precedes decision_time")

    factors["observation_id"] = factors["observation_id"].astype(str)
    factors["factor_id"] = factors["factor_id"].astype(str)
    factors["factor_known_at"] = parse_time(factors["factor_known_at"], "factor_values.factor_known_at")
    if factors.duplicated(["observation_id", "factor_id"]).any():
        raise ContractError("factor_values contains duplicate observation_id × factor_id")
    unknown_observations = sorted(set(factors["observation_id"]) - set(observations["observation_id"]))
    if unknown_observations:
        raise ContractError(f"factor_values references unknown observations: {unknown_observations[:5]}")
    decision_lookup = observations.drop_duplicates("observation_id").set_index("observation_id")["decision_time"]
    factor_decisions = factors["observation_id"].map(decision_lookup)
    if (factors["factor_known_at"] > factor_decisions).any():
        raise ContractError("factor_known_at is later than decision_time")

    metadata["factor_id"] = metadata["factor_id"].astype(str)
    metadata["factor_type"] = metadata["factor_type"].astype(str).str.lower()
    if metadata["factor_id"].duplicated().any():
        raise ContractError("factor_metadata contains duplicate factor_id")
    allowed_factor_types = {"continuous", "ordinal", "binary", "nominal"}
    invalid_types = sorted(set(metadata["factor_type"]) - allowed_factor_types)
    if invalid_types:
        raise ContractError(f"unsupported factor types: {invalid_types}")
    unknown_factors = sorted(set(factors["factor_id"]) - set(metadata["factor_id"]))
    if unknown_factors:
        raise ContractError(f"factor_values references factors missing from metadata: {unknown_factors}")
    factor_type_map = metadata.set_index("factor_id")["factor_type"]
    numeric_mask = factors["factor_id"].map(factor_type_map).ne("nominal")
    numeric_values = pd.to_numeric(factors.loc[numeric_mask, "factor_value"], errors="coerce")
    invalid_numeric = factors.loc[numeric_mask, "factor_value"].notna() & numeric_values.isna()
    if invalid_numeric.any():
        bad = factors.loc[numeric_mask].loc[invalid_numeric, "factor_id"].astype(str).head().tolist()
        raise ContractError(f"numeric factor contains non-numeric values: {bad}")
    if np.isinf(numeric_values.dropna().to_numpy(dtype=float)).any():
        raise ContractError("factor_values.factor_value contains infinity")
    factors.loc[numeric_mask, "factor_value"] = numeric_values.astype(object)

    labels["label_id"] = labels["label_id"].astype(str)
    if labels["label_id"].duplicated().any():
        raise ContractError("label_metadata contains duplicate label_id")
    for column in ["decision_interval_observations", "holding_horizon_observations", "execution_delay_observations"]:
        labels[column] = pd.to_numeric(labels[column], errors="raise").astype(int)
    if (labels["decision_interval_observations"] <= 0).any() or (labels["holding_horizon_observations"] <= 0).any():
        raise ContractError("decision interval and holding horizon must be positive")
    if (labels["execution_delay_observations"] < 0).any():
        raise ContractError("execution delay must be non-negative")
    labels["label_is_net_of_costs"] = [parse_bool(v, "label_is_net_of_costs") for v in labels["label_is_net_of_costs"]]
    labels["overlapping_positions"] = [parse_bool(v, "overlapping_positions") for v in labels["overlapping_positions"]]
    implied_overlap = labels["holding_horizon_observations"] > labels["decision_interval_observations"]
    if (labels["overlapping_positions"] != implied_overlap).any():
        raise ContractError("overlapping_positions conflicts with holding horizon and decision interval")
    unknown_labels = sorted(set(observations["label_id"]) - set(labels["label_id"]))
    if unknown_labels:
        raise ContractError(f"observations references labels missing from metadata: {unknown_labels}")

    if not exposures.empty:
        exposures["observation_id"] = exposures["observation_id"].astype(str)
        exposures["exposure_id"] = exposures["exposure_id"].astype(str)
        exposures["exposure_type"] = exposures["exposure_type"].astype(str).str.lower()
        exposures["exposure_known_at"] = parse_time(exposures["exposure_known_at"], "exposures.exposure_known_at")
        if exposures.duplicated(["observation_id", "exposure_id"]).any():
            raise ContractError("exposures contains duplicate observation_id × exposure_id")
        if not set(exposures["exposure_type"]).issubset({"continuous", "categorical"}):
            raise ContractError("exposure_type must be continuous or categorical")
        unknown = sorted(set(exposures["observation_id"]) - set(observations["observation_id"]))
        if unknown:
            raise ContractError(f"exposures references unknown observations: {unknown[:5]}")
        exposure_decisions = exposures["observation_id"].map(decision_lookup)
        if (exposures["exposure_known_at"] > exposure_decisions).any():
            raise ContractError("exposure_known_at is later than decision_time")
        continuous = exposures["exposure_type"].eq("continuous")
        exposures.loc[continuous, "exposure_value"] = finite_or_nan(
            exposures.loc[continuous, "exposure_value"], "exposures.exposure_value"
        )

    return {
        "observations": observations,
        "factors": factors,
        "metadata": metadata,
        "labels": labels,
        "exposures": exposures,
    }


def validate_config(config: dict[str, Any], labels: pd.DataFrame) -> dict[str, Any]:
    required = {"study_id", "timezone", "thresholds_confirmed", "deployments", "recipes"}
    missing = sorted(required - set(config))
    if missing:
        raise ContractError(f"config missing fields: {missing}")
    if not parse_bool(config["thresholds_confirmed"], "thresholds_confirmed"):
        raise ContractError("thresholds_confirmed must be true before a frozen plan can be built")
    if not config["deployments"] or not config["recipes"]:
        raise ContractError("deployments and recipes must be non-empty")
    deployment_ids: set[str] = set()
    for deployment in config["deployments"]:
        for key in ["id", "information_cutoff", "windows"]:
            if key not in deployment:
                raise ContractError(f"deployment missing {key}")
        if deployment["id"] in deployment_ids:
            raise ContractError(f"duplicate deployment id: {deployment['id']}")
        deployment_ids.add(deployment["id"])
        cutoff = pd.to_datetime(deployment["information_cutoff"], utc=True, errors="raise")
        if not isinstance(deployment["windows"], dict) or not deployment["windows"]:
            raise ContractError("deployment windows must be a non-empty object")
        for window_id, start in deployment["windows"].items():
            start_time = pd.to_datetime(start, utc=True, errors="raise")
            if start_time > cutoff:
                raise ContractError(f"window {window_id} starts after cutoff in {deployment['id']}")

    label_map = labels.set_index("label_id").to_dict("index")
    recipe_ids: set[str] = set()
    normalized = json.loads(json.dumps(config))
    for recipe in normalized["recipes"]:
        required_recipe = {
            "id", "consensus_id", "label_id", "window_id", "primary_metric",
            "strategy_contract",
            "preferred_group_count", "fallback_group_count", "minimum_pool_size",
            "minimum_group_size", "factor_non_null_ratio", "valid_decision_coverage",
            "require_all_groups",
            "minimum_valid_decisions", "valid_decision_cap", "fdr_alpha", "confidence_level",
            "hac_primary_lag", "hac_sensitivity_lags", "minimum_effect", "positive_period_share",
            "monotonicity_threshold", "minimum_consensus_windows", "require_consistent_direction",
        }
        missing_recipe = sorted(required_recipe - set(recipe))
        if missing_recipe:
            raise ContractError(f"recipe {recipe.get('id', '<unknown>')} missing fields: {missing_recipe}")
        if recipe["id"] in recipe_ids:
            raise ContractError(f"duplicate recipe id: {recipe['id']}")
        recipe_ids.add(recipe["id"])
        if recipe["label_id"] not in label_map:
            raise ContractError(f"unknown label_id in recipe {recipe['id']}: {recipe['label_id']}")
        if recipe["primary_metric"] not in {"rank_ic", "spread"}:
            raise ContractError("primary_metric must be rank_ic or spread")
        for key in ["factor_non_null_ratio", "valid_decision_coverage", "fdr_alpha", "confidence_level", "positive_period_share"]:
            value = float(recipe[key])
            if not 0 < value < 1:
                raise ContractError(f"{key} must be between 0 and 1")
        for key in ["preferred_group_count", "fallback_group_count", "minimum_pool_size", "minimum_group_size", "minimum_valid_decisions", "valid_decision_cap", "minimum_consensus_windows"]:
            if int(recipe[key]) <= 0:
                raise ContractError(f"{key} must be positive")
        if int(recipe["valid_decision_cap"]) < int(recipe["minimum_valid_decisions"]):
            raise ContractError("valid_decision_cap must be >= minimum_valid_decisions")
        if float(recipe["minimum_effect"]) < 0:
            raise ContractError("minimum_effect must be non-negative")
        label = label_map[recipe["label_id"]]
        contract = recipe["strategy_contract"]
        contract_fields = {
            "decision_schedule", "decision_interval_observations", "entry_rule", "exit_rule",
            "holding_horizon_observations", "execution_delay_observations", "cost_model",
            "require_net_of_costs", "overlapping_positions",
        }
        contract_missing = sorted(contract_fields - set(contract)) if isinstance(contract, dict) else sorted(contract_fields)
        if contract_missing:
            raise ContractError(f"recipe {recipe['id']} strategy_contract missing fields: {contract_missing}")
        comparisons = {
            "decision_schedule": str(contract["decision_schedule"]),
            "decision_interval_observations": int(contract["decision_interval_observations"]),
            "entry_rule": str(contract["entry_rule"]),
            "exit_rule": str(contract["exit_rule"]),
            "holding_horizon_observations": int(contract["holding_horizon_observations"]),
            "execution_delay_observations": int(contract["execution_delay_observations"]),
            "cost_model": str(contract["cost_model"]),
            "overlapping_positions": parse_bool(contract["overlapping_positions"], "strategy_contract.overlapping_positions"),
        }
        for field, expected in comparisons.items():
            actual = label[field]
            if isinstance(expected, (int, bool)):
                actual = int(actual) if isinstance(expected, int) and not isinstance(expected, bool) else bool(actual)
            else:
                actual = str(actual)
            if actual != expected:
                raise ContractError(
                    f"recipe {recipe['id']} label {recipe['label_id']} does not match strategy {field}: {actual!r} != {expected!r}"
                )
        if parse_bool(contract["require_net_of_costs"], "strategy_contract.require_net_of_costs") and not bool(label["label_is_net_of_costs"]):
            raise ContractError(f"recipe {recipe['id']} requires a net-of-costs label")
        overlap_floor = math.ceil(
            int(label["holding_horizon_observations"]) / int(label["decision_interval_observations"])
        ) - 1
        lag = recipe["hac_primary_lag"]
        if lag == "auto":
            resolved_lag = overlap_floor
        else:
            resolved_lag = int(lag)
            if resolved_lag < overlap_floor:
                raise ContractError(
                    f"recipe {recipe['id']} HAC lag {resolved_lag} is below overlap floor {overlap_floor}"
                )
        sensitivities = sorted({int(x) for x in recipe["hac_sensitivity_lags"]})
        if not sensitivities or max(sensitivities) <= resolved_lag:
            raise ContractError(f"recipe {recipe['id']} needs a sensitivity lag longer than the primary lag")
        recipe["resolved_hac_primary_lag"] = resolved_lag
        recipe["overlap_lag_floor"] = overlap_floor
        recipe["hac_sensitivity_lags"] = sensitivities
        recipe["require_consistent_direction"] = parse_bool(
            recipe["require_consistent_direction"], "require_consistent_direction"
        )
        recipe["require_all_groups"] = parse_bool(recipe["require_all_groups"], "require_all_groups")
        for deployment in normalized["deployments"]:
            if recipe["window_id"] not in deployment["windows"]:
                raise ContractError(
                    f"recipe {recipe['id']} window {recipe['window_id']} missing in deployment {deployment['id']}"
                )
    return normalized


def normal_p_value(t_value: float) -> float:
    return math.erfc(abs(t_value) / math.sqrt(2.0))


def hac_mean(values: Iterable[float], lag: int, confidence: float) -> dict[str, float]:
    x = np.asarray([float(v) for v in values if pd.notna(v) and np.isfinite(v)], dtype=float)
    n = len(x)
    if n < 2:
        raise ContractError("HAC requires at least two observations")
    if lag < 0 or lag >= n:
        raise ContractError(f"HAC lag {lag} must be in [0, {n - 1}]")
    mean = float(np.mean(x))
    u = x - mean
    gamma0 = float(np.dot(u, u) / n)
    lrv = gamma0
    for ell in range(1, lag + 1):
        gamma = float(np.dot(u[ell:], u[:-ell]) / n)
        weight = 1.0 - ell / (lag + 1.0)
        lrv += 2.0 * weight * gamma
    if lrv < -1e-12:
        raise ContractError(f"negative HAC long-run variance: {lrv}")
    lrv = max(lrv, 0.0)
    se = math.sqrt(lrv / n)
    if se <= 0:
        raise ContractError("degenerate HAC standard error")
    t_value = mean / se
    z = NormalDist().inv_cdf((1.0 + confidence) / 2.0)
    return {
        "n": n,
        "mean": mean,
        "se": se,
        "t": t_value,
        "p": normal_p_value(t_value),
        "ci_lower": mean - z * se,
        "ci_upper": mean + z * se,
        "lag": lag,
    }


def bh_qvalues(p_values: list[float | None]) -> list[float | None]:
    valid = [(index, float(value)) for index, value in enumerate(p_values) if value is not None and np.isfinite(value)]
    if not valid:
        return [None] * len(p_values)
    valid.sort(key=lambda item: (item[1], item[0]))
    m = len(valid)
    adjusted: dict[int, float] = {}
    running = 1.0
    for rank_from_end in range(m, 0, -1):
        index, p_value = valid[rank_from_end - 1]
        running = min(running, m * p_value / rank_from_end)
        adjusted[index] = min(1.0, running)
    return [adjusted.get(index) for index in range(len(p_values))]


def spearman(x: pd.Series, y: pd.Series) -> float:
    valid = pd.DataFrame({"x": x, "y": y}).dropna()
    if len(valid) < 3 or valid["x"].nunique() < 2 or valid["y"].nunique() < 2:
        return math.nan
    return float(valid["x"].rank(method="average").corr(valid["y"].rank(method="average")))


def assign_groups(values: pd.Series, factor_type: str, groups: int) -> pd.Series:
    if factor_type == "binary":
        unique = set(values.dropna().astype(float).unique())
        if not unique.issubset({0.0, 1.0}) or unique != {0.0, 1.0}:
            return pd.Series(np.nan, index=values.index)
        return values.astype(float) + 1.0
    if factor_type == "ordinal" and values.nunique(dropna=True) <= groups:
        ordered = sorted(values.dropna().unique())
        mapping = {value: index + 1 for index, value in enumerate(ordered)}
        return values.map(mapping).astype(float)
    ranks = values.rank(method="average", pct=True)
    return np.ceil(ranks * groups).clip(1, groups)


def adjusted_r2(y: np.ndarray, categories: pd.Series) -> float:
    valid = pd.DataFrame({"y": y, "category": categories.fillna("UNKNOWN").astype(str)}).dropna()
    if len(valid) < 3 or valid["y"].nunique() < 2:
        return math.nan
    dummies = pd.get_dummies(valid["category"], drop_first=True, dtype=float)
    x = np.column_stack([np.ones(len(valid)), dummies.to_numpy(dtype=float)])
    if len(valid) <= x.shape[1]:
        return math.nan
    coefficients, *_ = np.linalg.lstsq(x, valid["y"].to_numpy(dtype=float), rcond=None)
    fitted = x @ coefficients
    residual = valid["y"].to_numpy(dtype=float) - fitted
    total = valid["y"].to_numpy(dtype=float) - valid["y"].mean()
    tss = float(np.dot(total, total))
    if tss <= 0:
        return math.nan
    r2 = 1.0 - float(np.dot(residual, residual)) / tss
    n, p = len(valid), x.shape[1] - 1
    return 1.0 - (1.0 - r2) * (n - 1) / (n - p - 1)


def write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            clean = {}
            for column in columns:
                value = row.get(column, "")
                if value is None or (isinstance(value, float) and not np.isfinite(value)):
                    value = ""
                elif isinstance(value, (bool, np.bool_)):
                    value = str(bool(value)).lower()
                clean[column] = value
            writer.writerow(clean)


def hash_outputs(directory: Path, names: Iterable[str]) -> dict[str, str]:
    return {name: file_sha256(directory / name) for name in names}
