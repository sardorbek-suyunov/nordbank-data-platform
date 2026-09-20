# Runbook

Operational procedures for running the platform locally.

## Local setup

Prerequisites: Docker Desktop with at least 6 GB allocated, Python 3.11, uv, GNU make, and Git.
On Windows, run make from Git Bash with make on the PATH; the Makefile sets `SHELL := bash`.

First run, from a clean clone:

```bash
make up            # generates .env if absent, validates it, builds if needed, waits for health
make health        # per-component probe table
make verify-dag    # trigger ops_stack_healthcheck from the CLI and verify it
```

No `.env` step: `make up` generates one when it is missing and prints every value it created.
`make init-env` does the same on demand and refuses to overwrite an existing file, because those
credentials are the ones a running stack was built with.

`make up` then validates `.env` against the template and refuses to start when a variable is
missing, a `__GENERATE__` sentinel survived, a value carries an inline comment, or the Fernet
key does not decode to 32 bytes. It also refuses when the Docker daemon is unreachable or
reports less than 6 GB, and says which it is.

Nothing else is required: the bucket, the database roles, the warehouse file, the admin user
and the `warehouse_access` pool are all created by the one-shot init services.

Expect roughly 6 minutes on the very first run, which builds the Airflow image and pulls
everything, then about 40 seconds from a `make nuke`, and about 35 seconds after a `make down`.

### The environment file

`.env` is gitignored and never committed. `.env.example` documents variables and contains no
working secret: every credential is the sentinel `__GENERATE__`, which `make init-env` replaces
with a freshly generated value, or `__EXTERNAL__` for a credential somebody else issues.
Generation is stdlib only, including the Fernet key, which is 32 random bytes in urlsafe base64.

The generated `.env` carries values without inline comments. Comments belong in the template: a
value that carried its own explanation into a container is how M1 spent an afternoon on a
signature mismatch.

The setting most likely to need changing per machine is the memory Docker Desktop is allowed
to use.

`AIRFLOW_UID` is the one people are tempted to change and should not. The usual advice, to set
it to `id -u` on Linux, applies to the official compose file because that one bind-mounts
writable directories from the host. Here every writable path is a named volume initialised from
the image, and every bind mount is read-only, so there is nothing to own. Changing the uid makes
the container run as a user whose home is not where Airflow is installed, and the first command
fails with `ModuleNotFoundError: No module named 'airflow'`. CI hit exactly this.

## Service endpoints

| Service | Endpoint | Credentials | Purpose |
|---|---|---|---|
| Airflow UI and API | http://localhost:8080 | `AIRFLOW_ADMIN_USER` and `AIRFLOW_ADMIN_PASSWORD` | Orchestration, DAG runs, task logs |
| Airflow health | http://localhost:8080/api/v2/monitor/health | none, endpoint is public | What `make health` probes |
| MinIO S3 API | http://localhost:9000 | `MINIO_ROOT_USER` and `MINIO_ROOT_PASSWORD` | The lake; `mc` is the admin path |
| MinIO console | http://localhost:9001 | as above | Browsing objects by hand |
| Source database | localhost:55432, database `nordbank` | `SOURCE_READ_USER` reads, `SOURCE_APP_USER` owns | Simulated core banking system |
| Airflow metadata database | localhost:5433, database `airflow` | `POSTGRES_AIRFLOW_USER` | Airflow state; not for platform data |
| Warehouse | `/opt/warehouse/nordbank.duckdb` inside the containers | none, file permissions | DuckDB file on a named volume |

### Why the source database publishes on 55432 and not 5432

`POSTGRES_SOURCE_PORT` defaults to 55432. That is deliberate and it is the only non-standard
port in the stack.

**The published port affects host-side tooling only.** Airflow, the init containers and every
other service reach `postgres-source` by service name on the compose network, on 5432, and
none of them reads this variable. Changing it cannot break anything inside the stack.

**What it defends against is a collision that reports nothing.** A native PostgreSQL
installation on the developer's machine is the single most likely thing to already hold 5432,
and when it does, Docker's published mapping is shadowed silently: `docker compose ps` still
prints `0.0.0.0:5432->5432/tcp`, `make up` succeeds and `make health` passes, because every
probe reaches the container over the compose network. Only host-side tooling notices, and
before M3 there was none that connected by port, so the collision was invisible.

M3's mutation engine connects from the host with a driver (ADR 0012), which is where this
surfaced: it reached a PostgreSQL 18 service instead of the container and failed
authentication for a role that server had never heard of. Two defences, because they cover
different failures. The non-standard default avoids the collision on most machines. The
connection then asserts it reached the Nordbank source anyway, and every failure path names a
port collision as a candidate cause, because a default cannot rule out a machine that happens
to use 55432 for something else.

If you need a different port, change `POSTGRES_SOURCE_PORT` in `.env` and run
`make down && make up`. Only the host side moves.

The warehouse is deliberately not reachable from the host: it lives on a named volume, so
probes and tests run inside a container. Connections are environment-defined and therefore
invisible in the UI and to `airflow connections list`; read them back with
`airflow connections get <id>` inside a container.

## Resource envelope

The stack is validated at a documented floor rather than at whatever a developer machine has.
The limits are set in `docker-compose.yml`:

| Service | `mem_limit` | Measured | Headroom |
|---|---|---|---|
| postgres-source | 512 MiB | 30 MiB | 17x |
| postgres-airflow | 512 MiB | 51 MiB | 10x |
| minio | 512 MiB | 73 MiB | 7.0x |
| airflow-apiserver | 1024 MiB | 253 MiB | 4.1x |
| airflow-scheduler | 2048 MiB | 305 MiB | 6.7x |
| airflow-dag-processor | 512 MiB | 286 MiB | 1.8x |
| airflow-triggerer | 512 MiB | 304 MiB | 1.7x |
| **Total, long-running** | **5632 MiB, 5.5 GiB** | **1302 MiB, 1.27 GiB** | **4.3x** |

The scheduler keeps the largest share deliberately: under LocalExecutor tasks run inside that
process, and dbt and pyarrow arrive at M4. The dag-processor and the triggerer are the tightest
at about 1.7x, which is the pair to watch first when something new is added.

The one-shot services are additional but transient: `minio-init` at 256 MiB and `airflow-init`
at 1 GiB, both of which have exited before the Airflow services finish starting.

Measured on 2026-09-18 against Docker 29.6.2, 32 CPUs, 31.2 GiB available to the daemon, storage
driver overlayfs, with the limits above in force: `make up` reached full health in 36 seconds
and every probe passed.

**The floor is re-measured at every milestone that adds runtime work, and this table is
updated with it.** The next two are M4, which adds dbt and pyarrow to the scheduler, and M6,
which builds the marts. A number carried forward without being measured again is an assumption
wearing a fact's clothes.

## Teardown

```bash
make down              # stop the services, keep every volume
make nuke              # stop and delete the volumes; asks for confirmation
FORCE=1 make nuke      # same, without the prompt, for scripts and CI
```

`make down` preserves the warehouse file, both databases and the lake. `make nuke` removes
them, and the next `make up` rebuilds everything from configuration.

## Common failures

| Symptom | Cause | Fix |
|---|---|---|
| `make up` fails with a port already allocated | Another Postgres, MinIO or Airflow is running on 5432, 5433, 8080, 9000 or 9001 | Change the `*_PORT` value in `.env`, or stop the other service. Only the host side moves; nothing inside the compose network changes |
| A host-side tool reports `authentication failed`, or `not the Nordbank source`, while `make health` passes | Something else holds the published port, so the container's mapping is shadowed. Docker reports the mapping either way and every in-stack probe still passes, because services reach each other over the compose network | `docker compose ps postgres-source` to confirm, then set `POSTGRES_SOURCE_PORT` in `.env` to a free port and `make down && make up`. The default is 55432 for this reason |
| `preflight: Docker reports N GiB ... below the 6 GiB floor` | Docker Desktop is allocated less memory than the stack is validated at | Raise it in Settings, Resources. On WSL2, set `memory=` in `%UserProfile%\.wslconfig` and run `wsl --shutdown` |
| Airflow services restart repeatedly, logs mention the Fernet key | `AIRFLOW_FERNET_KEY` is missing or not a valid 32-byte urlsafe base64 key | Generate one: `python -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"` and put it in `.env`, then `make down && make up` |
| `no space left on device`, or Docker Desktop reports a full disk | The WSL2 virtual disk filled with images, volumes and logs | `docker system prune`, `FORCE=1 make nuke` to drop stack volumes, and compact the WSL2 disk if it stays large |
| `make health` shows `lake-bucket fail`, or a DAG cannot find the bucket | A partial `nuke`, or MinIO started with an empty volume while `minio-init` did not re-run | `docker compose up -d minio-init` re-runs the idempotent provisioning; if that fails, `FORCE=1 make nuke && make up` |
| `make health` shows `warehouse busy` | A task holds the DuckDB file; a writer excludes readers (ADR 0002) | Nothing. It is counted as a pass. `STRICT=1` turns it into a failure, which is what CI uses |
| A task fails with a DuckDB lock conflict | Something outside Airflow held the file, or a task bypassed the pool | Every warehouse task must take the `warehouse_access` pool and go through `nordbank_ops.warehouse.connect`, which retries with backoff |
| `ModuleNotFoundError: No module named 'airflow'` in a container | `AIRFLOW_UID` was changed; the image installs Airflow into user 50000's home | Set `AIRFLOW_UID=50000` in `.env` |
| `import airflow` fails on Windows | Airflow does not support native Windows | Expected, not a defect. `make test-dags` runs those tests inside the project image and says so; `make test` never needs Airflow |
| `make test-integration` fails with "service not running" | The stack is down | `make up` first; the smoke tests run inside `airflow-scheduler` |
| `check-env` reports a variable present in `.env.example` and missing from `.env` | A milestone added an environment variable, and `.env` was generated before it | Add the variable by hand, or regenerate: `rm .env && make init-env`. This is the second time an init-once mechanism has bitten — see the database grant below — and it is the same shape of trap both times |
| `make schema-apply` fails with `permission denied for database nordbank` | The stack was started before the M2 branch, so `infra/docker/postgres-source/init/10_privileges.sh` never granted `create on database` to the application role. An init script runs once, on an empty data volume | `FORCE=1 make nuke && make up`. Recorded in ADR 0009 |
| `make seed` fails with `is not seeded; generator/profiles.yml names a code the reference layer does not have` | A profile parameter names a reference code the seed does not carry | Run `make schema-apply`, which seeds `ref`. If it persists, the parameter is wrong: the message names the table and the code |
| A `make seed` run leaves CSV files under `data/generator/` | The load failed partway, and the spool is kept deliberately so the rows that failed can be looked at | Inspect them, then delete the directory. A successful run removes its own spool unless `--keep-spool` was passed |
| `make seed-manifest CHECK=1` reports a difference after a generator change | Working as intended: the committed `ci` manifest is what makes determinism an enforced invariant | If the change was meant to alter generated values, regenerate with `make seed-manifest` and commit the new manifest alongside the change that caused it |

## Seeding the source database

```bash
make seed              # generate and load, honouring the three environment variables
make seed-verify       # run the fourteen coherence invariants against what was loaded
make seed-manifest     # write the run manifest; CHECK=1 compares the committed one
```

Three environment variables determine the output completely: `NORDBANK_SEED`,
`NORDBANK_ANCHOR_DATE` and `NORDBANK_ENV`. The same three give a byte-identical database.
The anchor defaults to the real current date so local data always looks current; CI pins
it, because the committed `ci` manifest is compared against a regeneration and an anchor
that moved every midnight would fail that comparison daily.

`make seed` empties the sixteen `core` tables first. `ref` and `platform` are seeded by
`make schema-apply` and are not touched.

### The pattern M3 and M4 need

The mutation engine at M3 advances the source by one business day, and the backfill at M4
needs genuine day-by-day change to extract. Loading with an anchor of today leaves
nothing to step forward into: every row is already as current as it can be, and a
watermark extraction has one window to read.

**Load with an anchor in the past, then step forward.**

```bash
NORDBANK_ANCHOR_DATE=2026-06-30 make seed   # history ends three months ago
make tick                                    # from M3: advance one business day
```

Each tick moves `updated_at` on the rows it touches, so an incremental extraction has a
real window of changed rows rather than the whole table or nothing. The loader's output is
a valid starting state for that by construction: every audit timestamp is the row's true
last-change time in simulated history, no row carries the load timestamp, and every
identity sequence is synchronised past the largest key its table holds, so the first row
the mutation engine inserts does not collide.

## Backfill procedure

Populated at M4, once partitions, batch ids and watermarks are in place. The anchor
pattern above is the half of it that exists now.

## Escalation

Populated at M7, together with the data quality gates and the ops observability model.

## Windows and WSL2

Docker Desktop runs the stack inside a WSL2 virtual machine that grows its memory use on demand
and does not return it to Windows promptly. That is normal, and it is why the memory limits
above matter more than the size of the machine: without them, a runaway service can take as much
as the VM is allowed.

Optional local adjustment: cap the VM in `%UserProfile%\.wslconfig`, then run `wsl --shutdown`.

```ini
[wsl2]
memory=10GB
processors=8
```

Ten gigabytes leaves the stack its 7.5 GiB envelope plus room for the runtime, and keeps the VM
from taking half the machine.

One more Windows note. Keep the repository inside the WSL2 filesystem rather than on a mounted
Windows drive if bind-mount I/O feels slow; DAG parsing is the first thing to suffer.

## Image pinning and registry risk

Every image is pinned to an explicit version, and images whose registry history has proven
unstable are pinned by digest as well as tag. MinIO qualifies: its images were withdrawn from
Docker Hub, so the stack pulls `quay.io/minio/minio` and `quay.io/minio/mc` by digest.

If quay.io becomes unavailable, the lake layer is the only thing affected, and the contingency
is to replace it with another S3-compatible server: LocalStack S3 for a drop-in local endpoint,
or SeaweedFS for something closer to production behaviour. Both speak the S3 API the platform
uses, so the change is a compose change and an endpoint change, not a code change.
