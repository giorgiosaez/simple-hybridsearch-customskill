"""HTTP custom skill that turns one accepted Blob into fully enriched chunks.

Search sends a Blob URL, metadata tenant/file IDs, and model/dimension configuration.
The Function validates both IDs against current Blob metadata, extracts pages, and generates page
summaries and page/chunk embeddings using its managed identity. Search later
adds language labels and projection keys. Script 03 packages the standalone
Python ingestion implementation as ingestion.py for this module to reuse.
"""

import json
import logging
import math
import os
from urllib.parse import unquote, urlparse

import azure.functions as func
import ingestion
from azure.core.exceptions import AzureError
from azure.identity import get_bearer_token_provider
from azure.storage.blob import BlobServiceClient
from openai import APIError, OpenAI
from pypdf.errors import PyPdfError

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)
logger = logging.getLogger(__name__)


def blob_name_from_url(url: str, account_url: str, container: str) -> str:
    """Validate an accepted-container HTTPS URL and decode its Blob name exactly once."""
    source, account = urlparse(url), urlparse(account_url)
    prefix = f"/{container}/"
    if (
        source.scheme != "https"
        or source.netloc.lower() != account.netloc.lower()
        or source.username
        or source.password
        or source.query
        or source.fragment
        or not source.path.startswith(prefix)
    ):
        raise ValueError("Source must be a Blob in the configured accepted container")
    name = unquote(source.path[len(prefix) :])
    if not name or any(ord(char) < 32 for char in name):
        raise ValueError("Invalid Blob name")
    return name


def enrich_record(data: dict[str, object]) -> dict[str, object]:
    """Return enriched chunks for one metadata-validated, version-guarded Blob.

    Reuse physical page extraction and token chunking from ingestion.py. Summarize
    and embed pages/chunks, but leave language detection and technical child keys
    to the downstream Search skills and index projections.
    """
    url = data.get("blob_url")
    expected_tenant_id = data.get("tenant_id")
    expected_file_id = data.get("file_id")
    if (
        not isinstance(url, str)
        or not isinstance(expected_tenant_id, str)
        or not isinstance(expected_file_id, str)
    ):
        raise TypeError("Blob URL and metadata tenant_id/file_id are required")
    ingestion.file_filter(expected_tenant_id, expected_file_id)
    account_url = ingestion.required("AZURE_STORAGE_ACCOUNT_URL")
    container = ingestion.required("AZURE_STORAGE_ACCEPTED_CONTAINER")
    name = blob_name_from_url(url, account_url, container)
    deployment = ingestion.required("AZURE_OPENAI_EMBEDDING_DEPLOYMENT")
    summary = ingestion.required("AZURE_OPENAI_SUMMARY_DEPLOYMENT")
    if data.get("embedding_deployment") != deployment or data.get("summary_deployment") != summary:
        raise ValueError("Function and skillset model deployments do not match")
    requested = data.get("dimensions")
    if isinstance(requested, bool) or not isinstance(requested, int) or requested <= 0:
        raise ValueError("The index must supply positive embedding dimensions")
    reduced = os.environ.get("AZURE_OPENAI_EMBEDDING_DIMENSIONS")
    requested_dimensions = int(reduced) if reduced is not None else None
    if requested_dimensions is not None and requested_dimensions != requested:
        raise ValueError("Function embedding dimensions do not match the target index")
    encoding = os.environ.get("RAG_TOKENIZER_ENCODING", "cl100k_base")
    with (
        ingestion.get_credential() as credential,
        BlobServiceClient(account_url, credential) as storage,
        storage.get_blob_client(container, name) as blob,
        OpenAI(
            base_url=f"{ingestion.required('AZURE_OPENAI_ENDPOINT')}/openai/v1/",
            api_key=get_bearer_token_provider(credential, "https://ai.azure.com/.default"),
            timeout=30,
            max_retries=0,
        ) as model,
    ):
        properties = blob.get_blob_properties()
        metadata_key = ingestion.required("AZURE_SEARCH_TENANT_METADATA_FIELD")
        tenant_id, file_id = ingestion.read_blob_identity(properties.metadata, metadata_key)
        if tenant_id != expected_tenant_id:
            raise ValueError("Trusted tenant metadata is missing, changed, or mismatched")
        if file_id != expected_file_id:
            raise ValueError("Trusted file_id metadata is missing, changed, or mismatched")
        raw = ingestion.read_blob(
            blob,
            ingestion.positive_int("RAG_MAX_BLOB_BYTES", 26214400),
            expected_etag=properties.etag,
        )
        pages = ingestion.extract_pages(name, raw)
        if len(pages) > ingestion.positive_int("RAG_MAX_PAGES", 50):
            raise ValueError("Document exceeds the configured page limit")
        chunks = ingestion.chunk_pages(
            pages, ingestion.positive_int("RAG_CHUNK_TOKENS", 512), encoding
        )
        page_enrichments = ingestion.enrich_pages(
            model, pages, deployment, summary, requested, requested_dimensions, encoding
        )
        vectors = ingestion.embed_texts(
            model,
            deployment,
            [chunk.content for chunk in chunks],
            requested,
            requested_dimensions,
        )
    documents = ingestion.map_documents(
        tenant_id, file_id, chunks, vectors, requested, page_enrichments=page_enrichments
    )
    # Search generates child keys and applies its built-in language skill after this response.
    for document in documents:
        del document["chunk_id"]
        del document["languages"]
    if any(not all(math.isfinite(v) for v in vector) for vector in vectors):
        raise ValueError("Invalid embedding")
    return {"chunks": documents}


@app.route(route="enrich", methods=["POST"], trigger_arg_name="request")
def enrich(request: func.HttpRequest) -> func.HttpResponse:
    """Handle a Function-key-protected POST using Azure Search's custom-skill protocol.

    Require exactly one record, preserve its recordId, and return either enriched
    data or sanitized per-record errors. Reject malformed batches with HTTP 400.
    The HTTP binding name must match the request parameter for Azure to load it.
    """
    for name in ("azure", "openai", "httpx", "httpcore", "pypdf"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        payload = request.get_json()
    except ValueError:
        return func.HttpResponse("Expected a JSON custom-skill request", status_code=400)
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("values"), list)
        or len(payload["values"]) != 1
    ):
        return func.HttpResponse("Expected exactly one custom-skill record", status_code=400)
    record = payload["values"][0]
    if (
        not isinstance(record, dict)
        or not isinstance(record.get("recordId"), str)
        or not isinstance(record.get("data"), dict)
    ):
        return func.HttpResponse("Invalid custom-skill record", status_code=400)
    result: dict[str, object] = {"recordId": record["recordId"]}
    try:
        result["data"] = enrich_record(record["data"])
    except (ValueError, TypeError, AzureError, APIError, OSError, PyPdfError) as error:
        logger.error("Custom enrichment failed: %s", type(error).__name__)
        result["errors"] = [{"message": f"Enrichment failed ({type(error).__name__})"}]
    return func.HttpResponse(
        json.dumps({"values": [result]}, allow_nan=False),
        mimetype="application/json",
    )
