"""The `ref` vocabularies, read from the database and handed to the generators as plain data.

Spec 003 requires entity generators to have no database access. Reading the reference data here
and passing it in keeps that true while making the generator use the codes the database
actually holds, rather than a second copy in `profiles.yml` that can drift from the seed.

Every collection is ordered by code. Nothing iterates a mapping for a random choice without
sorting first, because the determinism contract forbids depending on dictionary order — and
while CPython's insertion order is stable, the order rows arrive in from a query is not
something this code should rely on.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))


def _to_bool(value: str) -> bool:
    return value.strip() == "t"


def _to_int(value: str) -> int:
    return int(value.strip())


def _to_float(value: str) -> float:
    return float(value.strip())


def _to_str(value: str) -> str:
    return value.strip()


# table -> (columns, converters). The first column is always the code the rest is keyed on.
TABLES: dict[str, tuple[tuple[str, ...], tuple[Any, ...]]] = {
    "currencies": (("code", "minor_unit"), (_to_str, _to_int)),
    "countries": (
        ("code", "region_code", "is_eea", "is_sepa"),
        (_to_str, _to_str, _to_bool, _to_bool),
    ),
    "regions": (("code",), (_to_str,)),
    "mcc_codes": (("code", "band_code", "category"), (_to_str, _to_str, _to_str)),
    "mcc_bands": (("code",), (_to_str,)),
    "account_types": (
        ("code", "product_class_code", "is_deposit_taking"),
        (_to_str, _to_str, _to_bool),
    ),
    "account_statuses": (("code", "is_open"), (_to_str, _to_bool)),
    "card_products": (
        ("code", "product_class_code", "is_commercial"),
        (_to_str, _to_str, _to_bool),
    ),
    "channels": (("code", "is_digital"), (_to_str, _to_bool)),
    "transaction_types": (
        ("code", "is_customer_initiated", "direction"),
        (_to_str, _to_bool, _to_str),
    ),
    "transaction_statuses": (("code", "is_posted"), (_to_str, _to_bool)),
    "payment_types": (
        ("code", "is_customer_initiated", "direction"),
        (_to_str, _to_bool, _to_str),
    ),
    "payment_statuses": (
        ("code", "is_posted", "is_declined", "is_final"),
        (_to_str, _to_bool, _to_bool, _to_bool),
    ),
    "payment_schemes": (("code", "is_sepa", "settlement_days"), (_to_str, _to_bool, _to_int)),
    "loan_products": (
        ("code", "nominal_annual_rate", "term_months", "is_secured"),
        (_to_str, _to_float, _to_int, _to_bool),
    ),
    "loan_statuses": (("code", "is_open", "implies_default"), (_to_str, _to_bool, _to_bool)),
    "loan_application_statuses": (
        ("code", "is_decided", "is_approved"),
        (_to_str, _to_bool, _to_bool),
    ),
    "login_outcomes": (("code", "is_successful"), (_to_str, _to_bool)),
    "fraud_dispositions": (
        ("code", "is_final", "is_confirmed_fraud"),
        (_to_str, _to_bool, _to_bool),
    ),
    "fraud_rules": (("code",), (_to_str,)),
    "holder_roles": (("code", "is_primary", "carries_ownership"), (_to_str, _to_bool, _to_bool)),
    "risk_bands": (
        ("code", "band_ordinal", "pd_lower_bound", "pd_upper_bound"),
        (_to_str, _to_int, _to_float, _to_float),
    ),
    "decision_reasons": (("code",), (_to_str,)),
    "gl_accounts": (("code", "gl_account_type_code"), (_to_str, _to_str)),
    "gl_account_types": (("code", "normal_side_code"), (_to_str, _to_str)),
    "gl_source_entities": (("code",), (_to_str,)),
    "entry_sides": (("code", "sign_multiplier"), (_to_str, _to_int)),
}


class RefDataError(Exception):
    """Raised when the reference layer is not what the generator was written against."""


@dataclass(frozen=True)
class RefData:
    """Every `ref` vocabulary the generator reads, keyed by code and ordered by it."""

    tables: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)

    def rows(self, table: str) -> list[dict[str, Any]]:
        try:
            by_code = self.tables[table]
        except KeyError as error:
            raise RefDataError(f"reference table {table!r} was not loaded") from error
        return [by_code[code] for code in sorted(by_code)]

    def codes(self, table: str) -> list[str]:
        return sorted(self.tables.get(table, {}))

    def row(self, table: str, code: str) -> dict[str, Any]:
        try:
            return self.tables[table][code]
        except KeyError as error:
            raise RefDataError(
                f"{table}.{code} is not seeded; generator/profiles.yml names a code the "
                f"reference layer does not have"
            ) from error

    def require(self, table: str, codes: list[str] | tuple[str, ...]) -> None:
        """Fail early and by name when a profile references a code that is not seeded."""
        missing = [code for code in codes if code not in self.tables.get(table, {})]
        if missing:
            raise RefDataError(
                f"ref.{table} is missing code(s) named in generator/profiles.yml: "
                f"{', '.join(sorted(missing))}"
            )

    def codes_where(self, table: str, column: str, value: Any) -> list[str]:
        return sorted(code for code, row in self.tables[table].items() if row[column] == value)


# Separates one table's rows from the next in the single-session read below. A marker rather
# than a row count, so a table that returns nothing is still distinguishable from one that was
# not read at all.
TABLE_MARKER = "@@ref@@"


def _read_all_through_psql() -> dict[str, list[tuple[str, ...]]]:
    """Read all twenty-seven vocabularies in one psql session.

    One query per table costs one process launch inside a container each, measured at 0.589
    seconds: twenty-seven of them spent 16.5 seconds before the generator had produced a row,
    against a twenty second budget for the whole ci profile.
    """
    import source_db_exec as db

    script_parts = []
    for table, (columns, _) in TABLES.items():
        script_parts.append(f"\\echo {TABLE_MARKER}{table}")
        script_parts.append(
            f"select {', '.join(columns)} from ref.{table} order by {columns[0]};"  # noqa: S608
        )
    command = db._psql_command(False, ["-A", "-t", "-F", db.FIELD_SEPARATOR])
    completed = db._run(command, stdin="\n".join(script_parts) + "\n")
    if completed.returncode != 0:
        raise RefDataError(
            "reading ref failed; is the stack up and has `make schema-apply` run?\n"
            + (completed.stdout + completed.stderr).strip()[:1000]
        )

    out: dict[str, list[tuple[str, ...]]] = {}
    current: str | None = None
    for line in completed.stdout.splitlines():
        if line.startswith(TABLE_MARKER):
            current = line[len(TABLE_MARKER):].strip()
            out[current] = []
        elif current is not None and line.strip():
            out[current].append(tuple(line.split(db.FIELD_SEPARATOR)))
    return out


def load(executor: Any = None) -> RefData:
    """Read every reference vocabulary, through `executor` if given or psql if not."""
    if executor is None:
        raw_rows = _read_all_through_psql()
    else:
        raw_rows = {
            table: executor(
                f"select {', '.join(columns)} from ref.{table} order by {columns[0]}"  # noqa: S608
            )
            for table, (columns, _) in TABLES.items()
        }

    tables: dict[str, dict[str, dict[str, Any]]] = {}
    for table, (columns, converters) in TABLES.items():
        by_code: dict[str, dict[str, Any]] = {}
        for raw in raw_rows.get(table, []):
            if len(raw) != len(columns):
                raise RefDataError(
                    f"ref.{table}: expected {len(columns)} columns, got {len(raw)}"
                )
            row = {
                column: convert(value)
                for column, convert, value in zip(columns, converters, raw, strict=True)
            }
            by_code[row["code"]] = row
        if not by_code:
            raise RefDataError(f"ref.{table} is empty; run `make schema-apply` first")
        tables[table] = by_code
    return RefData(tables=tables)


def validate_against(ref: RefData, params: dict[str, Any]) -> None:
    """Fail before generating anything if a profile names a code the reference layer lacks.

    A missing code would otherwise surface as a foreign key violation partway through a load,
    after minutes of generation, naming a constraint rather than the parameter at fault.
    """
    ref.require("account_types", list(params["accounts"]["type_mix"]))
    ref.require("currencies", ["EUR", *params["accounts"]["non_eur_mix"]])
    ref.require("countries", list(params["customers"]["country_mix"]))
    ref.require("countries", list(params["merchants"]["non_eea_mix"]))
    ref.require("countries", list(params["payments"]["corridor_mix"]))
    ref.require("risk_bands", list(params["customers"]["risk_band_mix"]))
    ref.require("risk_bands", list(params["lending"]["approval_rate_by_band"]))
    ref.require("risk_bands", list(params["lending"]["default_rate_by_band"]))
    ref.require("card_products", list(params["cards"]["product_mix"]))
    ref.require("mcc_codes", list(params["merchants"]["mcc_mix"]))
    ref.require("mcc_bands", list(params["transactions"]["weekend_band_factors"]))
    ref.require("mcc_bands", list(params["amounts"]["by_band"]))
    ref.require("loan_products", list(params["lending"]["product_mix"]))
    ref.require("transaction_types", list(params["transactions"]["type_mix"]))
    ref.require(
        "transaction_types", list(params["transactions"]["bank_initiated_per_account_month"])
    )
    ref.require("transaction_statuses", list(params["transactions"]["status_mix"]))
    ref.require("channels", list(params["transactions"]["card_present_share_by_channel"]))
    ref.require("channels", [params["transactions"]["bank_initiated_channel"]])
    for type_code, mix in params["transactions"]["channel_mix_by_type"].items():
        ref.require("transaction_types", [type_code])
        ref.require("channels", list(mix))
    ref.require("payment_types", list(params["payments"]["type_mix"]))
    ref.require("payment_statuses", list(params["payments"]["status_mix"]))
    ref.require("payment_schemes", sorted(set(params["payments"]["scheme_by_type"].values())))
    ref.require("fraud_rules", list(params["fraud"]["rule_mix"]))
    ref.require("channels", list(params["digital"]["channel_mix"]))
    ref.require("login_outcomes", list(params["digital"]["outcome_mix"]))
    ref.require("gl_accounts", sorted(set(params["ledger"]["accounts"].values())))
    ref.require("gl_source_entities", ["transaction", "payment"])
    ref.require("entry_sides", ["D", "C"])

    missing_schemes = sorted(
        set(params["payments"]["type_mix"]) - set(params["payments"]["scheme_by_type"])
    )
    if missing_schemes:
        raise RefDataError(
            "generator/profiles.yml: payments.scheme_by_type has no scheme for "
            f"{', '.join(missing_schemes)}"
        )
