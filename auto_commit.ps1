param(
    [Parameter(Mandatory = $true)]
    [string]$Content,

    [string[]]$Details = @("NULL"),

    [string]$Remark = "NULL",

    [string[]]$Paths = @()
)

$ErrorActionPreference = "Stop"

function Format-ChangeNumber {
    param([int]$Number)
    return "T11005-{0:D4}" -f $Number
}

function Invoke-Git {
    param([string[]]$Arguments)
    & git @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "git $($Arguments -join ' ') failed with exit code $LASTEXITCODE"
    }
}

function Invoke-GitCapture {
    param([string[]]$Arguments)
    $output = & git @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "git $($Arguments -join ' ') failed with exit code $LASTEXITCODE"
    }
    return $output
}

function Split-Values {
    param([string[]]$Values, [string]$DefaultValue = "")
    $normalized = @()
    foreach ($value in $Values) {
        if ($value -match "\|") { $normalized += $value -split "\|" }
        elseif ($Values.Count -eq 1 -and $value -match ";") { $normalized += $value -split ";" }
        elseif ($Values.Count -eq 1 -and $value -match ",") { $normalized += $value -split "," }
        else { $normalized += $value }
    }
    $normalized = @($normalized | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if (-not $normalized -and $DefaultValue) { return @($DefaultValue) }
    return $normalized
}

$repoRoot = Invoke-GitCapture -Arguments @("rev-parse", "--show-toplevel")
if (-not $repoRoot) { throw "Not a Git repository." }
$repoRoot = $repoRoot | Select-Object -First 1
Set-Location $repoRoot

$statePath = Join-Path $repoRoot ".auto_commit_state.json"
$messagePath = Join-Path ([System.IO.Path]::GetTempPath()) ("T11005_commit_message_{0}.tmp" -f ([guid]::NewGuid().ToString("N")))
$pendingStatus = Invoke-GitCapture -Arguments @("status", "--short")
if (-not $pendingStatus) {
    Write-Host "No changes to commit."
    exit 0
}

$nextNumber = 1
$stateExisted = Test-Path -LiteralPath $statePath
if ($stateExisted) {
    $state = Get-Content -LiteralPath $statePath -Encoding UTF8 -Raw | ConvertFrom-Json
    if ($state.prefix -and $state.prefix -ne "T11005") {
        throw "Unexpected commit prefix in state file: $($state.prefix)"
    }
    if ($state.nextNumber -as [int]) { $nextNumber = [int]$state.nextNumber }
}

$changeNumber = Format-ChangeNumber -Number $nextNumber
$timestamp = Get-Date -Format "yyyy.MM.dd HH:mm:ss"
$normalizedDetails = Split-Values -Values $Details -DefaultValue "NULL"
$normalizedPaths = Split-Values -Values $Paths
$detailLines = @()
for ($index = 0; $index -lt $normalizedDetails.Count; $index++) {
    $detailLines += ("{0}).{1}" -f ($index + 1), $normalizedDetails[$index])
}
$messageLines = @(
    "修改日期： $timestamp",
    "1.修改编号： $changeNumber",
    "2.修改内容： $Content",
    "3.细节："
) + $detailLines + @("4.修改备注： $Remark")

$readmePath = Join-Path $repoRoot "README.md"
if (-not (Test-Path -LiteralPath $readmePath -PathType Leaf)) {
    throw "README.md is required for automatic change records."
}
$readmeOriginal = [System.IO.File]::ReadAllText($readmePath)
$normalizedReadme = $readmeOriginal -replace "`r`n", "`n"
$startMarker = "<!-- AUTO_COMMIT_LOG_START -->"
$endMarker = "<!-- AUTO_COMMIT_LOG_END -->"
$startIndex = $normalizedReadme.IndexOf($startMarker, [System.StringComparison]::Ordinal)
if ($startIndex -lt 0) { throw "README start marker is missing." }
$bodyStart = $startIndex + $startMarker.Length
$endIndex = $normalizedReadme.IndexOf($endMarker, $bodyStart, [System.StringComparison]::Ordinal)
if ($endIndex -lt 0) { throw "README end marker is missing." }
if ($normalizedReadme.IndexOf($startMarker, $bodyStart, [System.StringComparison]::Ordinal) -ge 0) {
    throw "README contains more than one automatic change-record section."
}
$existingReadmeBody = $normalizedReadme.Substring($bodyStart, $endIndex - $bodyStart).Trim([char[]]@("`n"))
$recordHeader = "### $changeNumber"
if (($existingReadmeBody -split "`n") -contains $recordHeader) {
    throw "README already contains change record: $changeNumber"
}
$readmeRecord = (@($recordHeader, "") + $messageLines) -join "`n"
$updatedReadmeBody = if ($existingReadmeBody) {
    $readmeRecord + "`n`n`n`n" + $existingReadmeBody
}
else { $readmeRecord }
$readmeUpdated = $normalizedReadme.Substring(0, $bodyStart) + "`n" +
    $updatedReadmeBody + "`n" + $normalizedReadme.Substring($endIndex)
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

try {
    @{ prefix = "T11005"; nextNumber = $nextNumber + 1 } |
        ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding UTF8
    [System.IO.File]::WriteAllText($readmePath, $readmeUpdated, $utf8NoBom)
    [System.IO.File]::WriteAllLines($messagePath, $messageLines, $utf8NoBom)
    if ($normalizedPaths.Count -gt 0) {
        Invoke-Git -Arguments (@("add", "--") + $normalizedPaths + @($statePath, $readmePath))
    }
    else { Invoke-Git -Arguments @("add", "-A") }
    Invoke-Git -Arguments @("commit", "-F", $messagePath)
    Write-Host "Committed: $changeNumber"
}
catch {
    if ($stateExisted) {
        @{ prefix = "T11005"; nextNumber = $nextNumber } |
            ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding UTF8
    }
    else { Remove-Item -LiteralPath $statePath -Force -ErrorAction SilentlyContinue }
    [System.IO.File]::WriteAllText($readmePath, $readmeOriginal, $utf8NoBom)
    throw
}
finally {
    Remove-Item -LiteralPath $messagePath -Force -ErrorAction SilentlyContinue
}
