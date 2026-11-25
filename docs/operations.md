# Operations and sharing

## Hosting constraints

Application data access and Function host storage are different concerns:

| Component | Current authentication | Current networking |
|---|---|---|
| Upload and operator scripts | Microsoft Entra credentials and data-plane RBAC | Caller must reach Storage, Search, and model endpoints |
| Search Blob data source | Search managed identity with Blob-reader access | Search must reach the accepted container |
| Search embedding skill | Search system-assigned managed identity | Search must reach the embedding model endpoint |
| Function summary-model calls | Function managed identity | Current Linux Consumption code uses a public model endpoint |
| Function host storage | Normal Azure CLI-configured storage connection using Shared Key | Current deployment needs reachable public host-storage endpoints |
| Search-to-Function request | Function key, kept out of source control | Search must reach the Function endpoint |

For this host-storage design, disabling **Allow storage account key access** can
break deployment/runtime storage authentication. Disabling **Public network
access** without an approved compatible connectivity design can break deployment
and cold starts. Public endpoint access does not mean anonymous access: anonymous
Blob access must remain disabled.

These are demo limitations, not production recommendations. Never bypass
organization policy, enable anonymous storage, or automatically broaden firewall
rules to make a deployment succeed. Use an administrator-approved hosting/network
design. Identity-based host storage and private connectivity remain future work.

Script 03's local `-WhatIf` makes no Azure calls. Its `-PreflightOnly` builds and
checks the package, then performs read-only Azure checks. Live preflight checks
explicitly disabled public endpoints and disabled Shared Key on existing host
storage before the first Azure mutation. Selected-network firewalls, organization
policies, RBAC propagation, remote package feeds, and runtime reachability can
still prevent deployment; preflight is not a connectivity guarantee.

For summary-model access, script 03 reuses an existing system-assigned identity
or adds it with a targeted ARM PATCH that preserves the app's Linux hosting
settings and any user-assigned identities. This avoids a whole-site identity
update being rejected as an incompatible hosting-plan change.
For a new app, `-FunctionPlanName` can select an existing deployment-owned Linux
Consumption (Y1) plan instead of the CLI's automatic shared plan. The script checks
ownership, SKU, Linux hosting, region, and any existing app association before
writing. It never moves an existing app between plans. Recreating an empty app
with a broken hosting association requires explicit operator approval.
For this explicit Y1 path, app creation uses ARM with `alwaysOn=false`; the CLI's
dedicated-plan creation path enables AlwaysOn, which Consumption hosting rejects.

## Permissions

Use narrowly scoped roles and enable the Search service's system-assigned
managed identity before configuring the Blob data source. Search data-plane
requests must allow Microsoft Entra authentication (RBAC or both authentication
modes, not API-key-only).

| Principal | Role and scope |
|---|---|
| Upload operator | Storage Blob Data Contributor on the accepted container/account |
| Index/schema/indexer operator | Search Service Contributor on the Search service |
| Search-query operator | Search Index Data Reader on the Search service; vector/hybrid queries also read index definitions and need Search Service Contributor |
| Operator making embedding probes/queries | Cognitive Services OpenAI User on the model resource |
| Search service managed identity | Storage Blob Data Reader on the accepted container/account AND Cognitive Services OpenAI User on the embedding resource; provision separately |
| Function managed identity | Cognitive Services OpenAI User on the summary-model resource only; script 03 assigns it |
| Deployment operator | Resource-creation permissions plus permission to assign those runtime roles; Contributor alone cannot assign roles |

Wait for role propagation before retrying access. Script 04 does not enable
identities, grant roles, or change networking. The Function key and optional
billable AI Services key are secrets, not substitutes for these permissions.

## Verify each stage

1. **Upload:** check Blob size, content type, and both metadata IDs. Upload success
   does not establish that the document was approved; validation is external.
2. **Functions:** confirm authenticated empty requests to `/api/enrich` and
   `/api/number-chunks` return HTTP 400 with `Expected exactly one custom-skill record`.
   Both `enrich` and `number_chunks` must appear in the same app's Function listing.
   A portal state of Running alone is insufficient.
3. **Indexer configuration:** confirm `allowSkillsetToReadFileData=true`,
   Document Layout with text output and `locationMetadata`, root
   tenant/file metadata projections, and a custom skill with only the `text` input
   and `summary` output at full-page context. Text Split uses 5,000/750; the
   numbering endpoint takes `texts` and returns `{content, number}` chunks.
   Both page and chunk embeddings use Search's identity, not either Function.
4. **Indexer execution:** inspect Search service -> Indexers -> the configured
   indexer -> Execution history. Require a new completed run, zero failed items,
   and review all warnings. A successful zero-item run does not prove a new
   document was enriched.
5. **Retrieval:** inspect indexed tenant/file IDs, physical `page_number`, chunk text, summaries,
   and language arrays. Query a distinctive phrase from the approved sample, then
   verify vector/hybrid retrieval too.

Local validation does not certify live network access or production scale.

Document Layout receives Search's original file reference directly. Passing that
special reference through a Conditional skill loses the type Layout needs.
Missing or empty tenant/file metadata instead causes the Conditional skill to pass
null page text to summarization, embeddings, and splitting, preventing unidentified
chunks. Layout still extracts these files before the guard runs. Inspect all indexer
warnings and verify expected records; a source document counted as processed is
not proof that chunks were indexed. The master rejects warnings. Metadata must
be trusted and validated at upload; the guard does not validate whitespace-only
or malicious metadata. There is no metadata validation in the summary Function.

## Schema migration

The current schema contains all eleven required business fields plus technical
`parent_id`. Earlier eight/nine-field local variants are incomplete. Use a new
index when incompatible fields remain (including the obsolete chunk-summary
field); existing stored records do not prove the new pipeline works.

1. Record the old index/indexer/skillset names. With the old configuration,
   pause its indexer using script 05 `--pause`, and wait until idle.
2. Configure new index, indexer, and skillset names. Keep the old index intact;
   neither schema creation nor step 04 performs destructive migration.
3. Grant Search's system identity Blob-reader and embedding-model access.
   Check Document Layout region/billing requirements before running the indexer.
4. Deploy and verify both functions in one app. Prefer a new app
   when the old pipeline must remain available; reusing it breaks the old contract.
5. Configure and run the new pipeline. Verify expected chunks, physical page numbers, and metadata,
   summaries, embeddings, languages, and queries before switching consumers.
6. Review old records/resources and obsolete Function Blob-reader assignments
   and Blob/embedding/page app settings for separately approved cleanup.

The master pauses only the configured indexer; selecting new names does **not**
pause the old one. Do not run the old indexer against the new Function.

## Document Layout requirements

The [Document Layout skill](https://learn.microsoft.com/azure/search/cognitive-search-skill-document-intelligence-layout)
uses Azure Document Intelligence to extract full physical pages and supplies
`locationMetadata.pageNumber`. Index projections write that value to `page_number`.
Omit extra Layout chunking and its SDK-default markdown header depth when using
text output. Page embedding and summarization consume the entire page `content`;
Text Split produces chunks within each page, followed by numbering, chunk
embedding, and Language Detection. Blob identity stays on the source document.

The summary Function rejects pages longer than 50,000 Python characters without
truncating. Full-page embeddings must also fit the model's token limit. Confirm
OCR completeness and expected physical-page coverage in a real indexing run.

- Use supported Search/Document Intelligence regions. Key-based billing needs a
  compatible multi-service Foundry resource in the same region as Search.
  An embedding deployment alone does not establish Layout compatibility.
- Layout and Language Detection share the configured skillset billing account.
  `AZURE_AI_SERVICES_KEY` is optional for the free document allowance; larger
  workloads require the compatible billable resource. Never publish its key.
- Check processing-tier limits and full-document coverage. A Document Intelligence
  F0 tier can limit PDFs to the first two pages; use an appropriate paid tier for
  larger documents. Neither a successful run nor an uploaded byte count proves
  that all intended pages were indexed.
- Supported formats include PDF, JPEG/JPG/PNG/BMP/TIFF, DOCX/XLSX/PPTX, and HTML.
  Plain text and Markdown are not supported by Layout. The generic uploader can
  upload those files, but this managed pipeline cannot analyze them.
- Layout supplies OCR for supported image/scanned documents. For non-PDF formats,
  Document Intelligence defines the page units; do not describe them as PDF pages.
- Use the original two-page [sample PDF](../samples/sample.pdf) for the quickstart.
  Verify chunks from pages 1 and 2. For a real PDF, verify distinctive content from
  the final page as well as expected page coverage; blank pages need not emit chunks.

These prerequisites are not provisioned or live-validated by script 03, which only
deploys both functions together. Local SDK tests do not prove
live Layout availability, OCR accuracy, or complete document coverage.

## Master workflow status and failures

[run_all.ps1](../run_all.ps1) delegates to the numbered examples rather than
duplicating their upload, schema, deployment, or query code. `-WhatIf` prints
the plan only. A normal run confirms once before changing local/Azure resources.
It preserves an existing `.env`; a missing one is collected through step 00.

The master disables an existing indexer's schedule before upload/deployment and
waits for its active run; disabling is not cancellation. Step 04 re-enables it.
Step 05 with `--wait` waits for existing work, requests another run, and requires
a distinct execution with success and no errors, failed items, or warnings.
Polling prints phase messages and counts, never raw service-error messages,
keys, Blob paths, or metadata IDs. The final search intentionally prints passages.
Review warnings in Azure execution history; they are not silently ignored.

If any step fails, the terminal shows the failed phase and the script exits
unsuccessfully. Earlier resource writes and uploads persist. Before step 04,
an indexer may remain paused; after step 04, its daily schedule can remain
enabled even if polling times out. A polling timeout does not cancel Azure work.
Inspect status before retrying, and resume/reconfigure only once the Function
is healthy. No automatic index reset, deletion, or rollback is performed.

Both endpoint/key pairs written by successful deployment are refreshed in the
current process as well as `.env`, preventing stale process keys from overriding
the newly deployed endpoint/key in the following step. Other existing process
settings still take precedence over `.env`; check them before starting.

The `/api/number-chunks` endpoint in the shared app follows the
[custom Web API skill contract](https://learn.microsoft.com/azure/search/cognitive-search-custom-skill-web-api).
It accepts one page's `texts` array and returns numbered chunk objects. Extraction,
[Text Split](https://learn.microsoft.com/azure/search/cognitive-search-skill-textsplit),
embeddings, and Language Detection remain native Search skills. No numbering
operation runs inside the summary handler; it remains a separate function
within the same Azure resource, sharing its deployment and host storage.

## Deployment failures

| Symptom | Check before retrying |
|---|---|
| Storage 403 | Correct account, data-plane role, public/private routing, firewall, and organization policy |
| Host storage has Shared Key disabled | This deployment still uses a key-based host-storage connection; use an approved compatible design |
| CLI rejects a resource write | Read the Azure policy/RBAC error; do not loop or change policy automatically |
| ZIP remote build fails | Inspect `az webapp log deployment show --name <app> --resource-group <group>` and Azure deployment logs |
| Deployment logs return 404 | Record the deployment ID/time; inspect host-storage connectivity and platform diagnostics |
| Function 503 or missing `enrich` | Host storage, worker dependencies, configuration, trigger registration, and runtime logs |
| Indexer skill error | Function readiness/key, metadata consistency, model access/dimensions, input limits, and timeout |

Do not print connection strings, keys, raw configuration files, or customer
passages while collecting diagnostics. Keep deployment notes outside source
control. The local `.azure` directory is ignored.

When updating an existing Function/skillset contract, pause the indexer first,
wait for any active run to finish, deploy and verify the Function, update the
skillset, then re-enable and verify indexing. Do not resume against an unhealthy
or incompatible Function. Index reset and old-record deletion require a deliberate
review; none are automatic here.

## Dependencies and known limitations

[constraints.txt](../constraints.txt) records the tested Windows/Python 3.12
environment. Install with `pip install -c constraints.txt -e ".[function]"`.
Constraints limit versions; unused development-package entries are not installed.
The constraints intentionally do not replace the supported dependency ranges.
Regenerate with `python -m pip freeze --exclude-editable --exclude pip` only in a
reviewed environment, then test a clean installation before releasing.

Azure's Linux remote build still resolves [Function requirements](../custom_skill/requirements.txt)
independently; the Windows constraints do not lock that environment.

## Known limitations

- The sample is operator-only. Script 06 is deliberately unfiltered across
  tenants; a production application must derive tenant scope from authenticated
  identity and authorize every query.
- The current hosting design requires Shared Key host storage and reachable
  public endpoints. Identity-based host storage/private networking require a
  compatible, approved hosting design.
- Document Layout performs page-aware extraction; supported formats and
  extraction limits depend on the Search and Document Intelligence tiers.
  The uploader's default 25 MiB limit is not a Search extraction guarantee.
- `AZURE_SEARCH_SPLIT_LENGTH` is 300–8,000 characters (default 2,000).
  `AZURE_SEARCH_SPLIT_OVERLAP` must be less than half the length (default 200).
  Chunks stay within physical PDF pages and carry their page number. The Function accepts
  at most 8,000 characters and one record per call, with a 30-second model timeout.
  The Web API skill allows 230 seconds; throughput and model limits still apply.
- The deployment resource ID identifies the summary model resource. The
  embedding compatibility probe currently accepts text-embedding-ada-002,
  text-embedding-3-small, and text-embedding-3-large.
- No deletion-detection policy is configured. Changing tenant/file metadata,
  moving or deleting Blobs, or mixing ingestion ownership can leave stale
  records. Plan and verify cleanup deliberately; no script resets the index.

## Before sharing or releasing

- Confirm approval for public/private visibility and the [MIT license](../LICENSE).
  Contributor attribution is generic; update it if your organization requires a
  particular copyright notice.
- Review the candidate Git files before committing. `.env`, `.env.*` other than
  the example, `.azure`, `data`, caches, virtual environments, and build outputs
  must not be published. Ignore rules do not erase existing Git history.
- Do not redistribute local PDFs without permission. The [original sample](../samples/sample.txt)
  is intended for sharing under this repository's license.
- Run local validation and a secret scanner. If a real secret ever reached
  history, revoke/rotate it and follow your organization's history-remediation
  process before sharing.
- With Gitleaks installed, run `gitleaks git . --log-opts=--all --redact` to
  check committed history. Before the first commit, scan only an exported,
  reviewed candidate tree with `gitleaks dir <candidate-directory> --redact`.
  Do not scan or publish a populated working tree containing private files.
- Replace environment-specific URLs/IDs with placeholders in examples.
- Review Git author/committer metadata too: secret scanners do not treat personal
  email addresses as credentials. Use the account's GitHub noreply address when
  personal-email disclosure is not intended.
- Do not publish Function keys or deployment bundles containing settings.
- Keep operational limitations documented, not as undisclosed workarounds.
