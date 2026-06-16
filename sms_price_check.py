"""Shared helpers for SMS price analytics and success-price persistence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable


def load_success_price_rows(path: str | Path) -> list[dict[str, Any]]:
    file_path = Path(path)
    if not file_path.exists():
        return []
    text = file_path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    data = json.loads(text)
    return list(data) if isinstance(data, list) else []


def save_success_price_rows(path: str | Path, rows: list[dict[str, Any]]) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _normalize_price(price) -> str:
    value = float(price)
    return f"{value:.6f}".rstrip("0").rstrip(".")


def record_success_price(
    rows: list[dict[str, Any]],
    sms_provider: str,
    country: str,
    success_price,
) -> list[dict[str, Any]]:
    normalized_price = _normalize_price(success_price)
    updated = [dict(row) for row in rows]
    for row in updated:
        if (
            str(row.get("sms_provider", "")) == str(sms_provider)
            and str(row.get("country", "")) == str(country)
            and _normalize_price(row.get("success_price", 0)) == normalized_price
        ):
            row["success_price"] = normalized_price
            row["success_count"] = int(row.get("success_count", 0)) + 1
            return updated
    updated.append(
        {
            "sms_provider": str(sms_provider),
            "country": str(country),
            "success_price": normalized_price,
            "success_count": 1,
        }
    )
    return updated


def _parse_max_price(max_price) -> float:
    try:
        return float(max_price)
    except (TypeError, ValueError):
        return 0.0


def build_recommendation(
    *,
    configured_countries: set[str] | None = None,
    country: str = "",
    lowest_price=None,
    second_price=None,
    lowest_count: int = 0,
    second_count: int = 0,
    history_success_count: int = 0,
    history_success_price=None,
    max_price="",
) -> dict[str, Any]:
    configured_countries = configured_countries or set()
    in_purchase_pool = str(country) in configured_countries
    price_limit = _parse_max_price(max_price)
    has_price = lowest_price is not None and float(lowest_price) > 0
    has_history = int(history_success_count or 0) > 0
    within_max_price = has_price and (price_limit <= 0 or float(lowest_price) <= price_limit)

    if in_purchase_pool:
        text = "已在池中"
    elif not has_price:
        text = "不推荐：无可用价格"
    elif not within_max_price:
        text = "不推荐：高于最高价格"
    elif has_history:
        text = f"推荐：低价 + 历史成功 {int(history_success_count)} 次"
    else:
        text = "可试用：低价但无历史成功"

    history_price = None
    if history_success_price not in (None, ""):
        history_price = float(history_success_price)

    score = 0.0
    if within_max_price:
        score += 100.0
    if has_history:
        score += min(int(history_success_count), 1000)
    if history_price is not None:
        score += max(0.0, 1.0 - history_price)
    if has_price:
        score += max(0.0, 1.0 - float(lowest_price))
    if in_purchase_pool:
        score += 10000.0

    return {
        "text": text,
        "score": round(score, 6),
        "in_purchase_pool": in_purchase_pool,
        "within_max_price": within_max_price,
        "history_success_count": int(history_success_count or 0),
        "history_success_price": _normalize_price(history_price) if history_price is not None else "",
        "lowest_price": _normalize_price(lowest_price) if has_price else "",
        "second_price": _normalize_price(second_price) if second_price not in (None, "") else "",
        "lowest_count": int(lowest_count or 0),
        "second_count": int(second_count or 0),
    }


def _history_by_provider_country(success_rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    history: dict[tuple[str, str], dict[str, Any]] = {}
    for row in success_rows:
        key = (str(row.get("sms_provider", "")), str(row.get("country", "")))
        count = int(row.get("success_count", 0) or 0)
        price = row.get("success_price", "")
        current = history.get(key)
        if current is None:
            history[key] = {"success_count": count, "success_price": price}
            continue
        current["success_count"] += count
        if current.get("success_price") in (None, ""):
            current["success_price"] = price
        elif price not in (None, "") and float(price) < float(current["success_price"]):
            current["success_price"] = price
    return history


def build_price_rows(
    price_catalog: Iterable[dict[str, Any]],
    configured_countries: Iterable[str],
    success_rows: list[dict[str, Any]],
    max_price,
) -> list[dict[str, Any]]:
    country_set = {str(country).strip() for country in configured_countries if str(country).strip()}
    history = _history_by_provider_country(success_rows)
    rows: list[dict[str, Any]] = []

    for item in price_catalog:
        sms_provider = str(item.get("sms_provider", ""))
        country = str(item.get("country", ""))
        key = (sms_provider, country)
        history_item = history.get(key, {})
        recommendation = build_recommendation(
            configured_countries=country_set,
            country=country,
            lowest_price=item.get("lowest_price"),
            second_price=item.get("second_price"),
            lowest_count=int(item.get("lowest_count", 0) or 0),
            second_count=int(item.get("second_count", 0) or 0),
            history_success_count=int(history_item.get("success_count", 0) or 0),
            history_success_price=history_item.get("success_price", ""),
            max_price=max_price,
        )
        rows.append(
            {
                "sms_provider": sms_provider,
                "country": country,
                "country_name": str(item.get("country_name", "")),
                "lowest_price": recommendation["lowest_price"],
                "second_price": recommendation["second_price"],
                "lowest_count": recommendation["lowest_count"],
                "second_count": recommendation["second_count"],
                "in_purchase_pool": recommendation["in_purchase_pool"],
                "recommendation": recommendation["text"],
                "recommendation_score": recommendation["score"],
                "history_success_count": recommendation["history_success_count"],
                "history_success_price": recommendation["history_success_price"],
            }
        )

    rows.sort(
        key=lambda row: (
            not row["in_purchase_pool"],
            -float(row["recommendation_score"]),
            float(row["lowest_price"] or 999999),
            row["sms_provider"],
            row["country"],
        )
    )
    return rows
