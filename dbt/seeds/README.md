# dbt/seeds

One seed, `seed_generalisation_bands`: the age and tenure band edges silver generalises
quasi-identifiers by (specification 008 section 8). It is a modelling decision that exists
nowhere else.

Reference data is not seeded here. The four seeds planned at M0 duplicated what the source
database's `ref` schema holds, and were dropped at M2: reference data reaches the warehouse
through extraction like any other entity. Anything that changes on a schedule is a source, not a
seed.
