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

## Records

The first field of every line says what the line is.

| Line | First field | Fields |
|---|---|---|
| 1 | `H` | `record_type, processor_id, settlement_date, file_sequence, created_at, layout_version` |
| 2 | `record_type` | The column line: the names of the detail fields, in order |
| 3 to n | `D` | One detail record per cleared transaction, fields as named by line 2 |
| after the details | `T` | `record_type, network, settlement_currency, record_count, amount_total`, one per network and currency |
| last | `Z` | `record_type, detail_record_count` |

The header's settlement date and file sequence make every file's bytes distinct, so an empty
day's file cannot collide with another's.

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

Settlement is one calendar day after clearing for both networks.
