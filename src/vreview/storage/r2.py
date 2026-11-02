from __future__ import annotations

from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


class R2Store:
    """S3-compatible BlobStore for Cloudflare R2 and local MinIO."""

    def __init__(
        self,
        endpoint_url: str,
        access_key_id: str,
        secret_access_key: str,
        bucket: str,
        public_endpoint_url: str | None = None,
        presign_ttl: int = 900,
    ) -> None:
        self.endpoint_url = endpoint_url
        self.access_key_id = access_key_id
        self.secret_access_key = secret_access_key
        self.bucket = bucket
        self.public_endpoint_url = public_endpoint_url
        self.presign_ttl = presign_ttl

        cfg = Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
        )

        self._s3 = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name="auto",
            config=cfg,
        )

        if public_endpoint_url and public_endpoint_url != endpoint_url:
            self._presign_s3 = boto3.client(
                "s3",
                endpoint_url=public_endpoint_url,
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_access_key,
                region_name="auto",
                config=cfg,
            )
        else:
            self._presign_s3 = self._s3

    def exists(self, key: str) -> bool:
        try:
            self._s3.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def get_file(self, key: str, dest: Path) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            head = self._s3.head_object(Bucket=self.bucket, Key=key)
            remote_size = head.get("ContentLength")
            if dest.exists() and remote_size is not None and dest.stat().st_size == remote_size:
                return dest
            self._s3.download_file(self.bucket, key, str(dest))
            return dest
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                raise FileNotFoundError(f"Key not found in store: {key}") from e
            raise

    def put_file(self, key: str, src: Path, content_type: str | None = None) -> None:
        extra_args: dict[str, Any] = {}
        if content_type:
            extra_args["ContentType"] = content_type
        self._s3.upload_file(
            str(src),
            self.bucket,
            key,
            ExtraArgs=extra_args if extra_args else None,
        )

    def get_bytes(self, key: str) -> bytes:
        try:
            resp = self._s3.get_object(Bucket=self.bucket, Key=key)
            return resp["Body"].read()
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                raise FileNotFoundError(f"Key not found in store: {key}") from e
            raise

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        kwargs: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": key,
            "Body": data,
        }
        if content_type:
            kwargs["ContentType"] = content_type
        self._s3.put_object(**kwargs)

    def copy(self, src_key: str, dst_key: str) -> None:
        try:
            self._s3.copy_object(
                Bucket=self.bucket,
                Key=dst_key,
                CopySource={"Bucket": self.bucket, "Key": src_key},
            )
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                raise FileNotFoundError(f"Key not found in store: {src_key}") from e
            raise

    def delete(self, key: str) -> None:
        self._s3.delete_object(Bucket=self.bucket, Key=key)

    def presign_get(self, key: str, ttl: int | None = None) -> str:
        ttl = ttl or self.presign_ttl
        return self._presign_s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=ttl,
        )

    def create_multipart(self, key: str, content_type: str | None = None) -> str:
        kwargs: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": key,
        }
        if content_type:
            kwargs["ContentType"] = content_type
        resp = self._s3.create_multipart_upload(**kwargs)
        return resp["UploadId"]

    def presign_part(self, key: str, upload_id: str, part_number: int, ttl: int | None = None) -> str:
        ttl = ttl or self.presign_ttl
        return self._presign_s3.generate_presigned_url(
            "upload_part",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "UploadId": upload_id,
                "PartNumber": part_number,
            },
            ExpiresIn=ttl,
        )

    def complete_multipart(self, key: str, upload_id: str, parts: list[dict]) -> None:
        sorted_parts = sorted(parts, key=lambda p: int(p["PartNumber"]))
        self._s3.complete_multipart_upload(
            Bucket=self.bucket,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={"Parts": sorted_parts},
        )

    def abort_multipart(self, key: str, upload_id: str) -> None:
        self._s3.abort_multipart_upload(
            Bucket=self.bucket,
            Key=key,
            UploadId=upload_id,
        )
