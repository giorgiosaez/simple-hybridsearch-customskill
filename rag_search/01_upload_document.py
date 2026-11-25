"""01: Upload an approved local document to the accepted Blob container.

Read settings from .env/environment, generate a stable file_id, and upload the
document with tenant_id and file_id metadata. Existing Blob content and metadata
are replaced. Only run this against the intended tenant's Blob name.

This script does not create containers, validate document content, or run Search.
An enabled indexer can discover the upload automatically.
"""

import hashlib
import json
import logging
import mimetypes
import os
import re
import sys
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from azure.core.exceptions import AzureError
from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
from azure.storage.blob import BlobServiceClient, ContentSettings
from dotenv import load_dotenv


class InputError(ValueError):
    """A validation message safe to print without revealing paths or metadata values."""


def read_required_setting(name: str) -> str:
    """Read a nonempty setting and validate service URLs without printing their values."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise InputError(f"Set the environment variable {name}")
    if name.endswith("_ENDPOINT") or name == "AZURE_STORAGE_ACCOUNT_URL":
        parsed = urlparse(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise InputError(f"{name} must be an HTTPS endpoint without credentials or a query")
    return value


def get_credential() -> DefaultAzureCredential | ManagedIdentityCredential:
    """Select local/default or managed-identity authentication from AZURE_AUTH_MODE."""
    mode = os.environ.get("AZURE_AUTH_MODE", "default")
    if mode == "managed":
        return ManagedIdentityCredential(client_id=os.environ.get("AZURE_CLIENT_ID"))
    if mode != "default":
        raise InputError("AZURE_AUTH_MODE must be 'default' or 'managed'")
    return DefaultAzureCredential()


def make_file_id(blob_name: str, tenant_id: str) -> str:
    """Generate the filename-plus-hash ID that downstream ingestion reads from metadata.

    The ID stays the same when content is overwritten at the same Blob name for
    the same tenant. Moving the Blob or changing its tenant produces a new ID.
    """
    if not blob_name.strip() or not tenant_id.strip():
        raise InputError("Blob name and trusted tenant ID are required")
    suffix = hashlib.sha256(
        json.dumps([tenant_id, blob_name], ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:12]
    return f"{PurePosixPath(blob_name).name}-{suffix}"


def build_blob_metadata(
    blob_name: str, tenant_id: str, tenant_metadata_field: str
) -> dict[str, str]:
    """Build identifier metadata, including the tenant key expected by script 04.

    Blob metadata travels in HTTP headers, so values must be ASCII without
    control characters. Use an ASCII Blob filename if the local filename is not.
    """
    if not blob_name.strip() or any(ord(char) < 32 or ord(char) == 127 for char in blob_name):
        raise InputError("RAG_BLOB_NAME must be nonempty and contain no control characters")
    if (
        not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", tenant_metadata_field)
        or tenant_metadata_field.lower().startswith("metadata_storage_")
        or tenant_metadata_field.lower()
        in (
            "content",
            "chunks",
            "pages",
            "identified_content",
            "file_data",
            "file_id",
        )
    ):
        raise InputError("Choose a valid, nonconflicting tenant metadata field name")

    metadata = {
        "tenant_id": tenant_id,
        "file_id": make_file_id(blob_name, tenant_id),
        tenant_metadata_field: tenant_id,
    }
    for value in metadata.values():
        if (
            not value.strip()
            or not value.isascii()
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            raise InputError(
                "Tenant ID and generated file ID must be nonempty ASCII without control characters"
            )
    return metadata


def main() -> None:
    """Validate settings and file size, overwrite the Blob with metadata, and report success."""
    load_dotenv(".env", override=False)
    logging.getLogger("azure").setLevel(logging.CRITICAL)

    account_url = read_required_setting("AZURE_STORAGE_ACCOUNT_URL")
    container_name = read_required_setting("AZURE_STORAGE_ACCEPTED_CONTAINER")
    local_file = Path(read_required_setting("RAG_LOCAL_FILE_PATH"))
    blob_name = read_required_setting("RAG_BLOB_NAME")
    tenant_id = read_required_setting("RAG_TENANT_ID")
    tenant_metadata_field = read_required_setting("AZURE_SEARCH_TENANT_METADATA_FIELD")
    metadata = build_blob_metadata(blob_name, tenant_id, tenant_metadata_field)

    try:
        max_bytes = int(os.environ.get("RAG_MAX_BLOB_BYTES", "26214400"))
    except ValueError:
        raise InputError("RAG_MAX_BLOB_BYTES must be a positive integer") from None
    if max_bytes <= 0:
        raise InputError("RAG_MAX_BLOB_BYTES must be a positive integer")

    with local_file.open("rb") as document:
        file_size = os.fstat(document.fileno()).st_size
        if file_size == 0:
            raise InputError("The local document must not be empty")
        if file_size > max_bytes:
            raise InputError("The local document exceeds RAG_MAX_BLOB_BYTES")

        content_type = mimetypes.guess_type(blob_name)[0] or "application/octet-stream"
        credential = get_credential()
        with (
            credential,
            BlobServiceClient(account_url, credential, logging_enable=False) as storage_client,
            storage_client.get_blob_client(container_name, blob_name) as blob_client,
        ):
            blob_client.upload_blob(
                document,
                length=file_size,
                overwrite=True,
                metadata=metadata,
                content_settings=ContentSettings(content_type=content_type),
                logging_enable=False,
            )

    # Do not print local paths, Blob names, tenant IDs, or generated file IDs.
    print(json.dumps({"event": "blob_uploaded", "bytes": file_size}))


if __name__ == "__main__":
    try:
        main()
    except (AzureError, ValueError, OSError) as exc:
        error = {"event": "blob_upload_failed", "error_type": type(exc).__name__}
        if isinstance(exc, InputError):
            error["reason"] = str(exc)
        print(json.dumps(error), file=sys.stderr)
        sys.exit(1)
