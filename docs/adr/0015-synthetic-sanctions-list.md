# 0015 — The sanctions list is the real feed's shape with synthetic content

Status: Accepted
Date: 2026-09-23

## Context

Specification 006 as issued asked for the OpenSanctions consolidated list, downloaded whole and
landed as a versioned snapshot, with the M3 screening fixture merged into it so screening has
something to match. Its criterion 14 asks that no real sanctioned individual's name appear in the
repository, in any fixture, **or in any landed object**. The two cannot both hold: landing the
real list lands every name on it.

The size of the conflict was measured on 2026-09-22 against the publisher's index. The
consolidated dataset held 300,971 entities; `entities.ftm.json` was 365 MB and the smallest
resource, `targets.simple.csv`, 73 MB. It exports four times a day, each export under a new
version string. The data is licensed Creative Commons 4.0 Attribution NonCommercial.

What the list holds matters more than its size. A sanctions or politically-exposed-person entry is
a statement about an identified, usually living person: that a government has designated them,
or that they hold or held public office, with dates of birth, nationalities and aliases beside
the name. Much of it is public, but public is not the same as free to process for any purpose,
and a designation or a PEP status is the kind of fact the GDPR treats with particular care
because of what it does to the person it describes. A regulated bank screens against such a list
because a legal obligation requires it and gives it a lawful basis for the processing, with the
retention, access control and purpose limitation that basis brings.

This platform is a portfolio lake. It has no such obligation and no such basis. It has no
retention story for bronze beyond M7's compaction, its quarantine and inbound areas are
deliberately simple, and at M9 its gold layer is exported to a publicly deployed application.
Landing the real list would put the personal data of three hundred thousand people into a
system that could not say why it holds it, could not honour a request about it, and would
publish what it derived from it.

The M3 fixture had already made the corresponding choice for the payment side: every screening
positive in `generator/fixtures/screening_positives.csv` is named `ZZ-TESTCASE … SANCTIONS-FIXTURE`
or `ZZ-TESTENTITY … SANCTIONS-FIXTURE`, a name no person or company has.

## Decision

The platform lands a sanctions snapshot that reproduces the real feed's shape exactly — the
publisher's index document, one FollowTheMoney entity per line with the documented top-level
keys, the multi-valued `properties`, a version string in the publisher's form and an export
timestamp — with **synthetic content**. The simulation publishes it to the inbound bucket; every
entity carries a `ZZ-` marker name; the M3 fixture's twelve names are among its entities, so a
payment drawn from the fixture can match.

The contract names the real feed as its source of truth and records what was measured about it:
the index and resource URLs, the version semantics, the export schedule, the sizes and the
licence. The real list is never downloaded into, committed to or landed by the platform.

Criterion 14 stands unchanged and becomes checkable by construction: every name the snapshot
lands matches the marker pattern, and the acceptance evidence asserts it over every landed
object rather than sampling it.

## Consequences

- Snapshot mode is exercised exactly as it would be against the real feed: versioning by the
  publisher's string, identity by content checksum, the no-op on unchanged content and a new
  batch on changed content, and the screening fixture present and matchable.
- **The shape can drift from the real feed unobserved.** The synthetic publisher writes the
  shape the contract describes, so a change to the real format is not seen by ingestion at all.
  It is seen only by a person comparing the contract with the publisher's documentation, and
  the contract records the date that was last done. This is the same trade the recorded
  fixtures make for the APIs, without even a live probe to catch it, because a live probe of the
  real list is the download this record rules out.
- Screening, when it is built, demonstrates the mechanism — resolve through the vault, match
  against a named version, record the match — and says nothing about match quality against
  real names, such as transliteration and alias handling. A real deployment would evaluate
  those against the real list under its own lawful basis.
- The volume is not representative: a few hundred entities against three hundred thousand. Any
  performance claim about screening has to be made against a measured volume elsewhere.

## Alternatives considered

**Land the real list, and narrow criterion 14 to the repository and the fixtures.** The most
realistic option, and what the specification first asked for. Rejected because it resolves the
contradiction by relaxing the criterion that protects the people on the list, and because the
M9 deployment makes the processing public in effect; a portfolio's wish to look realistic is not
a purpose that justifies it.

**Land the real list with the names removed or tokenised.** Keeps the volume and most of the
shape. Rejected because the rest of an entry — date of birth, nationality, position, the fact of
designation — still describes an identifiable person, and because a list without names cannot
be screened against, which is the only reason to hold it.

**Download the real list only in local runs and never in CI.** Rejected because a local run is
exactly where the lake lives, and the landed objects would be real personal data on a developer
machine with no more justification than in any other environment.
