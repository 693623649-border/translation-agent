[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string[]]$DocxPath,

    [string]$OutputDirectory,

    [switch]$Force
)

$ErrorActionPreference = "Stop"

function Resolve-ExplicitDocxPath {
    param([string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw "DOCX path is empty."
    }

    if ($Path.IndexOfAny([char[]]"*?") -ge 0) {
        throw "Wildcards are not allowed: $Path"
    }

    $item = Get-Item -LiteralPath $Path -ErrorAction Stop
    if ($item.PSIsContainer) {
        throw "Expected a DOCX file, got a directory: $Path"
    }

    if ($item.Extension -ine ".docx") {
        throw "Expected a .docx file, got: $($item.FullName)"
    }

    return $item.FullName
}

function Get-WinWordPids {
    @(Get-Process -Name "WINWORD" -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
}

$resolvedDocx = @()
$plannedExports = @()
$existingPids = @()

try {
    foreach ($path in $DocxPath) {
        $resolvedDocx += Resolve-ExplicitDocxPath -Path $path
    }

    if (($resolvedDocx | Select-Object -Unique).Count -ne $resolvedDocx.Count) {
        throw "Duplicate DOCX paths are not allowed."
    }

    if ($OutputDirectory) {
        if ($OutputDirectory.IndexOfAny([char[]]"*?") -ge 0) {
            throw "Wildcards are not allowed in OutputDirectory: $OutputDirectory"
        }
        $outputRoot = (New-Item -ItemType Directory -Path $OutputDirectory -Force).FullName

        $pdfNames = @($resolvedDocx | ForEach-Object { [System.IO.Path]::GetFileNameWithoutExtension($_) + ".pdf" })
        if (($pdfNames | Select-Object -Unique).Count -ne $pdfNames.Count) {
            throw "Multiple DOCX files would export to the same PDF name in OutputDirectory."
        }
    } else {
        $outputRoot = $null
    }

    $plannedExports = @(
        foreach ($docx in $resolvedDocx) {
            $sourceItem = Get-Item -LiteralPath $docx
            $pdfPath = if ($outputRoot) {
                Join-Path $outputRoot ($sourceItem.BaseName + ".pdf")
            } else {
                Join-Path $sourceItem.DirectoryName ($sourceItem.BaseName + ".pdf")
            }
            if ((Test-Path -LiteralPath $pdfPath) -and -not $Force) {
                throw "Output PDF already exists; pass -Force to replace it: $pdfPath"
            }
            [ordered]@{ docx = $sourceItem.FullName; pdf = $pdfPath }
        }
    )

    $existingPids = @(Get-WinWordPids)
} catch {
    [ordered]@{
        ok = $false
        error = $_.Exception.Message
        preexisting_winword_pids = $existingPids
        observed_new_winword_pids = @()
        created_winword_pid = $null
        exports = @()
        forced_process_cleanup = $false
    } | ConvertTo-Json -Depth 8
    exit 1
}

$word = $null
$wordPid = $null
$createdPids = @()
$mayQuitWordApplication = $false
$ownsWordProcess = $false
$exports = New-Object System.Collections.Generic.List[object]
$result = $null
$scriptExitCode = 0
$forcedProcessCleanup = $false

try {
    $word = New-Object -ComObject Word.Application
    foreach ($attempt in 1..30) {
        $createdPids = @(
            Get-WinWordPids | Where-Object { $existingPids -notcontains $_ }
        )
        if ($createdPids.Count -gt 0) {
            break
        }
        Start-Sleep -Milliseconds 100
    }
    if ($createdPids.Count -eq 0) {
        throw "Word COM did not create a new WINWORD process; refusing to control a pre-existing instance."
    }
    $mayQuitWordApplication = $true
    if ($createdPids.Count -ne 1) {
        throw "Expected one new WINWORD process, observed $($createdPids.Count): $($createdPids -join ', ')."
    }
    $ownsWordProcess = $true
    $wordPid = $createdPids[0]
    $word.Visible = $false
    $word.DisplayAlerts = 0

    foreach ($planned in $plannedExports) {
        $document = $null
        try {
            $document = $word.Documents.Open([string]$planned.docx, $false, $true, $false)
            $document.ExportAsFixedFormat([string]$planned.pdf, 17)
            $exports.Add([ordered]@{
                docx = $planned.docx
                pdf = (Get-Item -LiteralPath $planned.pdf).FullName
                ok = $true
            })
        } finally {
            if ($null -ne $document) {
                $document.Close($false)
                [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($document)
            }
        }
    }

    $result = [ordered]@{
        ok = $true
        renderer = "Microsoft Word COM"
        preexisting_winword_pids = $existingPids
        created_winword_pid = $wordPid
        exports = $exports
    }
} catch {
    $scriptExitCode = 1
    $result = [ordered]@{
        ok = $false
        error = $_.Exception.Message
        preexisting_winword_pids = $existingPids
        observed_new_winword_pids = $createdPids
        created_winword_pid = $wordPid
        exports = $exports
    }
} finally {
    if ($null -ne $word -and $mayQuitWordApplication) {
        try {
            $word.Quit()
        } catch {
        }
    }
    if ($null -ne $word) {
        [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($word)
    }

    [System.GC]::Collect()
    [System.GC]::WaitForPendingFinalizers()

    if ($ownsWordProcess) {
        foreach ($createdPid in $createdPids) {
            Wait-Process -Id $createdPid -Timeout 10 -ErrorAction SilentlyContinue
            if (Get-Process -Id $createdPid -ErrorAction SilentlyContinue) {
                Stop-Process -Id $createdPid -Force -ErrorAction SilentlyContinue
                Wait-Process -Id $createdPid -Timeout 5 -ErrorAction SilentlyContinue
                $forcedProcessCleanup = $true
            }
            if (Get-Process -Id $createdPid -ErrorAction SilentlyContinue) {
                $scriptExitCode = 1
                $result["ok"] = $false
                $result["error"] = "The owned WINWORD process did not exit: $createdPid"
            }
        }
    }
}

$result["forced_process_cleanup"] = $forcedProcessCleanup
$result | ConvertTo-Json -Depth 8
if ($scriptExitCode -ne 0) {
    exit $scriptExitCode
}
