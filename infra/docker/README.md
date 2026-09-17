# infra/docker

Compose file, service configuration and images for the local stack: Postgres, MinIO, Airflow
and the Streamlit application.

Owns service topology, ports, volumes and health checks. Application configuration is passed
in as environment variables from `.env`; no credentials are baked into an image.

Populated from M1.
