<#
.SYNOPSIS
00: Collect local settings for the Azure AI Search examples.
.DESCRIPTION
Prompts for real resource names, endpoints, and trusted metadata settings.
Writes .env next to this script and exports the values to this PowerShell
process. Does not contact Azure, provision resources, or request credentials.
The Python scripts read .env from their working directory.
.PARAMETER Overwrite
Explicitly allow replacement of an existing .env file. Without this switch,
the helper stops rather than overwriting local configuration.
.PARAMETER IncludeUpload
Also collect the Storage account URL, local document path, and destination Blob name.
.EXAMPLE
.\00_configure_environment.ps1
#>
[CmdletBinding()]
param(
    [switch]$Overwrite,
    [switch]$IncludeUpload
)

$ErrorActionPreference = 'Stop'
$envPath = Join-Path $PSScriptRoot '.env'
if ((Test-Path -LiteralPath $envPath) -and -not $Overwrite) {
    throw '.env already exists. Edit it directly, or rerun with -Overwrite to replace it.'
}

$requiredSettings = [ordered]@{
    AZURE_SEARCH_ENDPOINT = 'Azure AI Search HTTPS endpoint'
    AZURE_SEARCH_INDEX = 'Search index name'
    AZURE_OPENAI_ENDPOINT = 'Azure OpenAI HTTPS resource endpoint or full /openai/v1 URL'
    AZURE_OPENAI_EMBEDDING_DEPLOYMENT = 'Existing embedding deployment name'
    AZURE_OPENAI_SUMMARY_DEPLOYMENT = 'Existing chat deployment for page summaries (for example gpt-5.4-nano)'
    AZURE_SEARCH_DATA_SOURCE_NAME = 'Blob data source name'
    AZURE_SEARCH_SKILLSET_NAME = 'Skillset name'
    AZURE_SEARCH_INDEXER_NAME = 'Indexer name'
    AZURE_STORAGE_RESOURCE_ID = 'Storage account ARM resource ID'
    AZURE_STORAGE_ACCEPTED_CONTAINER = 'Accepted Blob container name'
    AZURE_SEARCH_TENANT_METADATA_FIELD = 'Trusted Blob metadata key for tenant ID'
    RAG_TENANT_ID = 'Authorized tenant ID for upload (ingestion reads Blob metadata; script 06 is unfiltered)'
    RAG_QUERY = 'Verification search query'
}
if ($IncludeUpload) {
    $requiredSettings['AZURE_STORAGE_ACCOUNT_URL'] = 'Storage account Blob HTTPS endpoint'
    $requiredSettings['RAG_LOCAL_FILE_PATH'] = 'Approved local document path (for example samples\sample.txt)'
    $requiredSettings['RAG_BLOB_NAME'] = 'Destination Blob name (existing content and metadata will be overwritten)'
}

$settings = [ordered]@{}
Write-Host 'Enter your real resource values. No Azure resources will be created or contacted.'
foreach ($entry in $requiredSettings.GetEnumerator()) {
    $current = [Environment]::GetEnvironmentVariable($entry.Key, 'Process')
    $prompt = $entry.Value
    if (-not [string]::IsNullOrWhiteSpace($current)) {
        $prompt += ' (Enter keeps the existing environment value)'
    }
    do {
        $value = (Read-Host $prompt).Trim()
        if ([string]::IsNullOrWhiteSpace($value)) {
            $value = $current
        }
    } while ([string]::IsNullOrWhiteSpace($value))
    if ($value -match '[\x00-\x1f\x7f]') {
        throw "$($entry.Key) cannot contain control characters."
    }
    $settings[$entry.Key] = $value
}

$defaults = [ordered]@{
    AZURE_AUTH_MODE = 'default'
    RAG_CHUNK_TOKENS = '512'
    RAG_TOKENIZER_ENCODING = 'cl100k_base'
    RAG_MAX_BLOB_BYTES = '26214400'
    RAG_MAX_PAGES = '50'
    RAG_SEARCH_MODE = 'hybrid'
    RAG_TOP_K = '5'
    RAG_USE_SEMANTIC = 'false'
}
foreach ($entry in $defaults.GetEnumerator()) {
    $settings[$entry.Key] = $entry.Value
}
foreach ($name in @('AZURE_CLIENT_ID', 'AZURE_OPENAI_EMBEDDING_DIMENSIONS')) {
    $value = [Environment]::GetEnvironmentVariable($name, 'Process')
    if (-not [string]::IsNullOrWhiteSpace($value)) {
        if ($value -match '[\x00-\x1f\x7f]') {
            throw "$name cannot contain control characters."
        }
        $settings[$name] = $value
    }
}

$lines = @('# Local configuration. Do not commit this file. Existing host variables take precedence.')
foreach ($entry in $settings.GetEnumerator()) {
    $escaped = $entry.Value.Replace('\', '\\').Replace("'", "\'")
    $lines += "$($entry.Key)='$escaped'"
}
[IO.File]::WriteAllLines($envPath, $lines, [Text.UTF8Encoding]::new($false))
foreach ($entry in $settings.GetEnumerator()) {
    [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, 'Process')
}

Write-Host 'Saved .env and populated this PowerShell process. Run scripts from this directory.'
Write-Host 'Script 04 enables daily indexing in batches of 5; language detection defaults to'
Write-Host 'the free 20-document/indexer/day'
Write-Host 'allowance unless AZURE_AI_SERVICES_KEY is already set in your environment.'
Write-Host 'No credentials were requested. Optional paid AI billing and Python ingestion settings'
Write-Host 'are documented in .env.example; they are not populated by this managed-route helper.'
Write-Host 'For optional upload settings, use -IncludeUpload when first running this helper.'
Write-Host 'Next: follow the README managed-route order. Validate script 03 with -WhatIf'
Write-Host 'and -PreflightOnly before deploying the Function.'
