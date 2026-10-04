# Card processor clearing file, layout 1

The file specification the `cardnet` contracts are authored against. Nordbank's card processor
sends one clearing file per settlement date, covering every card network it clears for the
bank. It is an **issuer clearing file**, sent by the processor to the issuing bank, and not a
scheme-to-scheme settlement report; the difference decides what the file may carry, and
`docs/generator_realism.md` states why.

The processor is simulated (`generator/settlement/`), and this document is the specification
the simulation writes to and the ingestion path reads against. The ingestion path knows this
document and nothing about how the file was made.

## Delivery

- One file per settlement date, named `NBK_CLR_<YYYYMMDD>_<NN>.csv`, delivered to the
  `cardnet/` prefix of the inbound bucket. The name is a convenience: a file is identified by
  its content (ADR 0013).
- UTF-8, comma-delimited, quoted where a field contains a comma or a quote (RFC 4180), LF line
  endings.
- A file for settlement date D normally arrives on D. A late file arrives three days after its
  settlement date.
- A correction is sent as `NBK_CLR_<YYYYMMDD>_<NN>_R<revision>.csv`, the day after the file it
  corrects. A transmission sent again after a failure carries `_RESEND`, and a copy the
  processor's transfer drops again carries `_RETRY`. None of these names is identity either.
- A file is produced, and laid out, on the day it is sent: its header's `created_at` is that
  day, and its layout is the one the processor used that day, whatever settlement date it
  covers.

## Records

The first field of every line says what the line is.

| Line | First field | Fields |
|---|---|---|
| 1 | `H` | `record_type, processor_id, settlement_date, file_sequence, revision, created_at, layout_version` |
| 2 | `record_type` | The column line: the names of the detail fields, in order |
| 3 to n | `D` | One detail record per cleared transaction, fields as named by line 2 |
| after the details | `T` | `record_type, network, settlement_currency, record_count, amount_total`, one per network and currency |
| last | `Z` | `record_type, detail_record_count` |

The header's settlement date and file sequence make every file's bytes distinct, so an empty
day's file cannot collide with another's.

**Sequence and revision are different things.** `file_sequence` numbers the files the processor
sends for one settlement date, and files with different sequences add up. `revision` numbers
the versions of one of those files: 1 as first sent, higher for a correction, and the highest
revision of a sequence replaces the lower ones. The same settlement date, sequence and revision
never names two different files.

**The trailer is the sender's control total.** `record_count` and `amount_total` are computed
by the processor over every detail record it wrote for that network and currency, before
transmission. A detail record damaged in transit still counts in the trailer, which is what
lets the platform reconcile the file against the ledger (trailer to ledger) independently of
reconciling the file against itself (landed plus quarantined to records read).

## Detail fields, layout 1

| Field | Meaning |
|---|---|
| `transaction_reference` | The reference the transaction is known by outside the bank: the bank's `core.transactions.transaction_reference` |
| `transaction_date` | The day the cardholder transacted |
| `clearing_date` | The day the item was presented and cleared |
| `network` | The card network the item cleared through |
| `card_reference` | The issuer's reference for the card, which an issuer clearing file carries so the bank can post to the right card |
| `masked_pan` | The first six and last four digits of the card number, the rest masked |
| `merchant_category_code` | Four-digit MCC; blank where there is no merchant, as for a cash withdrawal |
| `merchant_name` | The merchant as the acquirer sent it; blank where there is none |
| `presentment` | `CP` card present, `CNP` card not present |
| `settlement_currency` | ISO 4217 code of the amount |
| `settlement_amount` | Four decimal places, positive where the bank owes the network, negative where the network owes the bank |

Settlement is one calendar day after clearing for both networks. The settlements contract's
`source_of_truth.settlement_lag_days` is the machine-readable copy of this sentence, and the one
the platform reads.

## Fields the processor added later

| Field | From | Meaning | Classification |
|---|---|---|---|
| `acquirer_reference_number` | anchor plus 20 | The acquirer's 23-digit reference for the clearing record: a format digit, the acquirer's six-digit identifier, the clearing date as year digit and day of year, an eleven-digit sequence and a Luhn check digit | `non-personal`: it identifies a clearing record, an event, not a person, the same reasoning that makes `transaction_reference` non-personal; the record reaches a person only through `card_reference`, which is classified and tokenised |

No contract describes it: it is additive drift, logged and not landed, and it survives in bronze
in each record's `_raw_payload`. It has no counterpart in the core banking source or any other
feed, which is why it was chosen: an additive field that duplicated something the platform
already sources, such as an interchange amount, would be a second source of that fact.
