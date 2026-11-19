# Simple Hybrid Search with a Custom Skill

`simple-hybridsearch-customskill` is a readable Azure AI Search example.

Upload an approved document, extract and enrich its pages, and retrieve relevant
passages with keyword, vector, or hybrid search. Each numbered script is intended
to be read and run independently, without a shared application framework.

**This is a trusted-operator reference implementation, not a production-ready
multi-tenant application.** Script 06 searches across all tenants and prints source
passages. It does not enforce end-user authorization.

```text
Approved document -> Blob content + tenant/file metadata
                         |
                  Managed indexer (04)
                         |
                  Enrichment Function
                         |
                  Azure AI Search
                         |
                  Search example (06)
```

This walkthrough uses managed indexing. The Function packages
[alternative_ingest_document.py](rag_search/alternative_ingest_document.py) as
`ingestion.py` to reuse its extraction, chunking, and enrichment functions. Keep
that source file: it is a deployment dependency, not an extra step to run.

## Prerequisites and boundaries

- Windows PowerShell, Azure CLI signed in with `az login`, and Python 3.12
  (the tested release baseline).
- Existing Azure AI Search and Azure Storage resources, with an accepted-document
  container. These scripts do not create the Search service or document account.
- Existing embedding and summary deployments. The summary deployment must support
  the Responses API. Use deployment names from your environment.
- Permission to configure Search, create Function hosting, and assign narrowly
  scoped data-plane roles. See [permissions](docs/operations.md#permissions).
- Approval for billable Function hosting, model calls, and indexing.

**Hosting limitation:** script 03 currently deploys Linux Consumption hosting with
the normal key-based Function host-storage connection. That host storage needs
Shared Key access and reachable public endpoints for this deployment design.
This is distinct from the keyless Blob/model SDK calls. Do not weaken organization
policies to run this example. See [hosting constraints](docs/operations.md#hosting-constraints).

## Managed-route quickstart

Run all commands from this repository directory. Existing process environment
variables take precedence over the local configuration file.

| Step | Script | When to run |
|---|---|---|
| 00 | [00_configure_environment.ps1](00_configure_environment.ps1) | Configure local settings |
| 01 | [01_upload_document.py](rag_search/01_upload_document.py) | Upload a document, unless an approved Blob already has the required metadata |
| 02 | [02_create_index.py](rag_search/02_create_index.py) | Create or update the search index |
| 03 | [03_deploy_custom_skill.ps1](03_deploy_custom_skill.ps1) | Validate and deploy the enrichment Function |
| 04 | [04_create_indexer.py](rag_search/04_create_indexer.py) | Configure managed indexing and enable its initial run |
| 05 | [05_run_indexer.py](rag_search/05_run_indexer.py) | Optional: request an extra run |
| 06 | [06_search.py](rag_search/06_search.py) | Search after indexing completes |

### 1. Install and configure

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -c constraints.txt -e ".[function]"
az login
.\00_configure_environment.ps1 -IncludeUpload
```

Alternatively, copy [.env.example](.env.example) to `.env` and supply your own
values. Never commit or share the populated file. The constraints capture the
tested Windows/Python 3.12 environment; they are not a guarantee of other-platform
compatibility or a lock of Azure's remote build.

For a shareable first run, use the original [sample document](samples/sample.txt):

```dotenv
RAG_LOCAL_FILE_PATH='samples\sample.txt'
RAG_BLOB_NAME=sample.txt
RAG_TENANT_ID=demo-tenant
AZURE_SEARCH_TENANT_METADATA_FIELD=tenant_id
```

Use authorized tenant values for real documents. Local customer documents belong
in the ignored `data` directory, not in the published repository.

### 2. Upload and create the index

```powershell
.\.venv\Scripts\python.exe .\rag_search\01_upload_document.py
.\.venv\Scripts\python.exe .\rag_search\02_create_index.py
```

The uploader **overwrites content and metadata** at the selected Blob name. It
writes `tenant_id`, the configured tenant alias, and a stable generated `file_id`.
Ingestion reads those metadata IDs unchanged. Changing identity metadata can leave
old indexed records; cleanup is not automatic.

### 3. Validate and deploy the Function

Supply your own names and ARM resource IDs. The source Storage account must match
the upload/indexer settings. Both model deployments must belong to the supplied
model resource. Host storage is a separate account, not the document account.

```powershell
$deployment = @{
    ResourceGroup = '<function-resource-group>'
    Location = '<supported-consumption-region>'
    FunctionAppName = '<globally-unique-function-name>'
    FunctionStorageAccountName = '<globallyuniquestoragename>'
    SourceStorageResourceId = '<accepted-blob-storage-account-arm-id>'
    OpenAIResourceId = '<resource-hosting-both-model-deployments-arm-id>'
    SubscriptionId = '<subscription-id>'
}

# Local packaging checks; no Azure calls.
.\03_deploy_custom_skill.ps1 @deployment -WhatIf
# Read-only Azure prerequisite checks.
.\03_deploy_custom_skill.ps1 @deployment -PreflightOnly
# After reviewing the checks and billable resources:
.\03_deploy_custom_skill.ps1 @deployment
```

No switch changes network settings or organization policies. Use
`Get-Help .\03_deploy_custom_skill.ps1 -Full` for parameter details.

This step packages the Function, reuses only script-owned hosting resources,
assigns its Blob-reader/model roles, and verifies its authenticated route and
trigger registration. Do not continue if it fails.

### 4. Configure indexing and verify completion

```powershell
.\.venv\Scripts\python.exe .\rag_search\04_create_indexer.py
```

This enables processing immediately and schedules daily runs. It processes the
accepted container, not just the sample file. Review the container first.
The default language-detection allowance is 20 documents per indexer per day;
larger workloads require the optional billing configuration.

Verify a completed run with zero failed items in the Search indexer's execution
history, then inspect the indexed tenant/file IDs, page summaries, and languages.
Configuration success is not indexing success. For an extra run only:

```powershell
.\.venv\Scripts\python.exe .\rag_search\05_run_indexer.py
```

Script 05 submits an asynchronous request; it does not wait for completion.
See [verification and troubleshooting](docs/operations.md#verify-each-stage).

### 5. Search

Set `RAG_QUERY` and `RAG_SEARCH_MODE` in `.env`, then run:

```powershell
.\.venv\Scripts\python.exe .\rag_search\06_search.py
```

For the sample, ask "How can a team reduce energy use?".
**This administrative query is unfiltered across tenants.** Use only approved
test data. An application must derive tenant scope from authenticated identity
and apply authorization on every query; a caller-supplied tenant ID is not enough.

## Limits and maintenance

- Text PDFs, UTF-8 text/Markdown, and HTML only; no OCR or Office parsing.
- Default limits: 25 MiB, 50 pages, and token-bounded chunks. Any textless PDF page
  fails. The managed custom-skill timeout is 230 seconds.
- Large-document processing, private networking, identity-based host storage,
  and safe identity-migration cleanup remain future work. See the
  [operational limits](docs/operations.md#known-limitations).
- [Permissions, verification, and troubleshooting](docs/operations.md)
- [MIT license](LICENSE); bundled original sample text is covered by it.

## What is published

This minimal repository contains the managed-route scripts, Function source,
dependency files, configuration example, short operations guide, license, and
original sample document. The explicit [.gitignore](.gitignore) allowlist excludes
private configuration, customer data, local deployment notes, notebooks, tests,
CI, development tools, long-form reference notes, and generated files. Excluded
files can remain on the original development machine; they are not deployment
dependencies. New files are ignored until deliberately added to the allowlist.
