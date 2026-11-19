"""05: Request an extra run of an existing Azure AI Search indexer.

Read the Search endpoint and indexer name from .env/environment and submit an
asynchronous run request. Do not create, reset, or reconfigure the indexer.
An accepted request is not proof of completed enrichment; inspect execution
history and indexed fields separately.
"""

import json
import logging
import os
import sys
from urllib.parse import urlparse

from azure.core.exceptions import AzureError
from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
from azure.search.documents.indexes import SearchIndexerClient
from dotenv import load_dotenv


class InputError(ValueError):
    """A validation message safe for operational output."""


def read_required_setting(name: str) -> str:
    """Read a nonempty setting and reject insecure or credential-bearing endpoint URLs."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise InputError(f"Set the environment variable {name}")
    if not name.endswith("_ENDPOINT"):
        return value

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


def main() -> None:
    """Authenticate, request a run of the named indexer, and report acceptance without waiting."""
    load_dotenv(".env", override=False)
    logging.getLogger("azure").setLevel(logging.CRITICAL)

    search_endpoint = read_required_setting("AZURE_SEARCH_ENDPOINT")
    indexer_name = read_required_setting("AZURE_SEARCH_INDEXER_NAME")

    credential = get_credential()
    with credential, SearchIndexerClient(search_endpoint, credential) as indexer_client:
        indexer_client.run_indexer(indexer_name)

    # Acceptance means the run was requested, not that document processing finished.
    print(
        json.dumps(
            {
                "event": "indexer_run_requested",
                "indexer": indexer_name,
                "status": "accepted",
                "note": "Execution is asynchronous. Check execution history in Azure for completion.",
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except (AzureError, ValueError, OSError) as exc:
        error = {"event": "indexer_run_failed", "error_type": type(exc).__name__}
        if isinstance(exc, InputError):
            error["reason"] = str(exc)
        print(json.dumps(error), file=sys.stderr)
        sys.exit(1)
