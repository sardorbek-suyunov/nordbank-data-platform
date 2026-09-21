"""Unit tests for identifier tokenisation.

Criterion 9 asks that the same raw value yield the same token across entities, columns and
batches. That property has no natural enforcement — a tokeniser that mixed the column name into
the key would look correct in every single-entity test and would silently break every
cross-entity join — so it is asserted directly here and again against real data in the
integration suite.

No database and no Airflow: these run in `make test`.
"""

from __future__ import annotations

import pytest
from nordbank_ops.tokenise import TOKEN_WIDTH, Tokeniser, TokeniserError

SALT = "a-test-salt-that-is-not-the-real-one"


def tokeniser() -> Tokeniser:
    return Tokeniser.from_environment({"PII_TOKEN_SALT": SALT})


def test_the_salt_has_no_default():
    with pytest.raises(TokeniserError, match="PII_TOKEN_SALT"):
        Tokeniser.from_environment({})


def test_a_blank_salt_is_refused():
    with pytest.raises(TokeniserError):
        Tokeniser.from_environment({"PII_TOKEN_SALT": "   "})


def test_a_token_is_fixed_width_hexadecimal():
    token = tokeniser().token("DE49500105175407324931")
    assert len(token) == TOKEN_WIDTH
    assert set(token) <= set("0123456789abcdef")


def test_a_null_produces_no_token():
    assert tokeniser().token(None) is None


def test_the_same_value_yields_the_same_token_across_entities_and_columns():
    subject = tokeniser()
    name = "Jan Kowalski"
    from_customers = subject.tokenise_row({"full_name": name}, ("full_name",))[0]["full_name"]
    from_payments = subject.tokenise_row({"counterparty_name": name}, ("counterparty_name",))[0][
        "counterparty_name"
    ]
    assert from_customers == from_payments == subject.token(name)


def test_different_values_yield_different_tokens():
    subject = tokeniser()
    assert subject.token("Jan Kowalski") != subject.token("Jan Kowalskı")


def test_a_different_salt_yields_a_different_token():
    other = Tokeniser.from_environment({"PII_TOKEN_SALT": SALT + "!"})
    assert other.token("Jan Kowalski") != tokeniser().token("Jan Kowalski")


def test_tokenise_row_leaves_unnamed_columns_alone():
    row = {"customer_id": 4711, "full_name": "Jan Kowalski", "city": "Berlin"}
    rewritten, _ = tokeniser().tokenise_row(row, ("full_name",))
    assert rewritten["customer_id"] == 4711
    assert rewritten["city"] == "Berlin"
    assert rewritten["full_name"] != "Jan Kowalski"


def test_tokenise_row_does_not_mutate_its_input():
    row = {"full_name": "Jan Kowalski"}
    tokeniser().tokenise_row(row, ("full_name",))
    assert row == {"full_name": "Jan Kowalski"}


def test_tokenise_row_reports_the_pairs_it_produced_and_skips_nulls():
    row = {"full_name": "Jan Kowalski", "email": None}
    rewritten, seen = tokeniser().tokenise_row(row, ("full_name", "email"))
    assert rewritten["email"] is None
    assert seen == {"Jan Kowalski": tokeniser().token("Jan Kowalski")}


def test_a_column_absent_from_the_row_is_skipped_rather_than_invented():
    rewritten, seen = tokeniser().tokenise_row({"full_name": "Jan"}, ("full_name", "iban"))
    assert "iban" not in rewritten
    assert set(seen) == {"Jan"}


def test_a_non_string_value_is_rendered_deterministically():
    subject = tokeniser()
    assert subject.token(4711) == subject.token("4711")
