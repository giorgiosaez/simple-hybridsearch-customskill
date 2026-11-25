# Simple Hybrid Search with a Custom Skill

`simple-hybridsearch-customskill` is a readable Azure AI Search example.

Upload an approved document, summarize its full pages, and retrieve relevant
passages with keyword, vector, or hybrid search. Each numbered script is intended
to be read and run independently, without a shared application framework.

**This is a trusted-operator reference implementation, not a production-ready
multi-tenant application.** Script 06 searches across all tenants and prints source
passages. It does not enforce end-user authorization.

```text
Approved document -> Blob content + tenant/file metadata
                         |
                  Blob indexer: content + metadata (04)
                         |
                  Search Document Layout: full physical pages
                         |
                  Page embedding skill + summary-only Function
                         |
                  Search Text Split: 5,000 / 750 characters
                         |
                  Numbering function in the same app: {content, number}
                         |
                  Chunk embedding skill
                         |
                  Language Detection + Shaper
                         |
                  Index projections: text, summary, vectors, metadata
                         |
                  Search example (06)
```

The **indexer reads Blob file data and tenant/file metadata**. Search's Document
Layout skill extracts full-page text without additional Layout chunking. Search
embeds each full page, splits each page, embeds its chunks, detects language, and
projects the results into the index.
The **summary Function only summarizes**: it accepts `{"text": "full page text"}` and returns
`{"summary": "short summary"}` inside the custom-skill record envelope.
It does not download documents, read metadata, split text, or generate vectors.

A small [numbering function](custom_skill/function_app.py) in the same Function App only
turns Search's already-split strings into `{content, number}` objects. Text Split
returns text and ordinals in separate arrays; native projections do not pair
sibling arrays into one scalar number per chunk. Keeping the number attached to
its text avoids that failure without reimplementing Search's splitting.

For teaching, both HTTP handlers follow **read -> process -> respond**:
`read_record` validates the Search request, `build_record_result` handles the
operation and record-level errors, and `enrich` returns the HTTP response.
The summary function calls `summarize_record`; the numbering function calls
`number_record` and `build_numbering_result`. Both reuse `read_record` in the same
file and deployment package. The summary route stays `/api/enrich`; numbering
uses `/api/number-chunks`. Only the summary function calls the model.

Each chunk retains its **physical PDF `page_number`**, supplied by Document Layout's
location metadataâ€”not by counting chunks. Multiple chunks can belong to the same page.
Text Split uses a maximum of **5,000 characters** and **750-character overlap**.
It prefers sentence boundaries, so actual chunks can be shorter than the maximum.
Existing `.env` or process settings override these defaults; update old values
before running the workflow.
### Required index contract

All eleven fields below are mandatory. Simplifying ingestion must not remove
them or replace page-level values with chunk-level values.

| Field | Required meaning |
|---|---|
| `tenant_id` | Trusted tenant ID from Blob metadata |
| `file_id` | Stable file ID from Blob metadata |
| `page_number` | One-based physical PDF page number |
| `chunk_number` | One-based consecutive number within a physical page; restarts at 1 on each page |
| `chunk_content` | Text of this chunk |
| `chunk_id` | Unique index key for this chunk |
| `page_summary` | Summary of the entire source page, shared by its chunks |
| `embeddings` | Embedding of this chunk's content |
| `languages` | Language-code collection for this chunk |
| `page_content` | Entire extracted source-page text, shared by its chunks |
| `page_embeddings` | Embedding of the entire source page, shared by its chunks |

Managed index projections additionally require the technical `parent_id` field.
The schema and projections include all eleven fields. Page content, page summary,
and page vector are repeated on every chunk of that page; they are not computed
from a single chunk. Both vector fields have the same dimensions and profile.

## Prerequisites and boundaries

- Windows PowerShell, Azure CLI signed in with `az login`, and Python 3.12
  (the tested release baseline).
- Existing Azure AI Search and Azure Storage resources, with an accepted-document
  container. These scripts do not create the Search service or document account.
- Existing embedding and summary deployments. The summary deployment must support
  the Responses API. Use deployment names from your environment.
- Document Layout-compatible Search/Foundry regions. Beyond the free allowance,
  attach a compatible multi-service Foundry billing resourceâ€”not an OpenAI-only
  resource. Key-based billing requires matching regions. See
  [Layout requirements](docs/operations.md#document-layout-requirements).
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

**Use existing Blobs for the current workflow.** Run the master with `-SkipUpload`:
Azure Search reads the documents directly from the configured container. Each
approved Blob must already have the configured tenant metadata field and
`file_id`. No local document path or manual indexing script is needed.

The page-first pipeline above preserves all eleven fields. Full-page summary and
embedding skills run on the page before Text Split creates its chunks. Index
projections copy those page-level values onto each corresponding chunk. The
numbering Function remains a small adapter; it does not ingest or crack documents.

| Step | Script | When to run |
|---|---|---|
| 00 | [00_configure_environment.ps1](rag_search/00_configure_environment.ps1) | Configure local settings |
| 01 | [01_upload_document.py](rag_search/01_upload_document.py) | Upload a document, unless an approved Blob already has the required metadata |
| 02 | [02_create_index.py](rag_search/02_create_index.py) | Create or update the search index |
| 03 | [03_deploy_custom_skill.ps1](rag_search/03_deploy_custom_skill.ps1) | Deploy both functions in one Function App |
| 04 | [04_create_indexer.py](rag_search/04_create_indexer.py) | Configure managed indexing and enable its initial run |
| 05 | [05_run_indexer.py](rag_search/05_run_indexer.py) | Optional: request an extra run |
| 06 | [06_search.py](rag_search/06_search.py) | Search after indexing completes |

### Run the whole workflow

The [master script](run_all.ps1) calls the numbered scripts in order and prints
timestamped `RUNNING`, `OK`, `SKIPPED`, `FAILED`, and `COMPLETE` messages. Define
`$deployment` as shown in step 3 below, then run:

```powershell
# Print the plan only: no installation, configuration changes, or Azure calls.
.\run_all.ps1 @deployment -SkipUpload -WhatIf
# Use already-approved Blobs; asks for confirmation before making changes.
.\run_all.ps1 @deployment -SkipUpload
# Optional later: omit -SkipUpload to upload a local document through step 01.
```

The master script prepares a missing virtual environment and dependencies,
prompts through step 00 only if `.env` does not exist, performs deployment
preflight, pauses any existing indexer and waits until idle, and runs steps 01
through 06. It uses the repository as its working directory regardless of where
it is invoked. Install Python and Azure CLI and run `az login` first.

Step 04 can start indexing immediately. The master waits for active work, then
uses step 05 to request and verify a new run before searching. Failed items,
execution errors, warnings, or a timeout stop the workflow. Each indexer wait
has a 30-minute deadline; override with `-TimeoutSeconds 3600`. A zero-item run
is reported explicitly and does not prove that the intended document was indexed.
The workflow does not reset change tracking or roll back earlier changes.
An indexer paused before a failure can remain disabled; investigate before retrying.
Review the accepted container: indexing processes all eligible Blobs, not only
the uploaded file. Do not run competing indexing/deployment workflows.

The sections below explain the same steps individually.

### 1. Install and configure

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -c constraints.txt -e ".[function]"
az login
.\rag_search\00_configure_environment.ps1 -IncludeUpload
```

Alternatively, copy [.env.example](.env.example) to `.env` and supply your own
values. Never commit or share the populated file. The constraints capture the
tested Windows/Python 3.12 environment; they are not a guarantee of other-platform
compatibility or a lock of Azure's remote build.

For a shareable first run, use the original two-page [sample PDF](samples/sample.pdf).
Its original text is also available in [sample.txt](samples/sample.txt).
Plain text and Markdown are not supported by this managed Layout skill:

```dotenv
RAG_LOCAL_FILE_PATH='samples\sample.pdf'
RAG_BLOB_NAME=sample.pdf
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

### 3. Validate and deploy both functions in one app

Supply your own names and the summary model's ARM resource ID. The embedding
deployment belongs to the endpoint in `.env`; Search needs its own role there.
Host storage is a separate account, not the document account.

```powershell
$deployment = @{
    ResourceGroup = '<function-resource-group>'
    Location = '<supported-consumption-region>'
    FunctionAppName = '<globally-unique-function-name>'
    FunctionStorageAccountName = '<globallyuniquestoragename>'
    OpenAIResourceId = '<resource-hosting-summary-deployment-arm-id>'
    SubscriptionId = '<subscription-id>'
}
# Local packaging checks; no Azure calls.
.\rag_search\03_deploy_custom_skill.ps1 @deployment -WhatIf
# Read-only Azure prerequisite checks.
.\rag_search\03_deploy_custom_skill.ps1 @deployment -PreflightOnly
# After reviewing the checks and billable resources:
.\rag_search\03_deploy_custom_skill.ps1 @deployment
```

No switch changes network settings or organization policies. Use
`Get-Help .\rag_search\03_deploy_custom_skill.ps1 -Full` for parameter details.

This step packages the Function, reuses only script-owned hosting resources,
assigns the app's summary-model role, and verifies both authenticated routes and
trigger registration. Numbering makes no Storage-data or model calls. Both
functions share the existing app and host storage; no second app is needed.
Both endpoint/key pairs are saved locally without printing keys. Deployment
publishes the whole package, retaining the summary function alongside numbering.
Do not continue if either route fails.

### 4. Configure indexing and verify completion

```powershell
.\.venv\Scripts\python.exe .\rag_search\04_create_indexer.py
```

This enables processing immediately and schedules daily runs. It processes the
accepted container, not just the sample file. Review the container first.
The default Layout/language-detection allowance is 20 documents per indexer per day;
larger workloads require the optional billing configuration.

Verify a completed run with zero failed items in the Search indexer's execution
history, then inspect the indexed tenant/file IDs, physical `page_number`, chunk
numbers restarting per page, full-page content/summaries, both vectors, and languages.
The sample PDF should produce content from pages 1 and 2.
Configuration success is not indexing success. For an extra run only:

```powershell
.\.venv\Scripts\python.exe .\rag_search\05_run_indexer.py
```

Script 05 submits an asynchronous request; it does not wait for completion.
Use `--wait --timeout-seconds 1800` to wait for a new successful run, or
`--pause --timeout-seconds 1800` to disable the indexer and wait until idle.
Pause reports explicitly if no indexer exists. Neither option resets the index.
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

**Migrating from the earlier page-enrichment example:** select a new
`AZURE_SEARCH_INDEX`, `AZURE_SEARCH_INDEXER_NAME`, and `AZURE_SEARCH_SKILLSET_NAME`.
Pause the old indexer and wait for active work **before replacing the same Function**;
the master only pauses the indexer currently named in `.env`.
The old and new Function contracts are incompatible. The new pipeline preserves
all eleven fields, summarizes and embeds whole pages, and additionally embeds chunks.
No script deletes old records,
resets an index, revokes old roles, or removes obsolete Function app settings.
See the [migration checklist](docs/operations.md#schema-migration).

The [manual alternative](rag_search/alternative_ingest_document.py) is retained
for reference, but is not part of the current workflow and is never called by the
master. It writes the same eleven business fields directly. It uses Python character
windows (5,000/750), rather than Search's sentence-aware Text Split. Python counts
Unicode code points; service character accounting can differ for supplementary
characters. It supports text PDFs, text, and HTML, not Layout's OCR/Office coverage.
Set `AZURE_LANGUAGE_ENDPOINT` and `RAG_BLOB_NAME` for this route and run it after
script 02; do not mix manual and managed ingestion for the same corpus.

Full pages are never silently truncated: summary input is limited to 50,000
Python characters, and the embedding model's input-token limit also applies.
Oversized pages fail indexing and require deliberate preparation, not fake
page values copied from a chunk.

- Large-document processing, private networking, identity-based host storage,
  and safe identity-migration cleanup remain future work. See the
  [operational limits](docs/operations.md#known-limitations).
- [Permissions, verification, and troubleshooting](docs/operations.md)
- [MIT license](LICENSE); bundled original sample text is covered by it.

## What is published

This minimal repository contains the master, managed and manual scripts, both Function sources,
dependency files, configuration example, short operations guide, license, and
original sample document. The explicit [.gitignore](.gitignore) allowlist excludes
private configuration, customer data, local deployment notes, notebooks, tests,
CI, development tools, long-form reference notes, and generated files. Excluded
files can remain on the original development machine; they are not deployment
dependencies. New files are ignored until deliberately added to the allowlist.
