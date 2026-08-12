"""Explicit, prefix-scoped smoke test for the existing AWS S3 bucket."""

from __future__ import annotations

import os
import uuid

import boto3
import pytest

pytestmark = pytest.mark.real_s3


def test_real_s3_round_trip_is_confined_to_supplied_prefix() -> None:
    if os.getenv("RUN_REAL_S3_TESTS", "").lower() != "true":
        pytest.skip("real S3 test requires explicit RUN_REAL_S3_TESTS=true")

    bucket = os.environ.get("S3_BUCKET", "")
    region = os.environ.get("AWS_REGION", "")
    prefix = os.environ.get("TEST_S3_PREFIX", "").strip("/")
    assert bucket == "techsara-chatgpt"
    assert region == "us-east-1"
    assert prefix.startswith("integration-tests/")
    assert len(prefix) > len("integration-tests/")

    key = f"{prefix}/{uuid.uuid4()}.txt"
    payload = b"techsara real S3 integration probe"
    client = boto3.client("s3", region_name=region)
    try:
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=payload,
            ContentType="text/plain",
            ServerSideEncryption="AES256",
        )
        head = client.head_object(Bucket=bucket, Key=key)
        assert head["ContentLength"] == len(payload)
        assert client.get_object(Bucket=bucket, Key=key)["Body"].read() == payload
    finally:
        # The exact generated key is the only object this test may remove.
        assert key.startswith(f"{prefix}/")
        client.delete_object(Bucket=bucket, Key=key)
