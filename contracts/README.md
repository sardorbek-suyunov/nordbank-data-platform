# contracts

Data contracts for every ingested source: expected schema, types, nullability, primary key,
and the agreed behaviour when the source drifts.

A contract is the authority for what bronze accepts. Records that violate it are quarantined
with the reason attached, not dropped and not silently coerced.

Contracts are versioned; a breaking change means a new version and a migration note, so that
replaying an old partition still validates against the contract of its day.

Populated from M4.
