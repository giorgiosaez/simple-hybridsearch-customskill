"""02: Create or update the Azure AI Search index defined in this script.

The script follows four steps:
1. Read settings from .env, keeping any values already set in the environment.
2. Make a small, billable embedding request to learn the vector length.
3. Build the fields, vector-search settings, and semantic-ranking settings.
4. Submit that definition to Azure to create or update the index.

This script does not fetch or compare existing settings, upload documents, or
delete an index. Azure rejects schema changes it cannot apply in place.
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
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    HnswAlgorithmConfiguration,
    HnswParameters,
    SearchableField,
    SearchField,
    SearchFieldDataType,
    SearchIndex,
    SemanticConfiguration,
    SemanticField,
    SemanticPrioritizedFields,
    SemanticSearch,
    SimpleField,
    VectorSearch,
    VectorSearchProfile,
)
from dotenv import load_dotenv
from openai import APIError, OpenAI

VECTOR_PROFILE_NAME = "rag-hnsw-profile"
VECTOR_ALGORITHM_NAME = "rag-hnsw"
SEMANTIC_CONFIGURATION_NAME = "rag-semantic"


class InputError(ValueError):
    """A validation message safe to print without revealing request bodies."""


def read_required_setting(name: str) -> str:
    """Read a required setting without including its value in error messages.

    Endpoints must use HTTPS and cannot contain credentials, queries, or
    fragments. Accept either an OpenAI resource root or its /openai/v1 URL,
    returning the root so main() can add the API path exactly once.
    """
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


def probe_embedding_size(
    client: OpenAI, embedding_deployment: str, configured_embedding_size: int | None = None
) -> int:
    """Make a small embedding request and return the validated vector length.

    An embedding is a list of numbers; its size is the number of values.
    configured_embedding_size is optional: None uses the model's default size.
    If a size is configured, require the service to return that many values.
    This is a billable model call, not a local guess based on a deployment name.
    """
    if configured_embedding_size is not None and configured_embedding_size <= 0:
        raise InputError("AZURE_OPENAI_EMBEDDING_DIMENSIONS must be positive")

    # Omit dimensions unless requested: some embedding models do not support it.
    if configured_embedding_size is None:
        response = client.embeddings.create(
            model=embedding_deployment, input=["Embedding dimension check."]
        )
    else:
        response = client.embeddings.create(
            model=embedding_deployment,
            input=["Embedding dimension check."],
            dimensions=configured_embedding_size,
        )
    if len(response.data) != 1 or response.data[0].index != 0:
        raise InputError("The embedding service returned an unexpected response shape")

    vector = response.data[0].embedding
    if not vector:
        raise InputError("The embedding service returned an empty vector")
    if not all(math.isfinite(value) for value in vector):
        raise InputError("The embedding vector contains non-finite numbers")
    if not any(vector):
        raise InputError("The embedding vector contains only zeros")

    actual_embedding_size = len(vector)
    if (
        configured_embedding_size is not None
        and actual_embedding_size != configured_embedding_size
    ):
        raise InputError("The embedding service returned a dimension mismatch")
    return actual_embedding_size


def build_index(index_name: str, embedding_size: int) -> SearchIndex:
    """Describe one search document per chunk, without making Azure calls.

    Each chunk stores identifiers, text, page metadata, and two embedding vectors.
    HNSW finds nearby vectors using cosine similarity; semantic ranking uses
    chunk_content to rerank text matches.
    """
    if embedding_size <= 0:
        raise InputError("Vector dimensions must be positive")

    # Field definitions are like database columns, not actual document values.
    fields = [
        # Identifiers and document position
        SearchableField(
            name="chunk_id",
            type=SearchFieldDataType.String,
            key=True,
            filterable=True,
            analyzer_name="keyword",
        ),
        SimpleField(name="parent_id", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="tenant_id", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="file_id", type=SearchFieldDataType.String, filterable=True),
        SimpleField(
            name="page_number", type=SearchFieldDataType.Int32, filterable=True, sortable=True
        ),
        SimpleField(name="chunk_number", type=SearchFieldDataType.Int32, sortable=True),
        # Text and language metadata
        SearchableField(name="chunk_content", type=SearchFieldDataType.String),
        SearchableField(name="page_summary", type=SearchFieldDataType.String),
        SimpleField(name="page_content", type=SearchFieldDataType.String),
        SimpleField(
            name="languages",
            type=SearchFieldDataType.Collection(SearchFieldDataType.String),
            filterable=True,
            facetable=True,
        ),
    ]

    # Embedding vectors
    for vector_name in ("embeddings", "page_embeddings"):
        fields.append(
            SearchField(
                name=vector_name,
                type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
                searchable=True,
                filterable=False,
                sortable=False,
                facetable=False,
                hidden=True,
                vector_search_dimensions=embedding_size,
                vector_search_profile_name=VECTOR_PROFILE_NAME,
            )
        )

    vector_search = VectorSearch(
        algorithms=[
            HnswAlgorithmConfiguration(
                name=VECTOR_ALGORITHM_NAME, parameters=HnswParameters(metric="cosine")
            )
        ],
        profiles=[
            VectorSearchProfile(
                name=VECTOR_PROFILE_NAME, algorithm_configuration_name=VECTOR_ALGORITHM_NAME
            )
        ],
    )
    semantic_search = SemanticSearch(
        default_configuration_name=SEMANTIC_CONFIGURATION_NAME,
        configurations=[
            SemanticConfiguration(
                name=SEMANTIC_CONFIGURATION_NAME,
                prioritized_fields=SemanticPrioritizedFields(
                    content_fields=[SemanticField(field_name="chunk_content")]
                ),
            )
        ],
    )
    return SearchIndex(
        name=index_name,
        fields=fields,
        vector_search=vector_search,
        semantic_search=semantic_search,
    )


def main() -> None:
    """Load settings, validate the embedding size, update the index, and print its status."""
    load_dotenv(".env", override=False)

    # Avoid logging request details; the entry point reports sanitized errors.
    for logger_name in ("azure", "openai", "httpx", "httpcore"):
        logging.getLogger(logger_name).setLevel(logging.CRITICAL)
    search_endpoint = read_required_setting("AZURE_SEARCH_ENDPOINT")
    index_name = read_required_setting("AZURE_SEARCH_INDEX")
    openai_endpoint = read_required_setting("AZURE_OPENAI_ENDPOINT")
    embedding_deployment = read_required_setting("AZURE_OPENAI_EMBEDDING_DEPLOYMENT")

    size_setting = os.environ.get("AZURE_OPENAI_EMBEDDING_DIMENSIONS")
    # None lets the model choose its default number of values per embedding.
    configured_embedding_size = None
    try:
        if size_setting is not None:
            configured_embedding_size = int(size_setting)
    except ValueError:
        raise InputError("AZURE_OPENAI_EMBEDDING_DIMENSIONS must be a positive integer") from None

    credential = get_credential()
    with (
        credential,
        OpenAI(
            base_url=f"{openai_endpoint}/openai/v1/",
            api_key=get_bearer_token_provider(credential, "https://ai.azure.com/.default"),
        ) as openai_client,
        SearchIndexClient(search_endpoint, credential, logging_enable=False) as search_client,
    ):
        actual_embedding_size = probe_embedding_size(
            openai_client, embedding_deployment, configured_embedding_size
        )
        index = build_index(index_name, actual_embedding_size)
        search_client.create_or_update_index(index)

    print(
        json.dumps(
            {"event": "index_ready", "index": index_name, "dimensions": actual_embedding_size}
        )
    )


if __name__ == "__main__":
    try:
        main()
    except (AzureError, APIError, ValueError, OSError) as exc:
        error = {"event": "index_failed", "error_type": type(exc).__name__}
        if isinstance(exc, InputError):
            error["reason"] = str(exc)
        print(json.dumps(error), file=sys.stderr)
        sys.exit(1)
