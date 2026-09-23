"""The phase bodies of the four feed DAGs (spec 006 section 8).

The shape is specification 005's, with one phase added in front for the two delivery modes:

- **discover**, no warehouse access: list and hash what the publisher has delivered;
- **open**, pooled: recognise what has already landed, choose the contract version in force for
  each batch's interval, and allocate batches, in one transaction;
- **extract**, mapped, no warehouse access: fetch or read, parse, validate, tokenise, land;
- **register**, pooled, `all_done`: register what wrote and fail what did not, in one
  transaction;
- **gate**, terminal: fail the run if a batch failed.

A unit of work is what one mapped extract task handles: an interval for the API feeds, one
delivered file for the settlement feed (which lands two entities from it), one snapshot for the
sanctions list.

**No partial load, as in 005.** Nothing is written until everything a unit needs has been
fetched and read, so a unit that fails part way writes nothing, and its batches are failed with
the reason by the register step. Their watermarks stay where they were, and the next run
repeats the whole unit.
"""

from __future__ import annotations

import datetime as dt
import functools
import os
from typing import Any

from nordbank_ops import registry

REPORT_KEY = "report"
EXTRACT_TASK_ID = "extract"

FX = "ecb"
CARDNET = "cardnet"
SANCTIONS = "opensanctions"
FRED = "fred"


# --- shared ---------------------------------------------------------------------------------


def _chains(system: str) -> dict:
    from nordbank_ops.contracts import load_chains
    from nordbank_ops.ingest import contract_root

    return load_chains(contract_root() / system)


def _logical(context: dict) -> tuple[dt.datetime, dt.date]:
    start = context["dag_run"].logical_date
    if start.tzinfo is None:
        start = start.replace(tzinfo=dt.UTC)
    start = start.astimezone(dt.UTC)
    return start, start.date()


def _conf(context: dict) -> dict:
    return dict(getattr(context["dag_run"], "conf", None) or {})


def _midnight(day: dt.date) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(), tzinfo=dt.UTC)


def _allocate(
    connection,
    chains: dict,
    entity: str,
    *,
    interval_start: dt.datetime,
    ingest_date: dt.date,
    watermark_from: dt.datetime | None,
    opened_at: dt.datetime,
    run_id: str,
) -> dict:
    from nordbank_ops import contracts as versions

    current = chains[entity][-1]
    version = versions.select(connection, current.source_system, entity, interval_start.date())
    contract = versions.body(chains, entity, version)
    key = registry.BatchKey(
        source_system=contract.source_system, entity=entity, interval_start=interval_start
    )
    allocation = registry.allocate(
        connection,
        key,
        source_schema=contract.source_schema,
        ingest_date=ingest_date,
        interval_end=interval_start + dt.timedelta(days=1),
        contract_version=contract.contract_version,
        watermark_from=watermark_from,
        opened_at=opened_at,
        triggering_run_id=run_id,
    )
    batch = registry.batch(connection, allocation.batch_id)
    return {
        "entity": entity,
        "batch_id": batch["batch_id"],
        "contract_version": int(batch["contract_version"]),
        "ingest_date": batch["ingest_date"].isoformat(),
        "opened_at": batch["opened_at"].isoformat(),
        "interval_start": batch["interval_start"].isoformat(),
        "watermark_from": (
            batch["watermark_from"].isoformat() if batch["watermark_from"] else None
        ),
        "reused": allocation.reused,
    }


def _resolved(batch: dict) -> dict:
    return {
        "batch_id": batch["batch_id"],
        "ingest_date": dt.date.fromisoformat(batch["ingest_date"]),
        "opened_at": dt.datetime.fromisoformat(batch["opened_at"]),
    }


def _contract(system: str, batch: dict):
    from nordbank_ops.contracts import body

    return body(_chains(system), batch["entity"], int(batch["contract_version"]))


def _inbound() -> tuple[Any, str]:
    from nordbank_ops import clients

    return clients.lake_client(), os.environ["INBOUND_BUCKET"]


def _lake() -> tuple[Any, str]:
    from nordbank_ops import clients

    return clients.lake_client(), clients.lake_bucket()


def _fetcher(conf: dict):
    from nordbank_ops.feeds.fetch import RetryPolicy, fetch

    policy = RetryPolicy(
        max_attempts=int(conf.get("max_attempts", 5)),
        base_delay=float(conf.get("base_delay", 1.0)),
        max_delay=float(conf.get("max_delay", 30.0)),
        timeout=float(conf.get("timeout", 20.0)),
    )
    return functools.partial(fetch, policy=policy)


def open_unit(system: str, allocate) -> list[dict]:
    """Run `allocate(connection, chains)` inside one warehouse transaction.

    Every contract version on disk is recorded first, so the selection sees a bump committed
    since the last run and refuses a recorded version whose file was edited.
    """
    from nordbank_ops import contracts as versions
    from nordbank_ops import warehouse

    chains = _chains(system)
    with warehouse.connect(read_only=False) as connection:
        connection.execute("begin transaction")
        try:
            versions.sync(connection, chains, dt.datetime.now(dt.UTC))
            units = allocate(connection, chains)
            connection.execute("commit")
        except Exception:
            connection.execute("rollback")
            raise
    return units


# --- ECB FX rates ------------------------------------------------------------------------------


def fx_open(context: dict) -> list[dict]:
    from nordbank_ops.feeds.fx import dates_to_fetch

    start, day = _logical(context)
    run_id = context["dag_run"].run_id
    opened_at = dt.datetime.now(dt.UTC)

    def allocate(connection, chains):
        held = registry.watermark(connection, FX, "fx_rates")
        dates = dates_to_fetch(held.date() if held else None, day)
        batch = _allocate(
            connection,
            chains,
            "fx_rates",
            interval_start=start,
            ingest_date=day,
            watermark_from=held,
            opened_at=opened_at,
            run_id=run_id,
        )
        return [{"batches": [batch], "dates": [d.isoformat() for d in dates]}]

    units = open_unit(FX, allocate)
    print(f"open: fx_rates for {day}: dates {units[0]['dates']}")
    return units


def fx_extract(unit: dict, context: dict) -> list[dict]:
    from nordbank_ops.feeds import fx
    from nordbank_ops.feeds.land import failed, land
    from nordbank_ops.tokenise import Tokeniser

    conf = _conf(context)
    (batch,) = unit["batches"]
    contract = _contract(FX, batch)
    base_url = conf.get("base_url") or os.environ["FRANKFURTER_BASE_URL"]
    dates = [dt.date.fromisoformat(d) for d in unit["dates"]]
    fetched = fx.fetch_dates(dates, contract, base_url=base_url, fetch=_fetcher(conf))

    if fetched.failure:
        report = failed(contract, batch, fetched.failure)
    else:
        client, bucket = _lake()
        report = land(
            client=client,
            bucket=bucket,
            contract=contract,
            batch=_resolved(batch),
            parsed=fetched.parsed,
            tokeniser=Tokeniser.from_environment(),
            source_file=f"{base_url.rstrip('/')}/<date>?base=EUR",
        )
        if report.status != "failed":
            report.watermark_to = _midnight(dates[-1])
    out = report.as_dict()
    out["requests"] = [o.as_dict() for o in fetched.outcomes]
    for outcome in fetched.outcomes:
        print(
            f"extract: fx {outcome.key}: {outcome.outcome} after {outcome.attempts} attempt(s)"
            f"{', ' + outcome.detail if outcome.detail else ''}"
        )
    return [out]


# --- card settlement files --------------------------------------------------------------------


def settlement_prefix() -> str:
    return os.environ.get("CARD_SETTLEMENT_PREFIX", "cardnet/")


def expected_prefix(day: dt.date) -> str:
    """Where the file for a settlement date arrives, by the specification's naming."""
    return f"{settlement_prefix()}NBK_CLR_{day:%Y%m%d}_"


def settlement_discover(context: dict) -> list[dict]:
    from nordbank_ops.feeds import identity

    client, bucket = _inbound()
    found = identity.discover(client, bucket, settlement_prefix(), ".csv")
    out = []
    for candidate in found:
        head = identity.read_object(client, bucket, candidate.key).split(b"\n", 1)[0]
        fields = head.decode("utf-8", errors="replace").split(",")
        settlement_date = fields[2] if len(fields) > 2 and fields[0] == "H" else None
        out.append({**candidate.as_dict(), "settlement_date": settlement_date})
    print(f"discover: {len(out)} file(s) under {bucket}/{settlement_prefix()}")
    return out


def settlement_open(context: dict, candidates: list[dict], sensed: dict | None) -> list[dict]:
    from nordbank_ops.feeds import identity

    start, day = _logical(context)
    run_id = context["dag_run"].run_id
    opened_at = dt.datetime.now(dt.UTC)

    def allocate(connection, chains):
        landed = identity.landed_as(connection, [c["checksum"] for c in candidates])
        units = []
        for candidate in candidates:
            item = identity.Candidate(candidate["key"], candidate["checksum"], candidate["size"])
            if candidate["checksum"] in landed:
                identity.record_sighting(
                    connection,
                    item,
                    source_system=CARDNET,
                    ingest_date=day,
                    outcome=identity.ALREADY_INGESTED,
                    batch_id=landed[candidate["checksum"]],
                    run_id=run_id,
                    now=opened_at,
                )
                continue
            if not candidate["settlement_date"]:
                business = day
            else:
                business = dt.date.fromisoformat(candidate["settlement_date"])
            batches = [
                _allocate(
                    connection,
                    chains,
                    entity,
                    interval_start=_midnight(business),
                    ingest_date=day,
                    watermark_from=None,
                    opened_at=opened_at,
                    run_id=run_id,
                )
                for entity in ("settlements", "settlement_totals")
            ]
            identity.record_sighting(
                connection,
                item,
                source_system=CARDNET,
                ingest_date=day,
                outcome=identity.NEW,
                batch_id=batches[0]["batch_id"],
                run_id=run_id,
                now=opened_at,
            )
            units.append({"file": candidate, "batches": batches})

        todays = any(
            unit["file"]["settlement_date"] == day.isoformat() for unit in units if unit["file"]
        )
        if not todays and not (sensed or {}).get("found"):
            # No file for this day's settlement date, which is a correct outcome rather than a
            # failure: an empty batch per entity records that the processor was waited for and
            # sent nothing for the day, the way specification 005 records an empty reference
            # batch. A late file for an earlier date arriving today does not fill that gap, and
            # when this day's own file arrives late it lands as the next sequence.
            batches = [
                _allocate(
                    connection,
                    chains,
                    entity,
                    interval_start=start,
                    ingest_date=day,
                    watermark_from=None,
                    opened_at=opened_at,
                    run_id=run_id,
                )
                for entity in ("settlements", "settlement_totals")
            ]
            units.append({"file": None, "batches": batches})
        return units

    units = open_unit(CARDNET, allocate)
    described = [u["file"]["key"] if u["file"] else "no file" for u in units]
    print(f"open: settlements for {day}: {described or 'nothing new'}")
    return units


def settlement_open_units(context: dict, candidates: list[dict]) -> list[dict]:
    sensed = context["ti"].xcom_pull(task_ids="wait_for_file")
    return settlement_open(context, candidates, sensed)


def settlement_extract(unit: dict, context: dict) -> list[dict]:
    from nordbank_ops.extract import ExtractReport
    from nordbank_ops.feeds import clearing, identity
    from nordbank_ops.feeds.land import failed, land
    from nordbank_ops.tokenise import Tokeniser

    by_entity = {b["entity"]: b for b in unit["batches"]}
    detail_batch, totals_batch = by_entity["settlements"], by_entity["settlement_totals"]
    detail_contract = _contract(CARDNET, detail_batch)
    totals_contract = _contract(CARDNET, totals_batch)

    if unit["file"] is None:
        reports = [
            ExtractReport(entity=b["entity"], batch_id=b["batch_id"], status="written")
            for b in (detail_batch, totals_batch)
        ]
        print("extract: no file for this day; empty batches")
        return [r.as_dict() for r in reports]

    file = unit["file"]
    inbound, inbound_bucket = _inbound()
    body = identity.read_object(inbound, inbound_bucket, file["key"])
    if identity.checksum(body) != file["checksum"]:
        reason = f"{file['key']} changed between discovery and reading it"
        return [
            failed(detail_contract, detail_batch, reason).as_dict(),
            failed(totals_contract, totals_batch, reason).as_dict(),
        ]

    tokeniser = Tokeniser.from_environment()
    read = clearing.read(body, detail_contract, totals_contract, tokeniser)
    delivery = {
        "key": file["key"],
        "checksum": file["checksum"],
        "size": file["size"],
        "business_date": read.settlement_date.isoformat() if read.settlement_date else None,
    }
    if read.structural_fault:
        reason = f"structurally malformed file {file['key']}: {read.structural_fault}"
        out = [
            failed(detail_contract, detail_batch, reason).as_dict(),
            failed(totals_contract, totals_batch, reason).as_dict(),
        ]
        print(f"extract: {reason}")
        return out

    client, bucket = _lake()
    source_file = f"{inbound_bucket}/{file['key']}"
    detail = land(
        client=client,
        bucket=bucket,
        contract=detail_contract,
        batch=_resolved(detail_batch),
        parsed=read.details,
        tokeniser=tokeniser,
        source_file=source_file,
    ).as_dict()
    totals = land(
        client=client,
        bucket=bucket,
        contract=totals_contract,
        batch=_resolved(totals_batch),
        parsed=read.totals,
        tokeniser=tokeniser,
        source_file=source_file,
    ).as_dict()
    if detail["status"] == "failed" or totals["status"] == "failed":
        reason = detail.get("failure_reason") or totals.get("failure_reason")
        for report in (detail, totals):
            report["status"], report["failure_reason"] = "failed", reason
    detail["delivery"] = {**delivery, "records_identity": True}
    totals["delivery"] = {**delivery, "records_identity": False}
    print(
        f"extract: {file['key']} for settlement date {delivery['business_date']}: "
        f"details read {detail['rows_read']}, landed {detail['rows_landed']}, quarantined "
        f"{detail['rows_quarantined']}; totals landed {totals['rows_landed']}; "
        f"status {detail['status']}"
    )
    return [detail, totals]


def settlement_identifiers(entry: dict):
    """The card references a registered clearing file carried, read back from the file."""
    from nordbank_ops.feeds import clearing, identity
    from nordbank_ops.tokenise import Tokeniser

    delivery = entry.get("delivery") or {}
    if entry.get("entity") != "settlements" or not delivery:
        return []
    inbound, bucket = _inbound()
    body = identity.read_object(inbound, bucket, delivery["key"])
    if identity.checksum(body) != delivery["checksum"]:
        raise RuntimeError(f"{delivery['key']} changed between landing and registering it")
    batch = {"entity": "settlements", "contract_version": entry["contract_version"]}
    detail = _contract(CARDNET, batch)
    totals = _contract(
        CARDNET, {"entity": "settlement_totals", "contract_version": entry["totals_version"]}
    )
    read = clearing.read(body, detail, totals, Tokeniser.from_environment())
    return [("card_reference", sorted(set(read.card_references)))]


# --- sanctions list ------------------------------------------------------------------------


def sanctions_prefix() -> str:
    return os.environ.get("SANCTIONS_SNAPSHOT_PREFIX", "opensanctions/sanctions/")


def sanctions_discover(context: dict) -> list[dict]:
    from nordbank_ops.feeds import identity, sanctions

    client, bucket = _inbound()
    contract = _chains(SANCTIONS)["entities"][-1]
    index_body = identity.read_object(client, bucket, sanctions_prefix() + "latest/index.json")
    index = sanctions.read_index(index_body, sanctions_prefix(), contract)
    body = identity.read_object(client, bucket, index.entities_key)
    candidate = {
        "key": index.entities_key,
        "checksum": identity.checksum(body),
        "size": len(body),
        "version": index.version,
        "published_at": index.published_at.isoformat(),
    }
    print(f"discover: sanctions version {index.version}, {candidate['checksum']}")
    return [candidate]


def sanctions_open(context: dict, candidates: list[dict]) -> list[dict]:
    from nordbank_ops.feeds import identity

    _start, day = _logical(context)
    run_id = context["dag_run"].run_id
    opened_at = dt.datetime.now(dt.UTC)

    def allocate(connection, chains):
        landed = identity.landed_as(connection, [c["checksum"] for c in candidates])
        units = []
        for candidate in candidates:
            item = identity.Candidate(candidate["key"], candidate["checksum"], candidate["size"])
            if candidate["checksum"] in landed:
                identity.record_sighting(
                    connection,
                    item,
                    source_system=SANCTIONS,
                    ingest_date=day,
                    outcome=identity.ALREADY_INGESTED,
                    batch_id=landed[candidate["checksum"]],
                    run_id=run_id,
                    now=opened_at,
                )
                print(
                    f"open: sanctions version {candidate['version']} has content already landed "
                    f"as {landed[candidate['checksum']]}; nothing to do"
                )
                continue
            published = dt.datetime.fromisoformat(candidate["published_at"])
            batch = _allocate(
                connection,
                chains,
                "entities",
                interval_start=published,
                ingest_date=day,
                watermark_from=None,
                opened_at=opened_at,
                run_id=run_id,
            )
            identity.record_sighting(
                connection,
                item,
                source_system=SANCTIONS,
                ingest_date=day,
                outcome=identity.NEW,
                batch_id=batch["batch_id"],
                run_id=run_id,
                now=opened_at,
            )
            units.append({"snapshot": candidate, "batches": [batch]})
        return units

    return open_unit(SANCTIONS, allocate)


def sanctions_extract(unit: dict, context: dict) -> list[dict]:
    from nordbank_ops.feeds import identity, sanctions
    from nordbank_ops.feeds.land import failed, land
    from nordbank_ops.tokenise import Tokeniser

    (batch,) = unit["batches"]
    snapshot = unit["snapshot"]
    contract = _contract(SANCTIONS, batch)
    inbound, inbound_bucket = _inbound()
    body = identity.read_object(inbound, inbound_bucket, snapshot["key"])
    if identity.checksum(body) != snapshot["checksum"]:
        return [failed(contract, batch, f"{snapshot['key']} changed after discovery").as_dict()]
    index = sanctions.Index(
        version=snapshot["version"],
        published_at=dt.datetime.fromisoformat(snapshot["published_at"]),
        entities_key=snapshot["key"],
    )
    parsed = sanctions.read_entities(body, index, contract)
    client, bucket = _lake()
    report = land(
        client=client,
        bucket=bucket,
        contract=contract,
        batch=_resolved(batch),
        parsed=parsed,
        tokeniser=Tokeniser.from_environment(),
        source_file=f"{inbound_bucket}/{snapshot['key']}",
    ).as_dict()
    report["delivery"] = {
        "key": snapshot["key"],
        "checksum": snapshot["checksum"],
        "size": snapshot["size"],
        "business_date": index.published_at.date().isoformat(),
        "publisher_version": snapshot["version"],
        "records_identity": True,
    }
    print(
        f"extract: sanctions {snapshot['version']}: read {report['rows_read']}, landed "
        f"{report['rows_landed']}, quarantined {report['rows_quarantined']}"
    )
    return [report]


# --- FRED ------------------------------------------------------------------------------------


def macro_open(context: dict) -> list[dict]:
    start, day = _logical(context)
    run_id = context["dag_run"].run_id
    opened_at = dt.datetime.now(dt.UTC)

    def allocate(connection, chains):
        batch = _allocate(
            connection,
            chains,
            "series",
            interval_start=start,
            ingest_date=day,
            watermark_from=None,
            opened_at=opened_at,
            run_id=run_id,
        )
        return [{"batches": [batch]}]

    return open_unit(FRED, allocate)


def macro_extract(unit: dict, context: dict) -> list[dict]:
    from nordbank_ops.feeds import fred
    from nordbank_ops.feeds.land import failed, land
    from nordbank_ops.tokenise import Tokeniser

    (batch,) = unit["batches"]
    contract = _contract(FRED, batch)
    names = fred.series()
    fetched = fred.fetch_series(
        names, contract, api_key=os.environ[fred.KEY_VARIABLE], fetch=_fetcher(_conf(context))
    )
    if fetched.failure:
        report = failed(contract, batch, fetched.failure)
    else:
        client, bucket = _lake()
        report = land(
            client=client,
            bucket=bucket,
            contract=contract,
            batch=_resolved(batch),
            parsed=fetched.parsed,
            tokeniser=Tokeniser.from_environment(),
            source_file=fred.ENDPOINT,
        )
    out = report.as_dict()
    out["requests"] = [o.as_dict() for o in fetched.outcomes]
    return [out]


# --- register and gate ------------------------------------------------------------------------


def flatten_reports(pulled) -> list[dict]:
    """Every report the mapped extract tasks pushed, however Airflow hands them back.

    Each extract task pushes a list of reports. Measured in Airflow 3.3.2, pulling a mapped
    task's XCom returns one value per map index when there are several, and the single pushed
    value itself when there is one, so the same call yields a list of lists or a list of dicts
    depending on how many units the run had.
    """
    if pulled is None:
        return []
    if isinstance(pulled, dict):
        return [pulled]
    out: list[dict] = []
    for item in pulled:
        if isinstance(item, dict):
            out.append(item)
        elif item:
            out.extend(flatten_reports(item))
    return out


def register(context: dict, system: str, identifier_values=None) -> dict:
    from nordbank_ops import clients, warehouse
    from nordbank_ops.feeds.register import register_feed_run
    from nordbank_ops.tokenise import Tokeniser

    _start, day = _logical(context)
    reports = flatten_reports(context["ti"].xcom_pull(task_ids=EXTRACT_TASK_ID, key=REPORT_KEY))
    run_id = context["dag_run"].run_id
    now = dt.datetime.now(dt.UTC)

    with warehouse.connect(read_only=False) as connection:
        reported = {r["batch_id"] for r in reports}
        stranded = connection.execute(
            """
            select batch_id, entity, contract_version from ops.batch_registry
             where source_system = ? and triggering_run_id = ? and status in ('open', 'written')
            """,
            [system, run_id],
        ).fetchall()
        versions = {batch_id: int(version) for batch_id, _entity, version in stranded}
        for batch_id, entity, _version in stranded:
            if batch_id not in reported:
                reports.append(
                    {
                        "entity": entity,
                        "batch_id": batch_id,
                        "status": "failed",
                        "rows_read": 0,
                        "rows_landed": 0,
                        "rows_quarantined": 0,
                        "failure_reason": "the extract task did not report",
                    }
                )
        for report in reports:
            report.setdefault("contract_version", versions.get(report["batch_id"]))
        totals_versions = {
            r["batch_id"].split("-", 1)[1]: r["contract_version"]
            for r in reports
            if r["entity"] == "settlement_totals"
        }
        for report in reports:
            if report["entity"] == "settlements":
                report["totals_version"] = totals_versions.get(report["batch_id"].split("-", 1)[1])

        summary = register_feed_run(
            connection=connection,
            client=clients.lake_client(),
            lake_bucket=clients.lake_bucket(),
            reports=sorted(reports, key=lambda r: r["batch_id"]),
            tokeniser=Tokeniser.from_environment(),
            identifier_values=identifier_values or (lambda _entry: []),
            now=now,
        )
    body = summary.as_dict()
    print(
        f"register: {system} for {day}: {len(summary.registered)} registered, "
        f"{len(summary.failed)} failed, {summary.vault_rows_added} vault row(s) added, "
        f"{summary.quarantine_rows_loaded} quarantine row(s) indexed, "
        f"{summary.files_recorded} file(s) recorded, {summary.requests_recorded} request(s)"
    )
    for entity, reason in summary.failed:
        print(f"register: {entity} failed: {reason}")
    return body


def gate(summary: dict) -> None:
    from nordbank_ops.phases import gate_phase

    gate_phase(summary)
