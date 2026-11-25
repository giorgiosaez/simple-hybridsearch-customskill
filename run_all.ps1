<#
.SYNOPSIS
Run the numbered managed-indexing examples from setup through search.
.DESCRIPTION
Prepares missing local dependencies and configuration, validates deployment,
pauses any existing indexer, uploads an approved document, creates the index,
deploys summary and numbering functions in one app, configures indexing, waits for a new successful run, and
searches. Stops on errors or indexing warnings. Prints phase status, not settings.
Creates billable Azure resources and overwrites the selected Blob. Uses existing
process environment values before .env. Search output contains document passages.
If a later step fails, earlier changes persist and the indexer may remain paused.
.PARAMETER SkipUpload
Use an existing approved Blob with tenant/file metadata instead of step 01.
The Search indexer still reads and processes the configured Blob container.
This does not skip ingestion and does not invoke the manual indexing alternative.
.PARAMETER TimeoutSeconds
Maximum wait for each indexer pause/completion phase; defaults to 1800 seconds.
.PARAMETER ResourceGroup
Resource group for Function hosting, passed to step 03.
.PARAMETER Location
Azure region for Function hosting, passed to step 03.
.PARAMETER FunctionAppName
Globally unique Function App name, passed to step 03.
.PARAMETER FunctionStorageAccountName
Separate host-storage account name, passed to step 03.
.PARAMETER OpenAIResourceId
ARM resource ID hosting the summary deployment, passed to step 03.
.PARAMETER SubscriptionId
Azure subscription for deployment. If omitted, use the signed-in CLI subscription.
.EXAMPLE
.\run_all.ps1 @deployment -SkipUpload -WhatIf
.EXAMPLE
.\run_all.ps1 @deployment -SkipUpload
#>
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [Parameter(Mandatory = $true)][string]$ResourceGroup,
    [Parameter(Mandatory = $true)][string]$Location,
    [Parameter(Mandatory = $true)][string]$FunctionAppName,
    [Parameter(Mandatory = $true)][string]$FunctionStorageAccountName,
    [Parameter(Mandatory = $true)][string]$OpenAIResourceId,
    [string]$SubscriptionId,
    [switch]$SkipUpload,
    [ValidateRange(1, 86400)][int]$TimeoutSeconds = 1800
)

$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$phase = 'planning'
$started = Get-Date

function Write-Status {
    param([string]$Status, [string]$Message)
    Write-Host ("[{0:HH:mm:ss}] [{1}] {2}" -f (Get-Date), $Status, $Message)
}

function Invoke-Python {
    param([string[]]$Arguments)
    & $python @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python step failed (exit $LASTEXITCODE). Review the preceding sanitized error."
    }
}

function Invoke-Example {
    param([string]$Name, [string[]]$Arguments = @())
    Write-Status 'RUNNING' $Name
    Invoke-Python -Arguments (@((Join-Path $PSScriptRoot "rag_search\$Name")) + $Arguments)
    Write-Status 'OK' $Name
}

Write-Status 'PLAN' 'Setup -> validate -> pause -> upload -> index -> deploy -> configure -> wait -> search'
Write-Status 'NOTICE' 'Billable operations; upload overwrites the selected Blob; search prints source passages.'
if (-not $PSCmdlet.ShouldProcess(
    'Configured Azure resources and accepted-document container',
    'Run the full managed-indexing workflow, including hosting deployment and document processing'
)) { return }

Push-Location $PSScriptRoot
try {
    $phase = 'local setup'
    Write-Status 'RUNNING' $phase
    if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
        throw 'Install Azure CLI and run az login before continuing.'
    }
    & az account show --only-show-errors --output none
    if ($LASTEXITCODE -ne 0) { throw 'Azure CLI sign-in is required. Run az login, then retry.' }
    if (-not (Test-Path -LiteralPath $python)) {
        & python -m venv (Join-Path $PSScriptRoot '.venv')
        if ($LASTEXITCODE -ne 0) { throw 'Creating .venv failed. Install Python 3.12 and retry.' }
    }
    & $python -c 'import sys; sys.exit(sys.version_info < (3, 12))'
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 or newer is required.' }
    & $python -c 'import azure.functions, azure.identity, azure.search.documents, azure.storage.blob, azure.ai.textanalytics, openai, pypdf, bs4, tiktoken, dotenv'
    if ($LASTEXITCODE -ne 0) {
        Write-Status 'RUNNING' 'Installing missing project dependencies'
        Invoke-Python -Arguments @('-m', 'pip', 'install', '-c', 'constraints.txt', '-e', '.[function]')
    }
    if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot '.env'))) {
        Write-Status 'INPUT' '00_configure_environment.ps1 will collect your resource settings.'
        & (Join-Path $PSScriptRoot 'rag_search\00_configure_environment.ps1') -IncludeUpload:(-not $SkipUpload)
    } else {
        Write-Status 'OK' 'Reusing .env without overwriting it; existing process variables take precedence.'
    }

    $deployment = @{
        ResourceGroup = $ResourceGroup
        Location = $Location
        FunctionAppName = $FunctionAppName
        FunctionStorageAccountName = $FunctionStorageAccountName
        OpenAIResourceId = $OpenAIResourceId
    }
    if ($SubscriptionId) { $deployment.SubscriptionId = $SubscriptionId }

    $phase = '03 deployment preflight (read-only Azure checks)'
    Write-Status 'RUNNING' $phase
    & (Join-Path $PSScriptRoot 'rag_search\03_deploy_custom_skill.ps1') @deployment -PreflightOnly -Confirm:$false
    Write-Status 'OK' $phase

    $phase = 'pause existing indexing before changing documents or deployment'
    Invoke-Example '05_run_indexer.py' @('--pause', '--timeout-seconds', "$TimeoutSeconds")

    $phase = '01 upload'
    if ($SkipUpload) { Write-Status 'SKIPPED' '01 upload; using existing approved Blob metadata.' }
    else { Invoke-Example '01_upload_document.py' }

    $phase = '02 index definition'
    Invoke-Example '02_create_index.py'

    $phase = '03 shared summary and numbering Function App deployment'
    Write-Status 'RUNNING' $phase
    & (Join-Path $PSScriptRoot 'rag_search\03_deploy_custom_skill.ps1') @deployment -Confirm:$false
    Write-Status 'OK' $phase

    $phase = '04 managed indexing configuration'
    Invoke-Example '04_create_indexer.py'

    $phase = '05 wait for a new successful indexer run'
    Invoke-Example '05_run_indexer.py' @('--wait', '--timeout-seconds', "$TimeoutSeconds")

    $phase = '06 search'
    Invoke-Example '06_search.py'
    Write-Status 'COMPLETE' ("Workflow finished in {0:N1} minutes. Verify the intended document and retrieved IDs." -f ((Get-Date) - $started).TotalMinutes)
}
catch {
    Write-Status 'FAILED' $phase
    Write-Status 'ACTION' 'No rollback was attempted. If step 04 was not reached, an existing indexer may remain disabled. Review the error before rerunning.'
    throw
}
finally {
    Pop-Location
}
