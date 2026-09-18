# 0006 — LocalExecutor

Status: Accepted
Date: 2026-09-18

## Context

The platform runs on one machine, and the warehouse it writes to accepts one process at a
time (ADR 0002). The orchestration layer therefore has to schedule work whose bottleneck is a
single file, on hardware with four to eight cores, on a laptop that also runs an IDE and a
browser.

Airflow offers three realistic executors: LocalExecutor, which runs tasks as subprocesses of
the scheduler; CeleryExecutor, which distributes tasks to worker processes over a broker; and
KubernetesExecutor, which runs each task in its own pod.

## Decision

LocalExecutor. Tasks run as subprocesses of the scheduler, `parallelism` and
`max_active_tasks_per_dag` are set to values that leave the machine usable, and the stack has
no broker, no worker service and no result backend.

The service topology is the Airflow 3 set minus the Celery parts: API server, scheduler, DAG
processor, triggerer, plus a one-shot initialiser.

## Consequences

The repository does not demonstrate distributed execution. Anyone reading it to see how a
platform scales across workers will not find that here, and that is a real gap in a portfolio
project, mitigated only by this record saying so.

Task capacity is bounded by one machine, and tasks compete with everything else running on it.

Tasks are children of the scheduler process, so restarting the scheduler kills whatever is
running. There is no worker that survives a scheduler restart, which makes a redeploy during a
long transformation destructive in a way it would not be under Celery.

Against that, the stack is four Airflow containers instead of six or seven, it starts in under
a minute, and it fits inside 6 GB of Docker memory. On a platform whose warehouse serialises
every task anyway, distributing those tasks across workers would buy queueing, not throughput.

## Alternatives considered

**CeleryExecutor.** Rejected because it adds Redis and at least one worker service to a
deployment that has exactly one machine to schedule onto, and because the warehouse is a
single-access file: the extra workers would spend their time waiting on the `warehouse_access`
pool. It would demonstrate a distributed topology, which is the only thing it would buy here,
and it would cost about 1 GB of additional memory and two more failure modes to explain in the
runbook.

**KubernetesExecutor.** Rejected because it requires a cluster to be meaningful, and running
one locally to schedule tasks that contend for a file on a shared volume adds an orchestration
layer that the project would then have to maintain and document for no analytical benefit.
