"""The sixteen `core` tables, their columns, and the order they load in.

One definition, read by the spool that writes the rows and by the loader that copies them, so a
column added to one cannot go missing from the other. The order is a topological sort of the
foreign keys: parents before children. The loader drops foreign keys for the duration of the
load (ADR 0011), so the order is not what makes the load succeed — but it is what makes a
failure land on the table that caused it rather than on the first child of a missing parent.

`created_at` and `updated_at` appear on every table and are written by the loader, not by the
database. The `set_updated_at` trigger fires `before update` only, so an insert never touches
it and a row left to the column default would carry the load timestamp. Spec 003's determinism
contract and acceptance criterion 4 both turn on that.
"""

from __future__ import annotations

AUDIT = ("created_at", "updated_at", "is_deleted")

TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "customers": (
        "customer_id",
        "customer_reference",
        "full_name",
        "email",
        "phone",
        "national_identifier",
        "date_of_birth",
        "kyc_status_code",
        "risk_band_code",
        "signup_date",
        "residence_country_code",
        *AUDIT,
    ),
    "customer_addresses": (
        "customer_address_id",
        "customer_id",
        "address_type_code",
        "address_line_1",
        "address_line_2",
        "city",
        "postal_code",
        "country_code",
        "valid_from_date",
        "valid_to_date",
        *AUDIT,
    ),
    "merchants": (
        "merchant_id",
        "merchant_reference",
        "merchant_name",
        "mcc_code",
        "country_code",
        *AUDIT,
    ),
    "agent_locations": (
        "agent_location_id",
        "agent_location_reference",
        "partner_name",
        "address_line",
        "city",
        "postal_code",
        "country_code",
        "active_from_date",
        "active_to_date",
        *AUDIT,
    ),
    "accounts": (
        "account_id",
        "account_number",
        "iban",
        "account_type_code",
        "currency_code",
        "country_code",
        "account_status_code",
        "opened_date",
        "closed_date",
        "overdraft_limit_amount",
        "current_balance_amount",
        *AUDIT,
    ),
    "account_holders": (
        "account_holder_id",
        "account_id",
        "customer_id",
        "holder_role_code",
        "ownership_weight",
        *AUDIT,
    ),
    "cards": (
        "card_id",
        "card_reference",
        "account_id",
        "card_product_code",
        "card_bin",
        "card_last_four",
        "card_status_code",
        "issued_date",
        "expiry_date",
        *AUDIT,
    ),
    "loan_applications": (
        "loan_application_id",
        "application_reference",
        "customer_id",
        "loan_product_code",
        "loan_application_status_code",
        "risk_band_code",
        "decision_reason_code",
        "applied_at",
        "decided_at",
        "requested_amount",
        "approved_amount",
        "application_currency_code",
        *AUDIT,
    ),
    "loans": (
        "loan_id",
        "loan_reference",
        "loan_application_id",
        "customer_id",
        "loan_product_code",
        "loan_status_code",
        "disbursed_date",
        "maturity_date",
        "written_off_date",
        "principal_amount",
        "loan_currency_code",
        "nominal_annual_rate",
        "term_months",
        *AUDIT,
    ),
    "loan_installments": (
        "loan_installment_id",
        "loan_id",
        "installment_number",
        "due_date",
        "due_amount",
        "paid_amount",
        "paid_at",
        "installment_currency_code",
        *AUDIT,
    ),
    "transactions": (
        "transaction_id",
        "transaction_reference",
        "account_id",
        "card_id",
        "merchant_id",
        "agent_location_id",
        "transaction_type_code",
        "channel_code",
        "transaction_status_code",
        "authorisation_outcome_code",
        "booked_at",
        "value_date",
        "transaction_amount",
        "transaction_currency_code",
        "is_card_present",
        "reversal_of_transaction_id",
        "counterparty_reference",
        *AUDIT,
    ),
    "payments": (
        "payment_id",
        "payment_reference",
        "account_id",
        "payment_type_code",
        "payment_scheme_code",
        "payment_status_code",
        "initiated_at",
        "booked_at",
        "settled_at",
        "payment_amount",
        "payment_currency_code",
        "counterparty_iban",
        "counterparty_name",
        "counterparty_country_code",
        "remittance_reference",
        *AUDIT,
    ),
    "gl_transactions": (
        "gl_transaction_id",
        "gl_transaction_reference",
        "posting_date",
        "description",
        "source_entity_code",
        "source_entity_id",
        *AUDIT,
    ),
    "gl_entries": (
        "gl_entry_id",
        "gl_transaction_id",
        "posting_date",
        "gl_account_code",
        "entry_side_code",
        "amount",
        "entry_currency_code",
        "account_id",
        *AUDIT,
    ),
    "fraud_alerts": (
        "fraud_alert_id",
        "alert_reference",
        "transaction_id",
        "customer_id",
        "fraud_rule_code",
        "fraud_disposition_code",
        "alerted_at",
        "dispositioned_at",
        "alert_score",
        "analyst_reference",
        *AUDIT,
    ),
    "login_sessions": (
        "login_session_id",
        "session_reference",
        "customer_id",
        "channel_code",
        "login_outcome_code",
        "started_at",
        "ended_at",
        "device_fingerprint",
        "ip_address",
        "ip_country_code",
        *AUDIT,
    ),
}

# Parents before children.
LOAD_ORDER: tuple[str, ...] = (
    "customers",
    "customer_addresses",
    "merchants",
    "agent_locations",
    "accounts",
    "account_holders",
    "cards",
    "loan_applications",
    "loans",
    "loan_installments",
    "transactions",
    "payments",
    "gl_transactions",
    "gl_entries",
    "fraud_alerts",
    "login_sessions",
)

# The identity column of each table, which the loader resynchronises with setval after the
# load. COPY does not advance the sequence, and a missed setval is invisible until the M3
# mutation engine's first insert collides (ADR 0011).
IDENTITY_COLUMN: dict[str, str] = {table: columns[0] for table, columns in TABLE_COLUMNS.items()}

# gl_entries commits in its own chunked transactions because of the deferred balance trigger,
# so the loader treats it differently from every other table.
LEDGER_ENTRY_TABLE = "gl_entries"
LEDGER_BATCH_TABLE = "gl_transactions"

assert set(LOAD_ORDER) == set(TABLE_COLUMNS), (
    "LOAD_ORDER and TABLE_COLUMNS must cover the same tables"
)
assert len(LOAD_ORDER) == 16, "spec 003 loads sixteen core tables"
