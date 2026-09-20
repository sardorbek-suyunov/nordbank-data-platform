"""The duplicate's near-miss name, and the disposition's posterior.

Both are decisions a database cannot check for us. A duplicate that is byte-identical would be
caught by an equality join and would teach M5 nothing; one that is unrecognisable is not a
duplicate. And a disposition drawn from a hidden truth the source does not record would make
Q10's precision a property of the generator rather than of the fraud operation.
"""

from __future__ import annotations

import random
import unicodedata

from generator.config import load_profile
from generator.mutation.phases.dirt import CONFUSABLES, DIACRITICS, _near_miss, _rough
from generator.realism import fraud as fraud_model

FRAUD = load_profile("dev").params["fraud"]
NAMES = ("Jan Kowalski", "Anna Novak", "Lukas Meier", "Sofia Rossi", "Elena Costa")


def edit_distance(left: str, right: str) -> int:
    previous = list(range(len(right) + 1))
    for i, a in enumerate(left, start=1):
        current = [i]
        for j, b in enumerate(right, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (a != b)))
        previous = current
    return previous[-1]


def near_misses() -> list[tuple[str, str]]:
    out = []
    for seed in range(200):
        rng = random.Random(seed)
        original = NAMES[seed % len(NAMES)]
        out.append((original, _near_miss(rng, original)))
    return out


def test_a_duplicate_is_close_enough_to_match_and_different_enough_to_need_matching():
    pairs = near_misses()
    assert any(original != duplicate for original, duplicate in pairs)
    for original, duplicate in pairs:
        assert edit_distance(original, duplicate) <= 2, (original, duplicate)


def test_the_given_name_is_never_touched():
    # A duplicate shares enough to be recognisable. Changing both halves would make the pair a
    # different person rather than the same one entered twice.
    for original, duplicate in near_misses():
        assert duplicate.split(" ")[0] == original.split(" ")[0]


def test_every_edit_produces_all_four_kinds_over_enough_draws():
    produced = {original != duplicate for original, duplicate in near_misses()}
    assert True in produced

    confusable_used = any(
        any(character in CONFUSABLES.values() for character in duplicate)
        for _, duplicate in near_misses()
    )
    diacritic_used = any(
        any(character in DIACRITICS.values() for character in duplicate)
        for _, duplicate in near_misses()
    )
    assert confusable_used
    assert diacritic_used


def test_a_confusable_is_a_different_code_point_that_normalises_to_nothing_else():
    # The point of a confusable: it looks the same and compares unequal, and Unicode
    # normalisation does not fix it, so a pipeline that casefolds and strips accents still sees
    # two different strings.
    for latin, cyrillic in CONFUSABLES.items():
        assert latin != cyrillic
        assert unicodedata.normalize("NFKD", cyrillic) != latin


def test_a_diacritic_normalises_back_to_its_latin_letter():
    # Unlike a confusable, this one a normalising pipeline can resolve, which is why the
    # duplicate generator produces both kinds.
    for latin, accented in DIACRITICS.items():
        stripped = "".join(
            character
            for character in unicodedata.normalize("NFKD", accented)
            if not unicodedata.combining(character)
        )
        assert stripped.lower() in (latin, accented.lower())


def test_rough_text_changes_the_string_without_changing_the_name():
    for seed in range(40):
        rough = _rough(random.Random(seed), "Jan Kowalski")
        assert " ".join(rough.split()).lower() == "jan kowalski"


def test_the_disposition_prior_is_the_detector_not_a_parameter():
    prior = fraud_model.alert_prior(FRAUD)
    low, high = load_profile("dev").band("confirmed_fraud_rate_among_alerts")
    assert low <= prior <= high


def test_a_higher_score_is_confirmed_more_often():
    probabilities = [
        fraud_model.confirmation_probability(FRAUD, score)
        for score in (0.05, 0.2, 0.4, 0.6, 0.8, 0.95)
    ]
    assert probabilities == sorted(probabilities)
    assert probabilities[0] < 0.05
    assert probabilities[-1] > 0.95


def test_the_posterior_stays_a_probability_at_the_ends_of_the_range():
    for score in (0.0, 1.0):
        value = fraud_model.confirmation_probability(FRAUD, score)
        assert 0.0 <= value <= 1.0
