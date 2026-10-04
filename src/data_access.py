from __future__ import annotations

import csv
import json

import pandas as pd

from src.config import data_dir

# Columns parsed as numbers. Everything else stays a string; blank cells become None.
NUMERIC_COLUMNS = {
    "annual_software_budget_usd",
    "committed_usd",
    "available_usd",
    "annual_cost_usd",
    "licensed_seats",
    "annual_amount_usd",
}


def _coerce(column: str, value: str | None) -> object:
    if value is None:
        return None
    value = value.strip()
    if value == "":
        return None
    if column in NUMERIC_COLUMNS:
        number = float(value)
        return int(number) if number.is_integer() else number
    return value


def read_table(filename: str) -> list[dict]:
    """Read a CSV as plain Python records (no NaN, no numpy scalars; JSON-safe)."""
    with (data_dir() / filename).open(encoding="utf-8", newline="") as f:
        return [{k: _coerce(k, v) for k, v in row.items()} for row in csv.DictReader(f)]


def employees() -> list[dict]:
    return read_table("employees.csv")


def budgets() -> list[dict]:
    return read_table("department_budgets.csv")


def software_catalog() -> list[dict]:
    return read_table("software_catalog.csv")


def vendors() -> list[dict]:
    return read_table("vendors.csv")


def purchase_history() -> list[dict]:
    return read_table("purchase_history.csv")


# --- Starter-pack DataFrame helpers, kept for compatibility. Prefer the record readers above.

def load_employees() -> pd.DataFrame:
    return pd.read_csv(data_dir() / "employees.csv")


def load_budgets() -> pd.DataFrame:
    return pd.read_csv(data_dir() / "department_budgets.csv")


def load_software_catalog() -> pd.DataFrame:
    return pd.read_csv(data_dir() / "software_catalog.csv")


def load_vendors() -> pd.DataFrame:
    return pd.read_csv(data_dir() / "vendors.csv")


def load_purchase_history() -> pd.DataFrame:
    return pd.read_csv(data_dir() / "purchase_history.csv")


def load_requests() -> list[dict]:
    return json.loads((data_dir() / "requests.json").read_text(encoding="utf-8"))


def get_request(request_id: str) -> dict:
    for request in load_requests():
        if request["request_id"] == request_id:
            return request
    raise KeyError(f"Unknown request_id: {request_id}")


def load_policy_text() -> str:
    return (data_dir() / "procurement_policy.md").read_text(encoding="utf-8")
