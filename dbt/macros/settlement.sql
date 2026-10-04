{#
  The clearing file's batch arithmetic (specification 009 section 1, `architecture.md`, "Batch
  arithmetic"), applied once and shared by the settlement models.

  A file's header declares its settlement date, the sender's `file_sequence` and a `revision`,
  and every detail and trailer record carries all three. Files of different sequences add.
  Within one settlement date and sequence the highest declared revision replaces the others,
  whatever order they arrived in, so arrival order, batch ids and ingest dates play no part.

  The winner is read from the detail lines and the trailers together. A correction whose every
  detail record was quarantined lands its trailers and no line; chosen from the lines alone, the
  first revision's lines would survive beside the correction's trailers. A correction with no
  record at all, neither line nor trailer, is invisible in bronze, and silver does not read the
  registry to find it: a known gap the specification records.

  `lines` and `trailers` are relations or CTE names.
#}
{% macro settlement_winners(lines, trailers) -%}
select settlement_date, file_sequence, max(revision) as revision
from (
    select settlement_date, file_sequence, revision from {{ lines }}
    union all
    select settlement_date, file_sequence, revision from {{ trailers }}
) as declared
group by settlement_date, file_sequence
{%- endmacro %}

{# The records of `records`, lines or trailers, that belong to their date and sequence's winner. #}
{% macro settlement_winning_records(records, lines, trailers) -%}
select records.*
from {{ records }} as records
inner join ({{ settlement_winners(lines, trailers) }}) as winners
    on records.settlement_date = winners.settlement_date
    and records.file_sequence = winners.file_sequence
    and records.revision = winners.revision
{%- endmacro %}

{#
  A line resolved to the bank's own entities, without correction (specification 009 section 2).

  `card_id` comes from the distinct pair of card token and card: `sl_cards` holds a version per
  change, so joining the token to it directly repeats a line once per version of its card. The
  token and the card are one to one. `transaction_id` and `merchant_id` come through the
  transaction reference, which `sl_transactions` holds once. A line that resolves to nothing is
  kept, with nulls, because a clearing item the bank cannot place is exactly what a
  reconciliation must still see.
#}
{% macro settlement_resolution(lines, cards, transactions) -%}
select
    lines.*,
    cards.card_id,
    transactions.transaction_id,
    transactions.merchant_id
from {{ lines }} as lines
left join (select distinct card_reference, card_id from {{ cards }}) as cards
    on lines.card_reference = cards.card_reference
left join {{ transactions }} as transactions
    on lines.transaction_reference = transactions.transaction_reference
{%- endmacro %}
