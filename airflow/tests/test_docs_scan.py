"""The documentation artefact scan finds every secret in the forms it can take."""

from pathlib import Path

from bronze_pii_scan import Matcher, encodings
from docs_scan import scan, secrets

ROOT = Path(__file__).resolve().parents[2]

TEMPLATE = (
    "MINIO_ROOT_PASSWORD=__GENERATE__      # the lake\n"
    "AIRFLOW_CONN_NORDBANK_SOURCE_DB=postgresql://reader:__GENERATE__@db:5432/x  # uri\n"
    "FRED_API_KEY=__EXTERNAL__\n"
    "LAKE_BUCKET=nordbank-lake\n"
)
ENV = (
    "MINIO_ROOT_PASSWORD=Zq8s0VnB1mWx4Tt2\n"
    "AIRFLOW_CONN_NORDBANK_SOURCE_DB=postgresql://reader:Kd9fQ2Lp7Xs1Vb3N@db:5432/x\n"
    "FRED_API_KEY=__EXTERNAL__\n"
    "LAKE_BUCKET=nordbank-lake\n"
)


def test_a_secret_inside_a_uri_is_found_as_its_generated_part():
    found = secrets(ENV, TEMPLATE)
    assert found == {
        "MINIO_ROOT_PASSWORD": "Zq8s0VnB1mWx4Tt2",
        "AIRFLOW_CONN_NORDBANK_SOURCE_DB": "Kd9fQ2Lp7Xs1Vb3N",
    }


def test_the_real_template_marks_at_least_the_known_secrets():
    template = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert template.count("__GENERATE__") >= 13


def test_a_planted_secret_and_a_planted_vault_value_are_found(tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text(
        '<script>n={"description":"Kd9fQ2Lp7Xs1Vb3N","name":"Zo\\u00eb Tester"}</script>',
        encoding="utf-8",
    )
    matcher = Matcher([("token-1", encodings("Zoë Tester"))])
    hits = scan(site, secrets(ENV, TEMPLATE), matcher)
    assert any("AIRFLOW_CONN_NORDBANK_SOURCE_DB" in hit for hit in hits)
    assert any("token-1" in hit for hit in hits)
    assert not any("Kd9f" in hit for hit in hits), "a hit must never print the value"


def test_a_clean_site_has_no_hit(tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<p>Classification: `identifier`</p>", encoding="utf-8")
    assert scan(site, secrets(ENV, TEMPLATE), Matcher([("t", encodings("Zoë Tester"))])) == []
