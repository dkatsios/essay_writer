"""Quick R2 connectivity check."""

import os

os.environ.pop("SSL_CERT_FILE", None)

from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(".env"), override=True)
os.environ.pop("SSL_CERT_FILE", None)

import boto3
from botocore.config import Config as BotoConfig

client = boto3.client(
    "s3",
    endpoint_url=os.environ["ESSAY_WRITER_STORAGE__R2_ENDPOINT_URL"],
    aws_access_key_id=os.environ["ESSAY_WRITER_STORAGE__R2_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["ESSAY_WRITER_STORAGE__R2_SECRET_ACCESS_KEY"],
    region_name="auto",
    verify=False,
    config=BotoConfig(
        retries={"max_attempts": 1, "mode": "standard"},
        signature_version="s3v4",
        s3={"addressing_style": "path"},
        connect_timeout=10,
        read_timeout=10,
    ),
)
bucket = os.environ["ESSAY_WRITER_STORAGE__R2_BUCKET"]

print(f"Endpoint: {os.environ['ESSAY_WRITER_STORAGE__R2_ENDPOINT_URL']}")
print(f"Bucket: {bucket}")

try:
    # Test: list objects
    resp = client.list_objects_v2(Bucket=bucket, MaxKeys=1)
    print(f"SUCCESS - list_objects_v2 returned KeyCount={resp.get('KeyCount', 0)}")

    # Test: put + get + delete
    test_key = "_test/connectivity_check.txt"
    client.put_object(Bucket=bucket, Key=test_key, Body=b"hello r2")
    got = client.get_object(Bucket=bucket, Key=test_key)
    body = got["Body"].read()
    assert body == b"hello r2", f"Content mismatch: {body!r}"
    client.delete_object(Bucket=bucket, Key=test_key)
    print("SUCCESS - put/get/delete cycle passed")

except Exception:
    import traceback

    traceback.print_exc()
