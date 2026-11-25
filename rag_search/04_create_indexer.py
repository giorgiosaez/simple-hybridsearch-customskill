"""04: Connect accepted Blobs to the enriched Azure AI Search index.

Search reads Blob data and identity metadata, uses Document Layout to extract
physical pages, embeds and summarizes each page, splits pages into numbered chunks,
embeds and language-tags the chunks, and projects them into the index.
Enable an indexer with a 24-hour schedule and five source documents per batch.
Configuration can start processing, but this script neither resets the indexer
nor waits for ingestion to finish.
"""

import json
import logging
import math
import os
import re
import sys
from datetime import timedelta
from urllib.parse import urlparse

from azure.core.exceptions import AzureError
from azure.identity import (
    DefaultAzureCredential,
    ManagedIdentityCredential,
    get_bearer_token_provider,
)
from azure.search.documents.indexes import SearchIndexClient, SearchIndexerClient
from azure.search.documents.indexes.models import (
    AzureOpenAIEmbeddingSkill,
    CognitiveServicesAccountKey,
    ConditionalSkill,
    DefaultCognitiveServicesAccount,
    DocumentIntelligenceLayoutSkill,
    IndexingParameters,
    IndexingParametersConfiguration,
    IndexingSchedule,
    InputFieldMappingEntry,
    LanguageDetectionSkill,
    OutputFieldMappingEntry,
    SearchIndex,
    SearchIndexer,
    SearchIndexerDataContainer,
    SearchIndexerDataSourceConnection,
    SearchIndexerIndexProjection,
    SearchIndexerIndexProjectionSelector,
    SearchIndexerIndexProjectionsParameters,
    SearchIndexerSkillset,
    ShaperSkill,
    SplitSkill,
    WebApiSkill,
)
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


def validate_tenant_metadata_field(tenant_metadata_field: str) -> None:
    """Reject tenant metadata names that are invalid or collide with enrichment paths."""
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
        raise InputError("Choose a valid custom Blob metadata field name for the tenant ID")


def validate_index_for_projections(index: SearchIndex) -> int:
    """Check fields needed to write enriched chunks and return the index embedding size.

    Projections turn each enriched chunk into a search document. The index must
    already have compatible keys, metadata, text fields, and embedding vectors.
    """
    fields_by_name = {field.name: field for field in index.fields}
    chunk_id_field = fields_by_name.get("chunk_id")
    parent_id_field = fields_by_name.get("parent_id")

    # Search generates these technical keys when it projects chunks into the index.
    if (
        chunk_id_field is None
        or not chunk_id_field.key
        or chunk_id_field.type != "Edm.String"
        or not chunk_id_field.searchable
        or chunk_id_field.analyzer_name != "keyword"
        or parent_id_field is None
        or parent_id_field.type != "Edm.String"
        or not parent_id_field.filterable
        or parent_id_field.key
    ):
        raise InputError(
            "Index projections need a keyword-analyzed chunk_id key and a filterable parent_id. "
            "Run script 02; an incompatible existing index requires a new index and reingestion."
        )
    for field_name in ("tenant_id", "file_id"):
        field = fields_by_name.get(field_name)
        if field is None or field.type != "Edm.String" or not field.filterable:
            raise InputError(
                "The target index must contain filterable tenant_id and file_id strings"
            )
    languages_field = fields_by_name.get("languages")
    if (
        languages_field is None
        or languages_field.type != "Collection(Edm.String)"
        or not languages_field.filterable
    ):
        raise InputError("The target index needs a filterable languages string collection")

    content_field = fields_by_name.get("chunk_content")
    embedding_field = fields_by_name.get("embeddings")
    if (
        content_field is None
        or content_field.type != "Edm.String"
        or not content_field.searchable
        or embedding_field is None
        or not embedding_field.searchable
        or embedding_field.type != "Collection(Edm.Single)"
        or not embedding_field.vector_search_dimensions
        or not embedding_field.vector_search_profile_name
    ):
        raise InputError("The target index is missing searchable chunk content or vector settings")
    for field_name in ("page_number", "chunk_number"):
        number_field = fields_by_name.get(field_name)
        if (
            number_field is None
            or number_field.type != "Edm.Int32"
            or not number_field.filterable
            or not number_field.sortable
        ):
            raise InputError(f"The target index needs a filterable, sortable {field_name} integer")
    summary_field = fields_by_name.get("page_summary")
    if summary_field is None or summary_field.type != "Edm.String" or not summary_field.searchable:
        raise InputError("The target index needs a searchable page_summary string")
    page_content = fields_by_name.get("page_content")
    if page_content is None or page_content.type != "Edm.String":
        raise InputError("The target index needs a page_content string")
    page_embedding = fields_by_name.get("page_embeddings")
    if (
        page_embedding is None
        or page_embedding.type != "Collection(Edm.Single)"
        or not page_embedding.searchable
        or not page_embedding.vector_search_profile_name
        or page_embedding.vector_search_profile_name != embedding_field.vector_search_profile_name
        or page_embedding.vector_search_dimensions != embedding_field.vector_search_dimensions
    ):
        raise InputError(
            "Page/chunk vector dimensions must match and both vectors need search profiles"
        )

    index_embedding_size = embedding_field.vector_search_dimensions
    return index_embedding_size


def probe_embedding_model(
    client: OpenAI,
    embedding_deployment: str,
    index_embedding_size: int,
    configured_embedding_size: int | None = None,
) -> str:
    """Make a billable test request, check the vector, and return the supported model name.

    The index fixes the required embedding size. An optional configured size
    requests that length from the model; None uses the model's default.
    """
    if configured_embedding_size is not None and configured_embedding_size != index_embedding_size:
        raise InputError("Requested embedding dimensions do not match the target index")

    # Some models do not support the dimensions argument, so omit it unless configured.
    if configured_embedding_size is None:
        response = client.embeddings.create(
            model=embedding_deployment, input=["Embedding configuration check."]
        )
    else:
        response = client.embeddings.create(
            model=embedding_deployment,
            input=["Embedding configuration check."],
            dimensions=configured_embedding_size,
        )
    if len(response.data) != 1 or response.data[0].index != 0:
        raise InputError("Embedding response shape is invalid")
    vector = response.data[0].embedding
    if len(vector) != index_embedding_size:
        raise InputError("Embedding has a dimension mismatch with the target index")
    if not all(math.isfinite(value) for value in vector):
        raise InputError("The embedding vector contains non-finite numbers")
    if not any(vector):
        raise InputError("The embedding vector contains only zeros")
    if response.model not in (
        "text-embedding-ada-002",
        "text-embedding-3-small",
        "text-embedding-3-large",
    ):
        raise InputError("The deployment model is not supported by AzureOpenAIEmbeddingSkill")
    return response.model


def build_blob_data_source(
    *,
    data_source_name: str,
    storage_resource_id: str,
    container_name: str,
) -> SearchIndexerDataSourceConnection:
    """Define the Blob container Search reads, without making Azure requests.

    ResourceId authenticates with the Search service's managed identity rather
    than a Storage account key. No deletion-detection policy is configured.
    """
    resource_id = storage_resource_id.rstrip("/")
    if not re.fullmatch(
        r"/subscriptions/[^/;\s]+/resourceGroups/[^/;\s]+/providers/"
        r"Microsoft\.Storage/storageAccounts/[a-z0-9]{3,24}",
        resource_id,
        re.IGNORECASE,
    ):
        raise InputError("AZURE_STORAGE_RESOURCE_ID must identify an Azure Storage account")

    return SearchIndexerDataSourceConnection(
        name=data_source_name,
        type="azureblob",
        connection_string=f"ResourceId={resource_id};",
        container=SearchIndexerDataContainer(name=container_name),
    )


def build_index_projection(
    index_name: str, tenant_metadata_field: str
) -> SearchIndexerIndexProjection:
    """Map enriched chunk values to index fields, including the languages collection.

    Search creates chunk_id and parent_id itself. Only chunks become index
    documents; the parent Blob record is not indexed as a separate document.
    """
    field_mappings = [
        InputFieldMappingEntry(name="tenant_id", source=f"/document/{tenant_metadata_field}"),
        InputFieldMappingEntry(name="file_id", source="/document/file_id"),
        InputFieldMappingEntry(
            name="page_number", source="/document/pages/*/locationMetadata/pageNumber"
        ),
        InputFieldMappingEntry(name="chunk_number", source="/document/pages/*/chunks/*/number"),
        InputFieldMappingEntry(name="chunk_content", source="/document/pages/*/chunks/*/content"),
        InputFieldMappingEntry(name="page_summary", source="/document/pages/*/summary"),
        InputFieldMappingEntry(name="page_content", source="/document/pages/*/content"),
        InputFieldMappingEntry(name="embeddings", source="/document/pages/*/chunks/*/embeddings"),
        InputFieldMappingEntry(name="page_embeddings", source="/document/pages/*/embeddings"),
        InputFieldMappingEntry(
            name="languages", source="/document/pages/*/chunks/*/language_metadata/codes/*"
        ),
    ]
    return SearchIndexerIndexProjection(
        selectors=[
            SearchIndexerIndexProjectionSelector(
                target_index_name=index_name,
                parent_key_field_name="parent_id",
                source_context="/document/pages/*/chunks/*",
                mappings=field_mappings,
            )
        ],
        parameters=SearchIndexerIndexProjectionsParameters(
            projection_mode="skipIndexingParentDocuments"
        ),
    )


def check_function_skill(endpoint: str, key: str) -> None:
    """Require an HTTPS Function URL without credentials, plus its Function key."""
    parsed = urlparse(endpoint)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not key.strip()
    ):
        raise InputError("Configure an HTTPS custom-skill endpoint and its Function key")


def check_split_settings(length: int, overlap: int) -> None:
    """Text Split accepts 300-8000 characters; keep overlap under half a chunk."""
    if not 300 <= length <= 8000 or not 0 <= 2 * overlap < length:
        raise InputError(
            "Chunk length must be 300-8000 characters and overlap less than half the length"
        )


def check_embedding_settings(
    openai_endpoint: str, model: str, dimensions: int, deployment: str
) -> str:
    """Check what Search's embedding skill supports and return its resource URL."""
    if dimensions <= 0 or not deployment.strip():
        raise InputError("Positive dimensions and an embedding deployment are required")
    supported_dimensions = {
        "text-embedding-ada-002": (1536, 1536),
        "text-embedding-3-small": (1, 1536),
        "text-embedding-3-large": (1, 3072),
    }
    limits = supported_dimensions.get(model)
    if limits is None or not limits[0] <= dimensions <= limits[1]:
        raise InputError("Embedding model or dimensions are not supported by Search")
    endpoint = urlparse(openai_endpoint)
    if (
        endpoint.scheme != "https"
        or not endpoint.hostname
        or endpoint.username
        or endpoint.password
        or endpoint.query
        or endpoint.fragment
        or endpoint.path.rstrip("/") not in ("", "/openai/v1")
        or not endpoint.hostname.endswith((".openai.azure.com", ".services.ai.azure.com"))
    ):
        raise InputError("Use a supported Azure OpenAI resource endpoint for Search embeddings")
    return openai_endpoint.rstrip("/").removesuffix("/openai/v1")


def build_enrichment_skillset(
    *,
    index_name: str,
    skillset_name: str,
    tenant_metadata_field: str,
    embedding_deployment: str,
    openai_endpoint: str,
    embedding_model: str,
    embedding_size: int,
    custom_skill_endpoint: str,
    custom_skill_key: str,
    chunk_number_skill_endpoint: str,
    chunk_number_skill_key: str,
    ai_services_key: str | None = None,
    split_length: int = 5000,
    split_overlap: int = 750,
) -> SearchIndexerSkillset:
    """Define document enrichment and its index mappings, without making Azure requests.

    Pipeline, in order:
      1. Document Layout extracts one text section per physical page.
      2. The identity guard releases page text only when tenant and file IDs exist.
      3. Page embedding and the summary Function work on the whole page.
      4. Text Split cuts each page into overlapping chunks.
      5. The numbering Function (same app as the summary) numbers each page's chunks.
      6. Chunk embedding, language detection and a Shaper (which wraps the single
         language code in a collection) enrich every chunk.
    The index projection then writes one search document per chunk.

    Layout receives Search's original file reference directly, because routing it
    through a Conditional skill would lose the type Layout needs.
    """
    validate_tenant_metadata_field(tenant_metadata_field)
    check_function_skill(custom_skill_endpoint, custom_skill_key)
    check_function_skill(chunk_number_skill_endpoint, chunk_number_skill_key)
    check_split_settings(split_length, split_overlap)
    resource_url = check_embedding_settings(
        openai_endpoint, embedding_model, embedding_size, embedding_deployment
    )

    # Guard text, not Search's special file reference, which must reach Layout unchanged.
    identity_guard = ConditionalSkill(
        name="require-blob-identity",
        context="/document/pages/*",
        inputs=[
            InputFieldMappingEntry(
                name="condition",
                source=(
                    f"= $(/document/{tenant_metadata_field}) != null && "
                    f"$(/document/{tenant_metadata_field}) != '' && "
                    "$(/document/file_id) != null && $(/document/file_id) != ''"
                ),
            ),
            InputFieldMappingEntry(name="whenTrue", source="/document/pages/*/content"),
            InputFieldMappingEntry(name="whenFalse", source="= null"),
        ],
        outputs=[OutputFieldMappingEntry(name="output", target_name="identified_content")],
    )
    layout_skill = DocumentIntelligenceLayoutSkill(
        name="extract-full-pages",
        context="/document",
        output_mode="oneToMany",
        output_format="text",
        # SDK default is markdown-only and Azure rejects it for text output.
        markdown_header_depth=None,  # type: ignore[arg-type]
        extraction_options=["locationMetadata"],
        inputs=[InputFieldMappingEntry(name="file_data", source="/document/file_data")],
        outputs=[OutputFieldMappingEntry(name="text_sections", target_name="pages")],
    )
    page_embedding_skill = AzureOpenAIEmbeddingSkill(
        name="embed-full-page",
        context="/document/pages/*",
        resource_url=resource_url,
        deployment_name=embedding_deployment,
        model_name=embedding_model,
        dimensions=embedding_size,
        inputs=[InputFieldMappingEntry(name="text", source="/document/pages/*/identified_content")],
        outputs=[OutputFieldMappingEntry(name="embedding", target_name="embeddings")],
    )
    split_skill = SplitSkill(
        name="split-page-text",
        context="/document/pages/*",
        text_split_mode="pages",
        maximum_page_length=split_length,
        page_overlap_length=split_overlap,
        inputs=[InputFieldMappingEntry(name="text", source="/document/pages/*/identified_content")],
        outputs=[
            OutputFieldMappingEntry(name="textItems", target_name="chunk_texts"),
        ],
    )
    # Native Split outputs parallel arrays; projections cannot pair their sibling values.
    numbering_skill = WebApiSkill(
        name="number-page-chunks",
        context="/document/pages/*",
        uri=chunk_number_skill_endpoint,
        http_method="POST",
        timeout=timedelta(seconds=30),
        batch_size=1,
        degree_of_parallelism=1,
        http_headers={"x-functions-key": chunk_number_skill_key},
        inputs=[InputFieldMappingEntry(name="texts", source="/document/pages/*/chunk_texts/*")],
        outputs=[OutputFieldMappingEntry(name="chunks", target_name="chunks")],
    )
    embedding_skill = AzureOpenAIEmbeddingSkill(
        name="embed-chunk-text",
        context="/document/pages/*/chunks/*",
        resource_url=resource_url,
        deployment_name=embedding_deployment,
        model_name=embedding_model,
        dimensions=embedding_size,
        inputs=[InputFieldMappingEntry(name="text", source="/document/pages/*/chunks/*/content")],
        outputs=[OutputFieldMappingEntry(name="embedding", target_name="embeddings")],
    )

    summary_skill = WebApiSkill(
        name="summarize-full-page",
        context="/document/pages/*",
        uri=custom_skill_endpoint,
        http_method="POST",
        http_headers={"x-functions-key": custom_skill_key},
        timeout=timedelta(seconds=230),
        batch_size=1,
        degree_of_parallelism=1,
        inputs=[
            InputFieldMappingEntry(name="text", source="/document/pages/*/identified_content"),
        ],
        outputs=[OutputFieldMappingEntry(name="summary", target_name="summary")],
    )
    language_detection_skill = LanguageDetectionSkill(
        name="detect-chunk-language",
        context="/document/pages/*/chunks/*",
        inputs=[InputFieldMappingEntry(name="text", source="/document/pages/*/chunks/*/content")],
        outputs=[OutputFieldMappingEntry(name="languageCode", target_name="detected_language")],
    )
    # The detector returns one code; the index's languages field expects a collection.
    language_collection_skill = ShaperSkill(
        name="collect-chunk-language",
        context="/document/pages/*/chunks/*",
        inputs=[
            InputFieldMappingEntry(
                name="codes",
                source="=[$(/document/pages/*/chunks/*/detected_language)]",
            )
        ],
        outputs=[OutputFieldMappingEntry(name="output", target_name="language_metadata")],
    )

    billing_account: CognitiveServicesAccountKey | DefaultCognitiveServicesAccount
    if ai_services_key:
        billing_account = CognitiveServicesAccountKey(key=ai_services_key)
    else:
        billing_account = DefaultCognitiveServicesAccount()

    return SearchIndexerSkillset(
        name=skillset_name,
        cognitive_services_account=billing_account,
        skills=[
            layout_skill,
            identity_guard,
            page_embedding_skill,
            summary_skill,
            split_skill,
            numbering_skill,
            embedding_skill,
            language_detection_skill,
            language_collection_skill,
        ],
        index_projection=build_index_projection(index_name, tenant_metadata_field),
    )


def build_indexer(
    *,
    indexer_name: str,
    data_source_name: str,
    skillset_name: str,
    index_name: str,
) -> SearchIndexer:
    """Link the three named resources and define an enabled daily indexer.

    Five source documents are processed per batch, not per run. This only builds
    a local definition; saving it in Azure can start document processing.
    """
    return SearchIndexer(
        name=indexer_name,
        data_source_name=data_source_name,
        target_index_name=index_name,
        skillset_name=skillset_name,
        is_disabled=False,
        schedule=IndexingSchedule(interval=timedelta(hours=24)),
        parameters=IndexingParameters(
            batch_size=5,
            max_failed_items=0,
            max_failed_items_per_batch=0,
            configuration=IndexingParametersConfiguration(
                # The SDK types this SQL-only default as str, but Blob requires omission.
                query_timeout=None,  # type: ignore[arg-type]
                data_to_extract="contentAndMetadata",
                allow_skillset_to_read_file_data=True,
                fail_on_unsupported_content_type=True,
                fail_on_unprocessable_document=True,
            ),
        ),
    )


def main() -> None:
    """Validate live schema/model compatibility and create or update the enabled pipeline."""
    load_dotenv(".env", override=False)
    for logger_name in ("azure", "openai", "httpx", "httpcore"):
        logging.getLogger(logger_name).setLevel(logging.CRITICAL)

    search_endpoint = read_required_setting("AZURE_SEARCH_ENDPOINT")
    index_name = read_required_setting("AZURE_SEARCH_INDEX")
    data_source_name = read_required_setting("AZURE_SEARCH_DATA_SOURCE_NAME")
    skillset_name = read_required_setting("AZURE_SEARCH_SKILLSET_NAME")
    indexer_name = read_required_setting("AZURE_SEARCH_INDEXER_NAME")
    storage_resource_id = read_required_setting("AZURE_STORAGE_RESOURCE_ID")
    container_name = read_required_setting("AZURE_STORAGE_ACCEPTED_CONTAINER")
    tenant_metadata_field = read_required_setting("AZURE_SEARCH_TENANT_METADATA_FIELD")
    validate_tenant_metadata_field(tenant_metadata_field)
    custom_skill_endpoint = read_required_setting("AZURE_SEARCH_CUSTOM_SKILL_ENDPOINT")
    custom_skill_key = read_required_setting("AZURE_SEARCH_CUSTOM_SKILL_KEY")
    chunk_number_skill_endpoint = read_required_setting("AZURE_SEARCH_CHUNK_NUMBER_SKILL_ENDPOINT")
    chunk_number_skill_key = read_required_setting("AZURE_SEARCH_CHUNK_NUMBER_SKILL_KEY")
    openai_endpoint = read_required_setting("AZURE_OPENAI_ENDPOINT")
    embedding_deployment = read_required_setting("AZURE_OPENAI_EMBEDDING_DEPLOYMENT")
    ai_services_key = os.environ.get("AZURE_AI_SERVICES_KEY", "").strip() or None
    try:
        split_length = int(os.environ.get("AZURE_SEARCH_SPLIT_LENGTH", "5000"))
        split_overlap = int(os.environ.get("AZURE_SEARCH_SPLIT_OVERLAP", "750"))
    except ValueError:
        raise InputError("Split length and overlap must be integers") from None

    # None uses the model's default size; either way, it must match the target index.
    size_setting = os.environ.get("AZURE_OPENAI_EMBEDDING_DIMENSIONS")
    configured_embedding_size = None
    try:
        if size_setting is not None:
            configured_embedding_size = int(size_setting)
    except ValueError:
        raise InputError("Optional embedding dimensions must be an integer") from None

    credential = get_credential()
    with (
        credential,
        SearchIndexClient(search_endpoint, credential) as index_client,
        OpenAI(
            base_url=f"{openai_endpoint}/openai/v1/",
            api_key=get_bearer_token_provider(credential, "https://ai.azure.com/.default"),
        ) as openai_client,
        SearchIndexerClient(search_endpoint, credential) as indexer_client,
    ):
        index = index_client.get_index(index_name)
        index_embedding_size = validate_index_for_projections(index)
        model_name = probe_embedding_model(
            openai_client, embedding_deployment, index_embedding_size, configured_embedding_size
        )
        # Build every definition before saving any resource, so invalid input stops all writes.
        data_source = build_blob_data_source(
            data_source_name=data_source_name,
            storage_resource_id=storage_resource_id,
            container_name=container_name,
        )
        skillset = build_enrichment_skillset(
            index_name=index_name,
            skillset_name=skillset_name,
            tenant_metadata_field=tenant_metadata_field,
            embedding_deployment=embedding_deployment,
            openai_endpoint=openai_endpoint,
            embedding_model=model_name,
            embedding_size=index_embedding_size,
            custom_skill_endpoint=custom_skill_endpoint,
            custom_skill_key=custom_skill_key,
            chunk_number_skill_endpoint=chunk_number_skill_endpoint,
            chunk_number_skill_key=chunk_number_skill_key,
            ai_services_key=ai_services_key,
            split_length=split_length,
            split_overlap=split_overlap,
        )
        indexer = build_indexer(
            indexer_name=indexer_name,
            data_source_name=data_source_name,
            skillset_name=skillset_name,
            index_name=index_name,
        )
        # Create dependencies first. A later failure does not roll back earlier updates.
        indexer_client.create_or_update_data_source_connection(data_source)
        indexer_client.create_or_update_skillset(skillset)
        indexer_client.create_or_update_indexer(indexer)

    print(
        json.dumps(
            {
                "event": "managed_pipeline_configured",
                "indexer": indexer_name,
                "disabled": indexer.is_disabled,
                "schedule_hours": 24,
                "batch_size": 5,
                "dimensions": index_embedding_size,
                "model": model_name,
                "language_detection_billing": (
                    "configured_key"
                    if ai_services_key
                    else "free_allowance_20_documents_per_indexer_per_day"
                ),
                "document_layout_billing": (
                    "configured_key"
                    if ai_services_key
                    else "free_allowance_20_documents_per_indexer_per_day"
                ),
                "page_numbers": "document_layout_location_metadata",
                "chunk_numbers": "one_based_within_each_page",
                "chunk_length_characters": split_length,
                "chunk_overlap_characters": split_overlap,
                "note": "Configuration only; this does not prove a successful indexing run.",
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except (AzureError, APIError, ValueError, OSError) as exc:
        error = {"event": "pipeline_configuration_failed", "error_type": type(exc).__name__}
        if isinstance(exc, InputError):
            error["reason"] = str(exc)
        print(json.dumps(error), file=sys.stderr)
        sys.exit(1)
