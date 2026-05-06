"""Pyarrow S3 filesystem wired for MinIO.

We hit MinIO over plain HTTP inside the docker network — no TLS, no AWS
auth chain. Using path-style addressing (`scheme=http`, region="us-east-1")
keeps MinIO happy without needing virtual-hosted bucket subdomains.
"""

from __future__ import annotations

from typing import Any

from pyarrow import fs


def make_s3_filesystem(
    endpoint: str,
    access_key: str,
    secret_key: str,
    allow_bucket_creation: bool = True,
) -> Any:  # pyarrow.fs.S3FileSystem — pyarrow.fs is dynamically populated
    """Build a pyarrow S3FileSystem talking to MinIO.

    `allow_bucket_creation=True` lets `create_dir(bucket)` work on first boot;
    pyarrow rejects bucket-create at the filesystem level otherwise.
    """
    # pyarrow.fs.S3FileSystem is dynamically populated; mypy doesn't see it.
    return fs.S3FileSystem(  # type: ignore[attr-defined]
        endpoint_override=endpoint,
        access_key=access_key,
        secret_key=secret_key,
        scheme="http",
        region="us-east-1",
        background_writes=True,
        allow_bucket_creation=allow_bucket_creation,
    )


def hive_path(bucket: str, hour_start) -> str:
    """`s3://lab-archive/telemetry/year=2026/month=05/day=05/hour=14/data.parquet`"""
    return (
        f"{bucket}/telemetry"
        f"/year={hour_start.year}"
        f"/month={hour_start.month:02d}"
        f"/day={hour_start.day:02d}"
        f"/hour={hour_start.hour:02d}"
        f"/data.parquet"
    )
