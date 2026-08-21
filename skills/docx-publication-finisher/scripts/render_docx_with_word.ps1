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

if (-not ("NativeWordProcess" -as [type])) {
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

public static class NativeWordProcess
{
    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
}
"@
}

function Get-WordApplicationProcessId {
    param([object]$Application)

    [uint32]$processId = 0
    [void][NativeWordProcess]::GetWindowThreadProcessId(
        [IntPtr]$Application.Hwnd,
        [ref]$processId
    )
    if ($processId -eq 0) {
        throw "Could not resolve the WINWORD process for the COM application."
    }
    return [int]$processId
}

$resolvedDocx = @()
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

$existingPids = @(Get-WinWordPids)
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

$word = $null
$wordPid = $null
$ownsWordProcess = $false
$exports = New-Object System.Collections.Generic.List[object]

try {
    $word = New-Object -ComObject Word.Application
    $wordPid = Get-WordApplicationProcessId -Application $word
    if ($existingPids -contains $wordPid) {
        throw "Word COM reused an existing WINWORD process; refusing to control PID $wordPid."
    }
    $ownsWordProcess = $true
    $word.Visible = $false
    $word.DisplayAlerts = 0

    foreach ($planned in $plannedExports) {
        $document = $null
        try {
            $document = $word.Documents.Open($planned.docx, $false, $true, $false)
            $document.ExportAsFixedFormat($planned.pdf, 17)
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

    [ordered]@{
        ok = $true
        renderer = "Microsoft Word COM"
        preexisting_winword_pids = $existingPids
        created_winword_pid = $wordPid
        exports = $exports
    } | ConvertTo-Json -Depth 8
} catch {
    [ordered]@{
        ok = $false
        error = $_.Exception.Message
        preexisting_winword_pids = $existingPids
        created_winword_pid = $wordPid
        exports = $exports
    } | ConvertTo-Json -Depth 8
    exit 1
} finally {
    if ($null -ne $word -and $ownsWordProcess) {
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
}
