"""Profile parameters, read from `generator/profiles.yml` and validated on load.

Every tunable number in the generator lives in that file. Nothing here invents a default for a
missing parameter: a profile that omits something fails to load, because a generator that
quietly substitutes its own number for one the realism document claims to justify is worse than
one that refuses to start.

No generator branches on the profile name. A profile is a set of values to read.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

PROFILES_PATH = Path(__file__).resolve().parent / "profiles.yml"
SUPPORTED_VERSION = 1
PROFILE_NAMES = ("ci", "dev", "full")

# A mix is a probability distribution over named outcomes, so it sums to one. The tolerance is
# generous enough for the hand-written decimals in the file and far tighter than any difference
# that would change a distribution's shape.
MIX_TOLERANCE = 1e-6

REQUIRED_SECTIONS = (
    "acquisition",
    "customers",
    "accounts",
    "cards",
    "merchants",
    "agent_locations",
    "transactions",
    "amounts",
    "payments",
    "fraud",
    "lending",
    "attrition",
    "digital",
    "mutation",
    "ledger",
    "loading",
    "bands",
)

REQUIRED_PROFILE_KEYS = ("customers", "history_months", "assert_statistical_bands")


class ConfigError(Exception):
    """Raised when `profiles.yml` cannot be trusted. Nothing is generated from a bad profile."""


@dataclass(frozen=True)
class Profile:
    """One scale, with every parameter the generator reads resolved."""

    name: str
    customers: int
    history_months: int
    assert_statistical_bands: bool
    params: dict[str, Any]

    def section(self, name: str) -> dict[str, Any]:
        try:
            return self.params[name]
        except KeyError as error:  # pragma: no cover - validation catches this at load
            raise ConfigError(f"profile {self.name}: no section {name!r}") from error

    def band(self, name: str) -> tuple[float, float]:
        bands = self.section("bands")
        if name not in bands:
            raise ConfigError(f"profile {self.name}: no band {name!r}")
        band = bands[name]
        if isinstance(band, dict):
            return float(band["min"]), float(band["max"])
        raise ConfigError(f"profile {self.name}: band {name!r} is not a min/max pair")

    def limit(self, name: str) -> float:
        """A band expressed as a single ceiling rather than an interval."""
        bands = self.section("bands")
        if name not in bands:
            raise ConfigError(f"profile {self.name}: no band {name!r}")
        return float(bands[name])


@dataclass(frozen=True)
class RunConfig:
    """The three inputs that determine the output, resolved and validated."""

    profile: Profile
    seed: int
    anchor: dt.date

    @property
    def history_start(self) -> dt.date:
        """The first day of simulated history.

        Months are counted back from the anchor, so the window always ends on the anchor and a
        profile's history length is exact rather than approximate.
        """
        return _add_months(self.anchor, -self.profile.history_months)

    @property
    def months(self) -> list[dt.date]:
        """The first day of every month the history covers, oldest first."""
        first = self.history_start.replace(day=1)
        out: list[dt.date] = []
        cursor = first
        while cursor <= self.anchor:
            out.append(cursor)
            cursor = _add_months(cursor, 1)
        return out


def _add_months(value: dt.date, months: int) -> dt.date:
    """Shift a date by whole months, clamping the day to the target month's length."""
    total = value.year * 12 + (value.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    day = min(value.day, _days_in_month(year, month))
    return dt.date(year, month, day)


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (dt.date(year, month + 1, 1) - dt.timedelta(days=1)).day


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _check_mix(where: str, mix: Any) -> None:
    if not isinstance(mix, dict) or not mix:
        raise ConfigError(f"{where} is not a non-empty mapping of outcome to probability")
    total = 0.0
    for outcome, weight in mix.items():
        # A boolean outcome name is always the same mistake: YAML 1.1 reads bare NO, ON, OFF,
        # YES, Y and N as booleans, so the country code NO becomes False and stops matching
        # anything in ref.countries. Naming it here is worth more than the foreign key
        # violation it would otherwise become several minutes into a load.
        if isinstance(outcome, bool):
            raise ConfigError(
                f"{where} has a boolean outcome name: quote it in profiles.yml. "
                f"YAML reads bare NO, ON, OFF, YES, Y and N as booleans"
            )
        if not isinstance(weight, int | float) or isinstance(weight, bool):
            raise ConfigError(f"{where}.{outcome} is not a number")
        if weight < 0:
            raise ConfigError(f"{where}.{outcome} is negative")
        total += float(weight)
    if abs(total - 1.0) > MIX_TOLERANCE:
        raise ConfigError(f"{where} sums to {total!r}, not 1")


def _check_share(where: str, value: Any) -> None:
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ConfigError(f"{where} is not a number")
    if not 0.0 <= float(value) <= 1.0:
        raise ConfigError(f"{where} is {value!r}, outside [0, 1]")


def _leaf_mappings(where: str, value: Any) -> list[tuple[str, Any]]:
    """`value` itself, or its members when it is a mapping of mappings."""
    if isinstance(value, dict) and value and all(isinstance(v, dict) for v in value.values()):
        return [(f"{where}.{inner}", inner_value) for inner, inner_value in value.items()]
    return [(where, value)]


def _is_probability_key(key: str) -> bool:
    """Whether a parameter name promises a value in [0, 1].

    `_rate` is ambiguous: a default rate is a probability, a growth rate is not and a
    per-period count is not. The exclusions name the ones that are not.
    """
    if key.endswith(("_share", "_hazard")):
        return True
    if key.endswith("_rate"):
        return not key.startswith(("monthly_growth", "fx_markup")) and "per_" not in key
    return False


def _validate(name: str, params: dict[str, Any]) -> None:
    for section in REQUIRED_SECTIONS:
        if section not in params:
            raise ConfigError(f"profile {name}: missing section {section!r}")

    # Checked by naming convention rather than from an explicit list, so that a parameter added
    # later is checked without anybody remembering to add it here.
    #
    #   *_mix, count_weights, devices_per_customer_weights  a distribution, sums to one
    #   *_share, *_hazard, *_rate                           a probability, inside [0, 1]
    #
    # Either may be nested one level to vary by another dimension — channel_mix_by_type is a
    # mix per transaction type, card_present_share_by_channel a share per channel — so a dict
    # whose values are themselves dicts is walked rather than summed.
    for section_name, section in params.items():
        if not isinstance(section, dict):
            continue
        for key, value in section.items():
            where = f"{name}.{section_name}.{key}"
            if key.endswith("_mix") or key in ("count_weights", "devices_per_customer_weights"):
                for leaf_where, leaf in _leaf_mappings(where, value):
                    _check_mix(leaf_where, leaf)
            elif _is_probability_key(key):
                if isinstance(value, dict):
                    for inner, inner_value in value.items():
                        _check_share(f"{where}.{inner}", inner_value)
                else:
                    _check_share(where, value)

    hours = params["transactions"]["hour_weights"]
    if len(hours) != 24:
        raise ConfigError(f"profile {name}: transactions.hour_weights has {len(hours)} entries")
    months = params["transactions"]["month_weights"]
    if len(months) != 12:
        raise ConfigError(f"profile {name}: transactions.month_weights has {len(months)} entries")

    seasoning = params["lending"]["seasoning_curve"]
    if not seasoning or any(
        later < earlier for earlier, later in zip(seasoning, seasoning[1:], strict=False)
    ):
        raise ConfigError(f"profile {name}: lending.seasoning_curve must be non-decreasing")
    if not seasoning[0] >= 0.0 and seasoning[-1] <= 1.0:
        raise ConfigError(f"profile {name}: lending.seasoning_curve must lie within [0, 1]")

    for band_name, band in params["bands"].items():
        if isinstance(band, dict):
            if "min" not in band or "max" not in band:
                raise ConfigError(f"profile {name}: band {band_name} needs a min and a max")
            if float(band["min"]) > float(band["max"]):
                raise ConfigError(f"profile {name}: band {band_name} has min above max")

    cash_multiples = params["amounts"]["cash_multiples"]
    cash_weights = params["amounts"]["cash_multiple_weights"]
    if len(cash_multiples) != len(cash_weights):
        raise ConfigError(
            f"profile {name}: amounts.cash_multiples and its weights differ in length"
        )

    if params["ledger"]["entry_rows_per_transaction"] < 1:
        raise ConfigError(f"profile {name}: ledger.entry_rows_per_transaction must be positive")
    if params["loading"]["copy_chunk_rows"] < 1:
        raise ConfigError(f"profile {name}: loading.copy_chunk_rows must be positive")


def load_profiles(path: Path | None = None) -> dict[str, Profile]:
    """Read and validate every profile. A malformed file yields no profiles at all."""
    source = path or PROFILES_PATH
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ConfigError(f"no profile file at {source}") from error
    except yaml.YAMLError as error:
        raise ConfigError(f"{source.name} is not valid YAML: {error}") from error

    if not isinstance(raw, dict):
        raise ConfigError(f"{source.name} does not contain a mapping")
    if raw.get("version") != SUPPORTED_VERSION:
        raise ConfigError(
            f"{source.name} declares version {raw.get('version')!r}, "
            f"and this generator reads version {SUPPORTED_VERSION}"
        )

    defaults = raw.get("defaults")
    profiles = raw.get("profiles")
    if not isinstance(defaults, dict) or not isinstance(profiles, dict):
        raise ConfigError(f"{source.name} needs a defaults mapping and a profiles mapping")

    missing = [name for name in PROFILE_NAMES if name not in profiles]
    if missing:
        raise ConfigError(f"{source.name} is missing profile(s): {', '.join(missing)}")

    resolved: dict[str, Profile] = {}
    for name in PROFILE_NAMES:
        override = profiles[name] or {}
        if not isinstance(override, dict):
            raise ConfigError(f"profile {name} is not a mapping")
        for key in REQUIRED_PROFILE_KEYS:
            if key not in override:
                raise ConfigError(f"profile {name}: missing {key!r}")
        params = _deep_merge(
            defaults,
            {k: v for k, v in override.items() if k not in REQUIRED_PROFILE_KEYS},
        )
        _validate(name, params)
        if override["customers"] < 1:
            raise ConfigError(f"profile {name}: customers must be positive")
        if override["history_months"] < 1:
            raise ConfigError(f"profile {name}: history_months must be positive")
        resolved[name] = Profile(
            name=name,
            customers=int(override["customers"]),
            history_months=int(override["history_months"]),
            assert_statistical_bands=bool(override["assert_statistical_bands"]),
            params=params,
        )
    return resolved


def load_profile(name: str, path: Path | None = None) -> Profile:
    profiles = load_profiles(path)
    if name not in profiles:
        raise ConfigError(f"unknown profile {name!r}; expected one of {', '.join(PROFILE_NAMES)}")
    return profiles[name]
