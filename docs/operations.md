# Operations and sharing

## Hosting constraints

Application data access and Function host storage are different concerns:

| Component | Current authentication | Current networking |
|---|---|---|
| Upload and operator scripts | Microsoft Entra credentials and data-plane RBAC | Caller must reach Storage, Search, and model endpoints |
| Search Blob data source | Search managed identity with Blob-reader access | Search must reach the accepted container |
| Function content/model calls | Function managed identity | Current Linux Consumption code uses public service endpoints |
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
| Search service managed identity | Storage Blob Data Reader on the accepted container/account |
| Function managed identity | Storage Blob Data Reader on source Storage and Cognitive Services OpenAI User on the model resource; script 03 assigns these |
| Deployment operator | Resource-creation permissions plus permission to assign those runtime roles; Contributor alone cannot assign roles |

Wait for role propagation before retrying access. Script 04 does not enable
identities, grant roles, or change networking. The Function key and optional
billable AI Services key are secrets, not substitutes for these permissions.

## Verify each stage

1. **Upload:** check Blob size, content type, and both metadata IDs. Upload success
   does not establish that the document was approved; validation is external.
2. **Function:** confirm an authenticated empty `POST /api/enrich` returns HTTP
   400 with `Expected exactly one custom-skill record`, and that `enrich` appears
   in the Function listing. A portal state of Running alone is insufficient.
3. **Indexer configuration:** confirm the custom skill includes tenant metadata
   and `/document/file_id` inputs and projections include the enriched fields.
4. **Indexer execution:** inspect Search service -> Indexers -> the configured
   indexer -> Execution history. Require a new completed run, zero failed items,
   and review all warnings. A successful zero-item run does not prove a new
   document was enriched.
5. **Retrieval:** inspect indexed tenant/file IDs, page/chunk numbers, summaries,
   and language arrays. Query a distinctive phrase from the approved sample, then
   verify vector/hybrid retrieval too.

Local validation does not certify live network access or production scale.

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

The two Function settings written by successful deployment are refreshed in the
current process as well as `.env`, preventing stale process keys from overriding
the newly deployed endpoint/key in the following step. Other existing process
settings still take precedence over `.env`; check them before starting.

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
- Only text-based PDF, UTF-8 text/Markdown, and HTML are supported, with default
  limits of 25 MiB and 50 pages. Textless pages fail; there is no OCR.
- Page summaries and embeddings are generated synchronously. A 230-second
  custom-skill timeout and model limits constrain document size and throughput;
  larger workloads need a redesigned processing flow.
- Both configured deployments must be in the supplied model resource. The
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
