"""05: Request an extra run of an existing Azure AI Search indexer.

Read the Search endpoint and indexer name from .env/environment and submit an
asynchronous run request by default. This default does not reset or reconfigure the indexer.
An accepted request is not proof of completed enrichment; inspect execution
history and indexed fields separately.

Use --pause to disable scheduling and wait for an active run to finish before
deployment. Use --wait to request a new run and require successful completion.
Neither option resets change tracking or deletes indexed records.
"""

import argparse
import json
import logging
import os
import sys
import time
from urllib.parse import urlparse

from azure.core import MatchConditions
from azure.core.exceptions import AzureError, ResourceNotFoundError
from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
from azure.search.documents.indexes import SearchIndexerClient
from azure.search.documents.indexes.models import IndexerExecutionResult
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


def check_completed_run(result: IndexerExecutionResult) -> None:
    """Reject incomplete/failed runs and warnings without printing potentially sensitive details."""
    if result.status != "success":
        raise InputError("Indexer run did not succeed; inspect execution history in Azure")
    if result.failed_item_count or result.errors or result.warnings:
        raise InputError(
            "Indexer completed with failed items, errors, or warnings; "
            "review execution history before searching"
        )


def wait_until_idle(
    client: SearchIndexerClient,
    indexer_name: str,
    deadline: float,
    poll_seconds: int,
    *,
    check_active_run: bool = False,
) -> None:
    """Wait for active work to stop; disabling a schedule does not cancel a running batch."""
    observed_active_run = False
    while True:
        status = client.get_indexer_status(indexer_name)
        if status.status == "error":
            raise InputError("Indexer is in an error state; inspect execution history in Azure")
        if status.last_result is None or status.last_result.status != "inProgress":
            if observed_active_run and check_active_run:
                if status.last_result is None:
                    raise InputError("Active indexer execution disappeared; inspect Azure history")
                check_completed_run(status.last_result)
            return
        observed_active_run = True
        print(json.dumps({"event": "indexer_waiting", "phase": "active_run"}), flush=True)
        if time.monotonic() >= deadline:
            raise InputError("Timed out waiting for the active indexer run to stop")
        time.sleep(min(poll_seconds, max(0, deadline - time.monotonic())))


def pause_indexer(
    client: SearchIndexerClient, indexer_name: str, deadline: float, poll_seconds: int
) -> None:
    """Disable an existing indexer with an ETag guard, then wait for its active run."""
    try:
        indexer = client.get_indexer(indexer_name)
    except ResourceNotFoundError:
        print(json.dumps({"event": "indexer_not_created", "note": "Nothing to pause."}))
        return
    indexer.is_disabled = True
    client.create_or_update_indexer(indexer, match_condition=MatchConditions.IfNotModified)
    print(json.dumps({"event": "indexer_disabled", "note": "Waiting for any active run."}))
    wait_until_idle(client, indexer_name, deadline, poll_seconds)
    print(json.dumps({"event": "indexer_paused", "note": "Remains disabled until step 04."}))


def run_and_wait(
    client: SearchIndexerClient, indexer_name: str, deadline: float, poll_seconds: int
) -> None:
    """Wait for existing work, request a new run, and reject stale/failed/warning results."""
    wait_until_idle(client, indexer_name, deadline, poll_seconds, check_active_run=True)
    before = client.get_indexer_status(indexer_name).last_result
    previous_start = before.start_time if before is not None else None
    if time.monotonic() >= deadline:
        raise InputError("Indexer wait deadline expired before a new run could be requested")
    client.run_indexer(indexer_name)
    print(json.dumps({"event": "indexer_run_requested", "status": "accepted"}), flush=True)
    while True:
        status = client.get_indexer_status(indexer_name)
        if status.status == "error":
            raise InputError("Indexer is in an error state; inspect execution history in Azure")
        result = status.last_result
        if (
            result is not None
            and result.start_time is not None
            and (previous_start is None or result.start_time > previous_start)
        ):
            if result.status == "success":
                check_completed_run(result)
                print(json.dumps({"event": "indexer_completed", "items_processed": result.item_count}))
                if result.item_count == 0:
                    print(json.dumps({
                        "event": "indexer_no_new_items",
                        "note": "Change tracking found no new items; verify the intended Blob separately.",
                    }))
                return
            if result.status != "inProgress":
                raise InputError("New indexer run did not succeed; inspect execution history in Azure")
        print(json.dumps({"event": "indexer_waiting", "phase": "new_run"}), flush=True)
        if time.monotonic() >= deadline:
            raise InputError("Timed out waiting for a newly requested indexer run to complete")
        time.sleep(min(poll_seconds, max(0, deadline - time.monotonic())))


def main(*, pause: bool = False, wait: bool = False, timeout_seconds: int = 1800) -> None:
    """Request a run, or explicitly pause/wait, while preserving the original default behavior."""
    load_dotenv(".env", override=False)
    logging.getLogger("azure").setLevel(logging.CRITICAL)

    search_endpoint = read_required_setting("AZURE_SEARCH_ENDPOINT")
    indexer_name = read_required_setting("AZURE_SEARCH_INDEXER_NAME")
    if timeout_seconds <= 0 or (pause and wait):
        raise InputError("Use a positive timeout and choose only one of --pause or --wait")

    credential = get_credential()
    with credential, SearchIndexerClient(search_endpoint, credential) as indexer_client:
        deadline = time.monotonic() + timeout_seconds
        if pause:
            pause_indexer(indexer_client, indexer_name, deadline, 10)
            return
        if wait:
            run_and_wait(indexer_client, indexer_name, deadline, 10)
            return
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
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--pause", action="store_true", help="Disable the indexer and wait until idle.")
    mode.add_argument("--wait", action="store_true", help="Request a new run and wait for success.")
    parser.add_argument("--timeout-seconds", type=int, default=1800, help="Wait deadline (default: 1800).")
    options = parser.parse_args()
    try:
        main(**vars(options))
    except (AzureError, ValueError, OSError) as exc:
        error = {"event": "indexer_run_failed", "error_type": type(exc).__name__}
        if isinstance(exc, InputError):
            error["reason"] = str(exc)
        print(json.dumps(error), file=sys.stderr)
        sys.exit(1)
