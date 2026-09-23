#!/bin/sh
# Provision the lake bucket. Idempotent: re-running changes nothing.
#
# There is no bucket versioning here on purpose. Immutability comes from the batch id in the
# object key (ADR 0008), which needs no erasure-coded backend and no extra drives.
set -eu

echo "minio-init: waiting for ${MINIO_ENDPOINT}"
until mc alias set local "${MINIO_ENDPOINT}" "${MINIO_ROOT_USER}" "${MINIO_ROOT_PASSWORD}" >/dev/null 2>&1; do
    sleep 2
done

echo "minio-init: ensuring bucket ${LAKE_BUCKET}"
mc mb --ignore-existing "local/${LAKE_BUCKET}"

# The inbound bucket is where third parties deliver, before anything is ingested. It is a
# separate bucket rather than a prefix of the lake because a delivery carries identifiers in
# the clear, and the lake is the one place that must never hold one (ADR 0005).
echo "minio-init: ensuring bucket ${INBOUND_BUCKET}"
mc mb --ignore-existing "local/${INBOUND_BUCKET}"
mc anonymous set none "local/${INBOUND_BUCKET}"

echo "minio-init: ensuring prefixes"
for prefix in bronze quarantine; do
    if ! mc stat "local/${LAKE_BUCKET}/${prefix}/.keep" >/dev/null 2>&1; then
        printf '' | mc pipe "local/${LAKE_BUCKET}/${prefix}/.keep"
    fi
done

echo "minio-init: denying anonymous access"
mc anonymous set none "local/${LAKE_BUCKET}"

# mc reports a bucket with no anonymous policy as `private`, and older builds say `none`.
# Anything mentioning public, download, upload or write means anonymous access is open.
policy=$(mc anonymous get "local/${LAKE_BUCKET}")
echo "minio-init: ${policy}"
case "${policy}" in
    *public*|*download*|*upload*|*write*)
        echo "minio-init: anonymous access is not denied: ${policy}" >&2
        exit 1
        ;;
    *none*|*private*) ;;
    *)
        echo "minio-init: unrecognised anonymous policy: ${policy}" >&2
        exit 1
        ;;
esac

mc ls --recursive "local/${LAKE_BUCKET}"
echo "minio-init: done"
