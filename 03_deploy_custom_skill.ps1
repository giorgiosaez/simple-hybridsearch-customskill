<#
.SYNOPSIS
03: Provision and deploy the Python page-enrichment custom skill.
.DESCRIPTION
Packages the Function together with the reusable ingestion source and validates
its imports and handler/binding names locally. Creates or reuses tagged Linux
Consumption hosting, assigns the Function identity Blob-reader/OpenAI access,
and deploys with remote build. Verifies the authenticated route, synchronizes
triggers, and checks Azure registration before saving the endpoint/key to .env.
Requires Azure CLI sign-in, project dependencies, and role-assignment permissions.
Creates billable hosting but does not process documents or reset the indexer.
Use -WhatIf for local validation without Azure calls.
.PARAMETER PreflightOnly
Build locally and check existing Azure prerequisites without changing Azure resources.
.PARAMETER ResourceGroup
Resource group to create or reuse for Function hosting.
.PARAMETER Location
Azure region supporting Linux Consumption and Python 3.12.
.PARAMETER FunctionAppName
Globally unique name of the enrichment Function App.
.PARAMETER FunctionStorageAccountName
Globally unique Storage account name for Function hosting, not source documents.
.PARAMETER SourceStorageResourceId
ARM resource ID of the Storage account containing the accepted document container.
.PARAMETER OpenAIResourceId
ARM resource ID of the resource hosting both embedding and summary deployments.
.PARAMETER SubscriptionId
Target Azure subscription. If omitted, use the Azure CLI's current subscription.
.PARAMETER EnvFile
Local configuration file to read and update; defaults to .env next to this script.
#>
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [Parameter(Mandatory = $true)][string]$ResourceGroup,
    [Parameter(Mandatory = $true)][string]$Location,
    [Parameter(Mandatory = $true)][string]$FunctionAppName,
    [Parameter(Mandatory = $true)][string]$FunctionStorageAccountName,
    [Parameter(Mandatory = $true)][string]$SourceStorageResourceId,
    [Parameter(Mandatory = $true)][string]$OpenAIResourceId,
    [string]$SubscriptionId,
    [string]$EnvFile = (Join-Path $PSScriptRoot '.env'),
    [switch]$PreflightOnly
)

$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$envPath = [IO.Path]::GetFullPath($EnvFile)
if (-not (Test-Path -LiteralPath $python)) { throw 'Create the project .venv and install dependencies first.' }
if (-not (Test-Path -LiteralPath $envPath)) { throw 'Run 00_configure_environment.ps1 first to create .env.' }
if (-not (Get-Command az -ErrorAction SilentlyContinue)) { throw 'Install Azure CLI and run az login first.' }
if ($FunctionStorageAccountName -notmatch '^[a-z0-9]{3,24}$') { throw 'Use a valid Storage account name (3-24 lowercase letters/digits).' }
if ($FunctionAppName -notmatch '^[a-zA-Z0-9][a-zA-Z0-9-]{0,58}[a-zA-Z0-9]$') { throw 'Use a valid Function App name.' }
if ($SourceStorageResourceId -notmatch '^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft.Storage/storageAccounts/[^/]+$') {
    throw 'SourceStorageResourceId must identify the accepted-documents Storage account.'
}
if ($OpenAIResourceId -notmatch '^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft.CognitiveServices/accounts/[^/]+$') {
    throw 'OpenAIResourceId must identify the resource hosting both model deployments.'
}

$subscriptionArguments = @()
if ($SubscriptionId) { $subscriptionArguments = @('--subscription', $SubscriptionId) }
function Invoke-AzureJson {
    <#
    .SYNOPSIS
    Run an Azure CLI command and parse its JSON output.
    .DESCRIPTION
    Applies the selected subscription and fails explicitly on a nonzero exit
    code. Does not roll back resource changes made by earlier commands.
    .PARAMETER Arguments
    Azure CLI command tokens, excluding the az executable and common output flags.
    #>
    param([string[]]$Arguments)
    $result = & az @Arguments @subscriptionArguments --only-show-errors --output json
    if ($LASTEXITCODE -ne 0) {
        $operation = "$($Arguments[0]) $($Arguments[1])"
        if ($operation -eq 'functionapp deployment') {
            throw "Function ZIP deployment failed. Inspect remote-build and host-storage diagnostics with: az webapp log deployment show --name $FunctionAppName --resource-group $ResourceGroup. If logs return 404, record the deployment ID/time and check host-storage routing/authentication and Azure platform diagnostics. Earlier resource changes may persist."
        }
        if ($operation -eq 'role assignment') {
            throw 'Runtime RBAC assignment failed. Check role-assignment permissions and resource scope; Contributor alone does not grant data-plane access. Earlier resource changes may persist.'
        }
        throw "Azure CLI failed during $operation. Review the CLI error for authentication, permissions, quota, network, or organization-policy restrictions. No policy or network changes are automatic. Earlier resource changes may persist."
    }
    if ($result) { return ($result -join "`n" | ConvertFrom-Json) }
}

function Assert-FunctionHostStorage {
    param($Storage)
    if ($Storage.publicNetworkAccess -eq 'Disabled') {
        throw 'Function host-storage public network access is disabled. This Linux Consumption deployment needs reachable public host-storage endpoints. Use an organization-approved compatible hosting/network design; do not bypass policy. No further Azure changes were requested.'
    }
    if ($Storage.allowSharedKeyAccess -eq $false) {
        throw 'Function host-storage Shared Key access is disabled. This deployment uses the normal key-based host-storage connection. Use an approved identity-based host-storage design before disabling keys. No further Azure changes were requested.'
    }
    if ($Storage.networkRuleSet.defaultAction -eq 'Deny') {
        Write-Warning 'Host storage has a selected-network firewall. Verify approved deployment/runtime connectivity; preflight cannot establish reachability from the Function.'
    }
}

$work = Join-Path ([IO.Path]::GetTempPath()) ('rag-custom-skill-' + [guid]::NewGuid().ToString('N'))
$stage = Join-Path $work 'package'
$settingsFile = Join-Path $work 'appsettings.json'
$zip = Join-Path $work 'skill.zip'
try {
    New-Item -ItemType Directory -Path $stage -Force -WhatIf:$false -Confirm:$false | Out-Null
    foreach ($file in @('function_app.py', 'host.json', 'requirements.txt')) {
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot "custom_skill\$file") -Destination $stage -WhatIf:$false -Confirm:$false
    }
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'rag_search\alternative_ingest_document.py') -Destination (Join-Path $stage 'ingestion.py') -WhatIf:$false -Confirm:$false

    $env:RAG_DEPLOY_ENV_PATH = $envPath
    $env:RAG_DEPLOY_SETTINGS_PATH = $settingsFile
    @'
import json
import inspect
import os
import sys
from pathlib import Path
from dotenv import dotenv_values

package = Path(os.environ["RAG_DEPLOY_SETTINGS_PATH"]).parent / "package"
sys.path.insert(0, str(package))
import function_app

functions = function_app.app.get_functions()
if not any(function.get_function_name() == "enrich" for function in functions):
    raise SystemExit("Deployment package must register the enrich function")
for function in functions:
    bindings = json.loads(function.get_function_json())["bindings"]
    binding_names = {binding["name"] for binding in bindings if binding["name"] != "$return"}
    parameters = set(inspect.signature(function.get_user_function()).parameters)
    if binding_names != parameters:
        raise SystemExit("Function handler parameters do not match its declared bindings")

values = {**dotenv_values(os.environ["RAG_DEPLOY_ENV_PATH"]), **os.environ}
required = [
    "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_EMBEDDING_DEPLOYMENT",
    "AZURE_OPENAI_SUMMARY_DEPLOYMENT", "AZURE_STORAGE_ACCEPTED_CONTAINER",
    "AZURE_SEARCH_TENANT_METADATA_FIELD",
]
missing = [name for name in required if not str(values.get(name) or "").strip()]
if missing:
    raise SystemExit("Set these values before deployment: " + ", ".join(missing))
settings = {name: values[name] for name in required}
settings.update({
    "AZURE_AUTH_MODE": "managed", "FUNCTIONS_WORKER_RUNTIME": "python",
    "SCM_DO_BUILD_DURING_DEPLOYMENT": "true", "ENABLE_ORYX_BUILD": "true",
    "RAG_TOKENIZER_ENCODING": values.get("RAG_TOKENIZER_ENCODING") or "cl100k_base",
    "RAG_CHUNK_TOKENS": values.get("RAG_CHUNK_TOKENS") or "512",
    "RAG_MAX_BLOB_BYTES": values.get("RAG_MAX_BLOB_BYTES") or "26214400",
    "RAG_MAX_PAGES": values.get("RAG_MAX_PAGES") or "50",
})
if values.get("AZURE_OPENAI_EMBEDDING_DIMENSIONS"):
    settings["AZURE_OPENAI_EMBEDDING_DIMENSIONS"] = values["AZURE_OPENAI_EMBEDDING_DIMENSIONS"]
Path(os.environ["RAG_DEPLOY_SETTINGS_PATH"]).write_text(json.dumps(settings), encoding="utf-8")
'@ | & $python -
    if ($LASTEXITCODE -ne 0) { throw 'Local custom-skill configuration validation failed. No Azure resource changes were requested.' }
    Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $zip -WhatIf:$false -Confirm:$false

    if ((-not $PreflightOnly -or $WhatIfPreference) -and -not $PSCmdlet.ShouldProcess(
        "$ResourceGroup / $FunctionAppName in $Location",
        'Create billable Linux Python 3.12 Consumption Function hosting, assign read/model roles, and deploy the custom skill'
    )) { return }

    $null = Invoke-AzureJson @('account', 'show')
    $sourceStorage = Invoke-AzureJson @('storage', 'account', 'show', '--ids', $SourceStorageResourceId)
    $modelResource = Invoke-AzureJson @('resource', 'show', '--ids', $OpenAIResourceId)
    if ($sourceStorage.publicNetworkAccess -eq 'Disabled') {
        throw 'Source-storage public network access is disabled. The current Linux Consumption Function needs an approved reachable Blob endpoint; private networking requires a compatible hosting design. No Azure resource changes were requested.'
    }
    if ($sourceStorage.networkRuleSet.defaultAction -eq 'Deny') {
        Write-Warning 'Source storage has a selected-network firewall. Verify access from Search and the Function, not just this workstation.'
    }
    if ($modelResource.properties.publicNetworkAccess -eq 'Disabled') {
        throw 'Model public network access is disabled. Configure an approved compatible hosting/network design before deployment. No Azure resource changes were requested.'
    }
    $existingStorage = @()
    $existingApps = @()
    $groupExists = Invoke-AzureJson @('group', 'exists', '--name', $ResourceGroup)
    if ($groupExists -eq $true) {
        $group = Invoke-AzureJson @('group', 'show', '--name', $ResourceGroup)
        if ($group.location -ne $Location) {
            throw 'The existing resource group location differs from Location. Select its existing location or a different approved resource group. No Azure resource changes were requested.'
        }
        $existingStorage = @(Invoke-AzureJson @('storage', 'account', 'list', '--resource-group', $ResourceGroup))
        $existingApps = @(Invoke-AzureJson @('functionapp', 'list', '--resource-group', $ResourceGroup))
    }
    $storage = $existingStorage | Where-Object { $_.name -eq $FunctionStorageAccountName }
    $function = $existingApps | Where-Object { $_.name -eq $FunctionAppName }
    if ($storage -and $storage.tags.ragCustomSkill -ne $FunctionAppName) {
        throw 'Refusing to modify an existing Storage account not owned by this deployment script.'
    }
    if ($function -and $function.tags.ragCustomSkill -ne $FunctionAppName) {
        throw 'Refusing to modify an existing Function App not owned by this deployment script.'
    }
    if ($storage) { Assert-FunctionHostStorage $storage }
    if ($PreflightOnly) {
        Write-Host 'Preflight passed: local package and existing Azure prerequisites checked; no Azure resources changed.'
        Write-Host 'This does not prove runtime reachability, policy approval for new resources, quota availability, or RBAC propagation.'
        return
    }
    $null = Invoke-AzureJson @('group', 'create', '--name', $ResourceGroup, '--location', $Location)
    if (-not $storage) {
        $storage = Invoke-AzureJson @(
            'storage', 'account', 'create', '--resource-group', $ResourceGroup,
            '--name', $FunctionStorageAccountName, '--location', $Location,
            '--sku', 'Standard_LRS', '--kind', 'StorageV2', '--https-only', 'true',
            '--min-tls-version', 'TLS1_2', '--allow-blob-public-access', 'false',
            '--tags', "ragCustomSkill=$FunctionAppName"
        )
        Assert-FunctionHostStorage $storage
    }
    if (-not $function) {
        $null = Invoke-AzureJson @(
            'functionapp', 'create', '--resource-group', $ResourceGroup, '--name', $FunctionAppName,
            '--storage-account', $FunctionStorageAccountName,
            '--consumption-plan-location', $Location, '--os-type', 'Linux',
            '--runtime', 'python', '--runtime-version', '3.12', '--functions-version', '4',
            '--assign-identity', '[system]', '--disable-app-insights', 'true',
            '--https-only', 'true', '--tags', "ragCustomSkill=$FunctionAppName"
        )
    }
    $identity = Invoke-AzureJson @('functionapp', 'identity', 'assign', '--resource-group', $ResourceGroup, '--name', $FunctionAppName)
    foreach ($role in @(
        @{ Name = 'Storage Blob Data Reader'; Scope = $SourceStorageResourceId },
        @{ Name = 'Cognitive Services OpenAI User'; Scope = $OpenAIResourceId }
    )) {
        $null = Invoke-AzureJson @(
            'role', 'assignment', 'create', '--assignee-object-id', $identity.principalId,
            '--assignee-principal-type', 'ServicePrincipal', '--role', $role.Name, '--scope', $role.Scope
        )
    }
    $runtimeSettings = Get-Content -LiteralPath $settingsFile -Raw | ConvertFrom-Json
    $runtimeSettings | Add-Member -NotePropertyName 'AZURE_STORAGE_ACCOUNT_URL' -NotePropertyValue $sourceStorage.primaryEndpoints.blob -Force
    [IO.File]::WriteAllText($settingsFile, ($runtimeSettings | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
    $null = Invoke-AzureJson @('functionapp', 'config', 'appsettings', 'set', '--resource-group', $ResourceGroup, '--name', $FunctionAppName, '--settings', "@$settingsFile")
    $null = Invoke-AzureJson @('functionapp', 'deployment', 'source', 'config-zip', '--resource-group', $ResourceGroup, '--name', $FunctionAppName, '--src', $zip, '--build-remote', 'true')

    $appInfo = Invoke-AzureJson @('functionapp', 'show', '--resource-group', $ResourceGroup, '--name', $FunctionAppName)
    if (-not $appInfo.id) { throw 'Function App resource ID was not returned; cannot synchronize triggers.' }
    $keys = Invoke-AzureJson @('functionapp', 'keys', 'list', '--resource-group', $ResourceGroup, '--name', $FunctionAppName)
    if (-not $keys.functionKeys.default) { throw 'No Function host key was returned; .env was not updated.' }
    $env:RAG_DEPLOY_SKILL_ENDPOINT = "https://$($appInfo.defaultHostName)/api/enrich"
    $env:RAG_DEPLOY_SKILL_KEY = $keys.functionKeys.default
    $responsive = $false
    for ($attempt = 0; $attempt -lt 6; $attempt++) {
        try {
            $null = Invoke-WebRequest -UseBasicParsing -Uri $env:RAG_DEPLOY_SKILL_ENDPOINT `
                -Method Post -ContentType 'application/json' -Body '{"values":[]}' `
                -Headers @{ 'x-functions-key' = $env:RAG_DEPLOY_SKILL_KEY } -TimeoutSec 20
            throw 'The readiness request should have been rejected as an empty custom-skill batch.'
        }
        catch [System.Net.WebException] {
            $response = $_.Exception.Response
            if ($response -and [int]$response.StatusCode -eq 400) {
                $body = $_.ErrorDetails.Message
                if (-not $body) {
                    $reader = [IO.StreamReader]::new($response.GetResponseStream())
                    try { $body = $reader.ReadToEnd() }
                    finally { $reader.Dispose() }
                }
                if ($body -eq 'Expected exactly one custom-skill record') {
                    $responsive = $true
                    break
                }
            }
            if ($attempt -eq 5) { throw 'Function route is not responsive yet. Inspect deployment/host logs before rerunning.' }
            Start-Sleep -Seconds 10
        }
    }
    if (-not $responsive) { throw 'Function readiness could not be verified.' }
    $null = Invoke-AzureJson @('rest', '--method', 'post', '--url', "https://management.azure.com$($appInfo.id)/syncfunctiontriggers?api-version=2024-04-01")
    $registered = $false
    for ($attempt = 0; $attempt -lt 12; $attempt++) {
        $functions = @(Invoke-AzureJson @('functionapp', 'function', 'list', '--resource-group', $ResourceGroup, '--name', $FunctionAppName))
        if ($functions | Where-Object { $_.name -like '*/enrich' }) {
            $registered = $true
            break
        }
        if ($attempt -lt 11) { Start-Sleep -Seconds 10 }
    }
    if (-not $registered) {
        throw 'The route responds, but enrich is not registered after trigger synchronization. Check host logs or Application Insights. Local .env was not updated.'
    }
    @'
import os
from dotenv import set_key
path = os.environ["RAG_DEPLOY_ENV_PATH"]
set_key(path, "AZURE_SEARCH_CUSTOM_SKILL_ENDPOINT", os.environ["RAG_DEPLOY_SKILL_ENDPOINT"])
set_key(path, "AZURE_SEARCH_CUSTOM_SKILL_KEY", os.environ["RAG_DEPLOY_SKILL_KEY"])
'@ | & $python -
    if ($LASTEXITCODE -ne 0) { throw 'Function deployed, but local .env update failed.' }
    Write-Host 'Custom skill deployed; endpoint and key saved to .env without printing the key.'
    Write-Host 'No documents were processed. Allow RBAC propagation, then run script 04 to apply the enriched pipeline.'
    Write-Host 'Existing documents require explicit reset/reprocessing. Source tenant and file_id metadata are mandatory.'
}
finally {
    foreach ($name in @('RAG_DEPLOY_ENV_PATH', 'RAG_DEPLOY_SETTINGS_PATH', 'RAG_DEPLOY_SKILL_ENDPOINT', 'RAG_DEPLOY_SKILL_KEY')) {
        [Environment]::SetEnvironmentVariable($name, $null, 'Process')
    }
    if (Test-Path -LiteralPath $work) { Remove-Item -LiteralPath $work -Recurse -Force -WhatIf:$false -Confirm:$false }
}
