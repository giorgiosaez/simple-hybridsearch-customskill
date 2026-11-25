"""06: Search the configured index without tenant filtering.

Run keyword, vector, or hybrid retrieval and print source passages, page
page summaries, identifiers, and scores. Vector/hybrid modes embed the query first;
optional semantic ranking applies to keyword/hybrid mode. Results can span
tenants, so this example is for trusted operators, not end-user authorization.
"""

import json
import logging
import math
import os
import sys
from urllib.parse import urlparse

from azure.core.exceptions import AzureError
from azure.identity import (
    DefaultAzureCredential,
    ManagedIdentityCredential,
    get_bearer_token_provider,
)
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.models import VectorizedQuery, VectorQuery
from dotenv import load_dotenv
from openai import APIError, OpenAI


class InputError(ValueError):
    """A validation message safe for operational output."""


def read_required_setting(name: str) -> str:
    """Read a required setting, validate HTTPS endpoints, and normalize OpenAI URLs."""
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

    if name == "AZURE_OPENAI_ENDPOINT":
        if parsed.path.rstrip("/") not in ("", "/openai/v1"):
            raise InputError("Use an OpenAI resource endpoint or its /openai/v1 URL")
        return value.rstrip("/").removesuffix("/openai/v1")
    return value


def get_credential() -> DefaultAzureCredential | ManagedIdentityCredential:
    """Select local/default or managed-identity authentication from AZURE_AUTH_MODE."""
    mode = os.environ.get("AZURE_AUTH_MODE", "default")
    if mode == "managed":
        return ManagedIdentityCredential(client_id=os.environ.get("AZURE_CLIENT_ID"))
    if mode != "default":
        raise InputError("AZURE_AUTH_MODE must be 'default' or 'managed'")
    return DefaultAzureCredential()


def create_query_embedding(
    client: OpenAI,
    embedding_deployment: str,
    query: str,
    index_embedding_size: int,
    configured_embedding_size: int | None = None,
) -> list[float]:
    """Make a billable request to represent the query as a list of numbers.

    The vector must be finite, nonzero, and the same size as the index vectors.
    configured_embedding_size is optional; None uses the model's default size.
    """
    if not query.strip() or index_embedding_size <= 0:
        raise InputError("Query and index dimensions must be nonempty/positive")
    if configured_embedding_size is not None and configured_embedding_size != index_embedding_size:
        raise InputError("Requested embedding dimensions do not match the index")

    # Omit dimensions unless configured: some models do not support this argument.
    if configured_embedding_size is None:
        response = client.embeddings.create(model=embedding_deployment, input=[query])
    else:
        response = client.embeddings.create(
            model=embedding_deployment, input=[query], dimensions=configured_embedding_size
        )
    if len(response.data) != 1 or response.data[0].index != 0:
        raise InputError("Embedding response shape is invalid")
    vector = response.data[0].embedding
    if len(vector) != index_embedding_size:
        raise InputError("Query embedding has a dimension mismatch")
    if not all(math.isfinite(value) for value in vector):
        raise InputError("Query embedding contains non-finite numbers")
    if not any(vector):
        raise InputError("Query embedding contains only zeros")
    return vector


def search_documents(
    client: SearchClient,
    query: str,
    top_k: int = 5,
    *,
    mode: str = "hybrid",
    vector: list[float] | None = None,
    semantic: bool = False,
) -> list[dict[str, object]]:
    """Retrieve up to top_k passages across the index using the selected search mode.

    Keyword mode uses query text; vector mode uses the supplied query embedding;
    hybrid mode uses both. Return stored summaries and scores, not generated
    answers. Do not apply a tenant filter or silently downgrade semantic failures.
    """
    if not query.strip():
        raise InputError("Query must not be empty")
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 1000:
        raise InputError("top_k must be an integer between 1 and 1000")
    if mode not in ("keyword", "vector", "hybrid"):
        raise InputError("RAG_SEARCH_MODE must be keyword, vector, or hybrid")
    if semantic and mode == "vector":
        raise InputError("This example applies semantic ranking to keyword or hybrid queries")
    if mode != "keyword":
        if not vector:
            raise InputError("Vector and hybrid search require a nonempty query embedding")
        if not all(math.isfinite(value) for value in vector):
            raise InputError("Query embedding contains non-finite numbers")
        if not any(vector):
            raise InputError("Query embedding contains only zeros")

    # Keyword uses text, vector uses numbers, and hybrid sends both.
    search_text: str | None = query
    search_fields: list[str] | None = ["chunk_content"]
    if mode == "vector":
        search_text = None
        search_fields = None

    vector_queries: list[VectorQuery] | None = None
    if mode != "keyword" and vector is not None:
        candidate_count = top_k
        if semantic:
            # Semantic ranking reranks vector candidates; provide at least 50.
            candidate_count = max(50, top_k)
        vector_queries = [
            VectorizedQuery(
                vector=vector,
                fields="embeddings",
                k_nearest_neighbors=candidate_count,
            )
        ]

    query_type = "simple"
    semantic_configuration_name = None
    semantic_error_mode = None
    if semantic:
        query_type = "semantic"
        semantic_configuration_name = "rag-semantic"
        semantic_error_mode = "fail"

    results = client.search(
        search_text=search_text,
        search_fields=search_fields,
        vector_queries=vector_queries,
        query_type=query_type,
        semantic_configuration_name=semantic_configuration_name,
        semantic_error_mode=semantic_error_mode,
        select=[
            "chunk_id",
            "chunk_content",
            "file_id",
            "page_number",
            "chunk_number",
            "languages",
            "page_summary",
            "page_content",
        ],
        top=top_k,
    )
    passages: list[dict[str, object]] = []
    for result in results:
        passage: dict[str, object] = {
            "chunk_id": result["chunk_id"],
            "chunk_content": result["chunk_content"],
            "file_id": result["file_id"],
            "page_number": result["page_number"],
            "chunk_number": result["chunk_number"],
            "languages": result.get("languages"),
            "page_summary": result["page_summary"],
            "page_content": result["page_content"],
            "score": result.get("@search.score"),
            "reranker_score": result.get("@search.reranker_score"),
        }
        passages.append(passage)
    return passages


def main() -> None:
    """Load query settings, embed when needed, execute retrieval, and print JSON results."""
    load_dotenv(".env", override=False)
    for logger_name in ("azure", "openai", "httpx", "httpcore"):
        logging.getLogger(logger_name).setLevel(logging.CRITICAL)

    search_endpoint = read_required_setting("AZURE_SEARCH_ENDPOINT")
    index_name = read_required_setting("AZURE_SEARCH_INDEX")
    query = read_required_setting("RAG_QUERY")
    mode = os.environ.get("RAG_SEARCH_MODE", "hybrid")
    if mode not in ("keyword", "vector", "hybrid"):
        raise InputError("RAG_SEARCH_MODE must be keyword, vector, or hybrid")
    size_setting = os.environ.get("AZURE_OPENAI_EMBEDDING_DIMENSIONS")
    configured_embedding_size = None
    try:
        top_k = int(os.environ.get("RAG_TOP_K", "5"))
        if size_setting is not None:
            configured_embedding_size = int(size_setting)
    except ValueError:
        raise InputError("RAG_TOP_K and embedding dimensions must be integers") from None
    semantic_value = os.environ.get("RAG_USE_SEMANTIC", "false").lower()
    if semantic_value not in ("true", "false"):
        raise InputError("RAG_USE_SEMANTIC must be true or false")
    semantic = semantic_value == "true"
    if not 1 <= top_k <= 1000:
        raise InputError("RAG_TOP_K must be between 1 and 1000")
    if semantic and mode == "vector":
        raise InputError("Semantic ranking requires keyword or hybrid mode")

    # Keyword-only search does not need an embedding deployment or an OpenAI client.
    if mode != "keyword":
        openai_endpoint = read_required_setting("AZURE_OPENAI_ENDPOINT")
        embedding_deployment = read_required_setting("AZURE_OPENAI_EMBEDDING_DEPLOYMENT")

    credential = get_credential()
    with credential, SearchClient(search_endpoint, index_name, credential) as search_client:
        query_vector = None
        if mode != "keyword":
            with SearchIndexClient(search_endpoint, credential) as index_client:
                index = index_client.get_index(index_name)
            fields_by_name = {field.name: field for field in index.fields}
            embedding_field = fields_by_name.get("embeddings")
            if embedding_field is None or not embedding_field.vector_search_dimensions:
                raise InputError("Index has no embedding dimensions; run script 02")
            index_embedding_size = embedding_field.vector_search_dimensions

            with OpenAI(
                base_url=f"{openai_endpoint}/openai/v1/",
                api_key=get_bearer_token_provider(credential, "https://ai.azure.com/.default"),
            ) as openai_client:
                query_vector = create_query_embedding(
                    openai_client,
                    embedding_deployment,
                    query,
                    index_embedding_size,
                    configured_embedding_size,
                )

        results = search_documents(
            search_client, query, top_k, mode=mode, vector=query_vector, semantic=semantic
        )

    # Source content is the intended output of this example, not diagnostic telemetry.
    print(json.dumps({"mode": mode, "count": len(results), "results": results}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (AzureError, APIError, ValueError, OSError) as exc:
        error = {"event": "search_failed", "error_type": type(exc).__name__}
        if isinstance(exc, InputError):
            error["reason"] = str(exc)
        print(json.dumps(error), file=sys.stderr)
        sys.exit(1)
