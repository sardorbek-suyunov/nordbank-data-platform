"""Profile loading and validation.

The validator exists so that a bad profile fails at load with the parameter named, rather than
several minutes into a generation run as a foreign key violation or a distribution that quietly
does nothing. These tests check that each class of badness is caught.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import yaml

from generator.config import (
    PROFILE_NAMES,
    ConfigError,
    RunConfig,
    load_profile,
    load_profiles,
)


def test_every_profile_loads_and_validates():
    profiles = load_profiles()
    assert set(profiles) == set(PROFILE_NAMES)


def test_profiles_differ_only_in_scale():
    """Spec 003 section 1: profiles differ only in parameters, and in practice only in scale.

    If a profile ever needs a different distribution to work, that is a defect in the
    distribution, and this test is where it becomes visible.
    """
    profiles = load_profiles()
    reference = profiles["dev"].params
    for name in ("ci", "full"):
        differences = [
            section for section in reference if profiles[name].params[section] != reference[section]
        ]
        assert differences == [], (
            f"profile {name} overrides {differences}, so it is not the same bank at a "
            f"different size"
        )


def test_scale_increases_across_profiles():
    profiles = load_profiles()
    assert profiles["ci"].customers < profiles["dev"].customers < profiles["full"].customers
    assert (
        profiles["ci"].history_months
        < profiles["dev"].history_months
        < profiles["full"].history_months
    )


def test_statistical_bands_are_asserted_only_where_n_supports_them():
    profiles = load_profiles()
    assert profiles["ci"].assert_statistical_bands is False
    assert profiles["dev"].assert_statistical_bands is True
    assert profiles["full"].assert_statistical_bands is True


def _write(tmp_path: Path, mutate) -> Path:
    raw = yaml.safe_load(
        (Path(__file__).resolve().parent.parent / "profiles.yml").read_text(encoding="utf-8")
    )
    mutate(raw)
    target = tmp_path / "profiles.yml"
    target.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return target


def test_a_mix_that_does_not_sum_to_one_is_refused(tmp_path: Path):
    def mutate(raw):
        raw["defaults"]["customers"]["kyc_status_mix"]["verified"] += 0.2

    with pytest.raises(ConfigError, match="sums to"):
        load_profiles(_write(tmp_path, mutate))


def test_a_share_outside_zero_to_one_is_refused(tmp_path: Path):
    def mutate(raw):
        raw["defaults"]["accounts"]["joint_share"] = 1.4

    with pytest.raises(ConfigError, match="outside"):
        load_profiles(_write(tmp_path, mutate))


def test_a_boolean_outcome_name_is_refused_with_the_yaml_hint(tmp_path: Path):
    """The country code NO, unquoted, becomes False in YAML 1.1 and matches no country."""

    def mutate(raw):
        mix = raw["defaults"]["customers"]["country_mix"]
        mix[False] = mix.pop("NO")

    with pytest.raises(ConfigError, match="boolean outcome name"):
        load_profiles(_write(tmp_path, mutate))


def test_a_missing_section_is_refused(tmp_path: Path):
    def mutate(raw):
        del raw["defaults"]["fraud"]

    with pytest.raises(ConfigError, match="missing section"):
        load_profiles(_write(tmp_path, mutate))


def test_the_wrong_number_of_hour_weights_is_refused(tmp_path: Path):
    def mutate(raw):
        raw["defaults"]["transactions"]["hour_weights"] = [1.0] * 23

    with pytest.raises(ConfigError, match="hour_weights"):
        load_profiles(_write(tmp_path, mutate))


def test_a_decreasing_seasoning_curve_is_refused(tmp_path: Path):
    """The curve is cumulative, so it cannot go down."""

    def mutate(raw):
        raw["defaults"]["lending"]["seasoning_curve"] = [0.0, 0.5, 0.2, 1.0]

    with pytest.raises(ConfigError, match="non-decreasing"):
        load_profiles(_write(tmp_path, mutate))


def test_an_unsupported_version_is_refused(tmp_path: Path):
    def mutate(raw):
        raw["version"] = 99

    with pytest.raises(ConfigError, match="version"):
        load_profiles(_write(tmp_path, mutate))


def test_history_window_ends_on_the_anchor():
    config = RunConfig(profile=load_profile("dev"), seed=1, anchor=dt.date(2026, 9, 18))
    assert config.anchor == dt.date(2026, 9, 18)
    assert config.history_start == dt.date(2023, 9, 18)
    assert config.months[0] <= config.history_start
    assert config.months[-1] <= config.anchor


def test_bands_are_readable_and_ordered():
    profile = load_profile("dev")
    for name in (
        "fraud_rate",
        "alert_precision",
        "confirmed_fraud_rate_among_alerts",
        "login_precedes_transaction_share",
    ):
        # alert_recall is deliberately absent: it is unobservable in the loaded database,
        # so a band on it could never be checked against anything.
        low, high = profile.band(name)
        assert low <= high
    assert profile.limit("benford_mad_max") > 0
