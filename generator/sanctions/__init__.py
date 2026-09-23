"""The sanctions list publisher: a synthetic list in the real feed's shape (ADR 0015).

Simulation infrastructure, standing in for OpenSanctions the way `generator/settlement/` stands
in for the card processor. It publishes to the inbound bucket an index document and one
FollowTheMoney entity per line, in the shape `contracts/opensanctions/entities.yml` records from
the publisher's documentation, and the ingestion path reads it knowing nothing else about it.

**Every name is a marker name.** Each entity's caption, names and aliases begin `ZZ-` and end
`SANCTIONS-FIXTURE`, the convention the M3 screening fixture set, so no name in the list is a
name any person or company has, and criterion 14 can be checked over every landed object by
pattern rather than by sampling. The fixture's twelve names are among the entities, which is
what lets a payment drawn from the fixture match.

**Versions and content change on different clocks, as the real feed's do.** The real list
exports four times a day under a new version string whether or not anything changed. This one
publishes once per simulated day under a new version string, and its content changes only on
Mondays, when a handful of entities are designated and a few delisted. Most new versions are
therefore new strings over identical content, which is the case identity by content checksum
exists for (ADR 0013).

The list for a day is a pure function of the seed, the anchor and the day.
"""
