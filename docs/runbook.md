# Runbook

Operational procedures for running the platform locally.

## Local setup

Prerequisites: Docker Desktop with at least 6 GB allocated, Python 3.11, uv, GNU make, and Git.
On Windows, run make from Git Bash with make on the PATH; the Makefile sets `SHELL := bash`.

First run, from a clean clone:

```bash
cp .env.example .env
make up            # preflight, build if needed, start, wait for every healthcheck
make health        # per-component probe table
make verify-dag    # trigger ops_stack_healthcheck from the CLI and verify it
```

`make up` refuses to start when the Docker daemon is unreachable or reports less than 6 GB, and
says which it is. Nothing else is required: the bucket, the database roles, the warehouse file,
the admin user and the `warehouse_access` pool are all created by the one-shot init services.

Expect roughly 6 minutes on the very first run, which builds the Airflow image and pulls
everything, then about 40 seconds from a `make nuke`, and about 35 seconds after a `make down`.

### The environment file

`.env` is gitignored and never committed. `.env.example` carries a comment per variable and a
working development Fernet key and JWT secret, so that a plain copy starts; both are published
in this repository and protect nothing, so regenerate them for any environment that matters.

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
| Source database | localhost:5432, database `nordbank` | `SOURCE_READ_USER` reads, `SOURCE_APP_USER` owns | Simulated core banking system |
| Airflow metadata database | localhost:5433, database `airflow` | `POSTGRES_AIRFLOW_USER` | Airflow state; not for platform data |
| Warehouse | `/opt/warehouse/nordbank.duckdb` inside the containers | none, file permissions | DuckDB file on a named volume |

The warehouse is deliberately not reachable from the host: it lives on a named volume, so
probes and tests run inside a container. Connections are environment-defined and therefore
invisible in the UI and to `airflow connections list`; read them back with
`airflow connections get <id>` inside a container.

## Resource envelope

The stack is validated at a documented floor rather than at whatever a developer machine has.
The limits are set in `docker-compose.yml`:

| Service | `mem_limit` |
|---|---|
| postgres-source | 512 MiB |
| postgres-airflow | 512 MiB |
| minio | 1024 MiB |
| airflow-apiserver | 1536 MiB |
| airflow-scheduler | 2048 MiB |
| airflow-dag-processor | 1024 MiB |
| airflow-triggerer | 1024 MiB |
| **Total, long-running** | **7680 MiB, about 7.5 GiB** |

The one-shot services are additional but transient: `minio-init` at 256 MiB and `airflow-init`
at 1 GiB, both of which have exited before the Airflow services finish starting.

Validated on 2026-09-18 against Docker 29.6.2, 32 CPUs, 31.2 GiB available to the daemon,
storage driver overlayfs, with the limits above in force. Measured steady-state usage after a
successful health-check run was about 1.4 GiB in total: apiserver 296 MiB, dag-processor
304 MiB, scheduler 353 MiB, triggerer 321 MiB, minio 74 MiB, postgres-airflow 64 MiB,
postgres-source 32 MiB. The headroom is deliberate: the limits are what a laptop should reserve,
not what the stack uses when idle.

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
| `preflight: Docker reports N GiB ... below the 6 GiB floor` | Docker Desktop is allocated less memory than the stack is validated at | Raise it in Settings, Resources. On WSL2, set `memory=` in `%UserProfile%\.wslconfig` and run `wsl --shutdown` |
| Airflow services restart repeatedly, logs mention the Fernet key | `AIRFLOW_FERNET_KEY` is missing or not a valid 32-byte urlsafe base64 key | Generate one: `python -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"` and put it in `.env`, then `make down && make up` |
| `no space left on device`, or Docker Desktop reports a full disk | The WSL2 virtual disk filled with images, volumes and logs | `docker system prune`, `FORCE=1 make nuke` to drop stack volumes, and compact the WSL2 disk if it stays large |
| `make health` shows `lake-bucket fail`, or a DAG cannot find the bucket | A partial `nuke`, or MinIO started with an empty volume while `minio-init` did not re-run | `docker compose up -d minio-init` re-runs the idempotent provisioning; if that fails, `FORCE=1 make nuke && make up` |
| `make health` shows `warehouse busy` | A task holds the DuckDB file; a writer excludes readers (ADR 0002) | Nothing. It is counted as a pass. `STRICT=1` turns it into a failure, which is what CI uses |
| A task fails with a DuckDB lock conflict | Something outside Airflow held the file, or a task bypassed the pool | Every warehouse task must take the `warehouse_access` pool and go through `nordbank_ops.warehouse.connect`, which retries with backoff |
| `ModuleNotFoundError: No module named 'airflow'` in a container | `AIRFLOW_UID` was changed; the image installs Airflow into user 50000's home | Set `AIRFLOW_UID=50000` in `.env` |
| `import airflow` fails on Windows | Airflow does not support native Windows | Expected, not a defect. `make test-dags` runs those tests inside the project image and says so; `make test` never needs Airflow |
| `make test-integration` fails with "service not running" | The stack is down | `make up` first; the smoke tests run inside `airflow-scheduler` |

## Backfill procedure

Populated at M4, once partitions, batch ids and watermarks are in place.

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
