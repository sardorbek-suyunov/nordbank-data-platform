"""Provision the lake bucket. Idempotent: re-running changes nothing.

Runs in the project image, with the S3 client the platform already uses, so the stack pulls
no image for provisioning alone (ADR 0017).

There is no bucket versioning here on purpose. Immutability comes from the batch id in the
object key (ADR 0008), which needs no erasure-coded backend and no extra drives.

`python init.py probe BUCKET` lists the bucket's top-level entries instead, for
`make health`.
"""

from __future__ import annotations

import os
import sys
import time

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

WAIT_SECONDS = 120
PREFIXES = ("bronze", "quarantine")


def log(message: str) -> None:
    print(f"minio-init: {message}", flush=True)


def client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["MINIO_ENDPOINT"],
        aws_access_key_id=os.environ["MINIO_ROOT_USER"],
        aws_secret_access_key=os.environ["MINIO_ROOT_PASSWORD"],
        region_name="us-east-1",
        config=Config(retries={"max_attempts": 1}, connect_timeout=5, read_timeout=10),
    )


def wait_for(s3) -> None:
    log(f"waiting for {os.environ['MINIO_ENDPOINT']}")
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        try:
            s3.list_buckets()
            return
        except (BotoCoreError, ClientError) as error:
            if time.monotonic() > deadline:
                raise SystemExit(
                    f"minio-init: not reachable after {WAIT_SECONDS}s: {error}"
                ) from error
            time.sleep(2)


def ensure_bucket(s3, bucket: str) -> None:
    log(f"ensuring bucket {bucket}")
    try:
        s3.create_bucket(Bucket=bucket)
    except ClientError as error:
        if error.response["Error"]["Code"] != "BucketAlreadyOwnedByYou":
            raise


def deny_anonymous(s3, bucket: str) -> None:
    """Remove any bucket policy, then prove none is left.

    A bucket with no policy grants nothing to an anonymous caller, which is what `mc anonymous
    set none` produced. The check does not interpret a policy's statements: any policy that
    survives the deletion is a failure, so a grant cannot hide in a statement the check did not
    anticipate.
    """
    log(f"denying anonymous access to {bucket}")
    s3.delete_bucket_policy(Bucket=bucket)
    try:
        policy = s3.get_bucket_policy(Bucket=bucket)["Policy"]
    except ClientError as error:
        if error.response["Error"]["Code"] == "NoSuchBucketPolicy":
            log(f"{bucket}: no bucket policy, anonymous access denied")
            return
        raise
    raise SystemExit(f"minio-init: {bucket} still has a bucket policy: {policy}")


def ensure_prefixes(s3, bucket: str) -> None:
    log("ensuring prefixes")
    for prefix in PREFIXES:
        key = f"{prefix}/.keep"
        try:
            s3.head_object(Bucket=bucket, Key=key)
        except ClientError as error:
            if error.response["Error"]["Code"] not in ("404", "NoSuchKey", "NotFound"):
                raise
            s3.put_object(Bucket=bucket, Key=key, Body=b"")


def top_level(s3, bucket: str) -> list[str]:
    page = s3.list_objects_v2(Bucket=bucket, Delimiter="/")
    prefixes = [entry["Prefix"] for entry in page.get("CommonPrefixes", [])]
    objects = [entry["Key"] for entry in page.get("Contents", [])]
    return prefixes + objects


def provision() -> int:
    s3 = client()
    wait_for(s3)
    lake = os.environ["LAKE_BUCKET"]
    ensure_bucket(s3, lake)
    ensure_prefixes(s3, lake)
    deny_anonymous(s3, lake)
    for entry in top_level(s3, lake):
        log(f"{lake}/{entry}")
    log("done")
    return 0


def probe(bucket: str) -> int:
    try:
        entries = top_level(client(), bucket)
    except (BotoCoreError, ClientError) as error:
        print(f"{bucket}: {error}", file=sys.stderr)
        return 1
    for entry in entries:
        print(entry)
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "probe":
        raise SystemExit(probe(sys.argv[2]))
    raise SystemExit(provision())
