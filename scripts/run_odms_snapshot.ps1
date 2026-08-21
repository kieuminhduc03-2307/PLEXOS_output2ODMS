param(
    [Parameter(Mandatory=$true)][string]$OperatingSnapshot,
    [Parameter(Mandatory=$true)][string]$ResponseJson,
    [string]$Server = '.\SQLEXPRESS',
    [string]$Model = 'RTS-GMLC',
    [switch]$StoreSV,
    [ValidateSet('AuditOnly','SwingBus')][string]$MismatchDistribution = 'SwingBus',
    [double]$ReadbackToleranceMW = 0.0001,
    [double]$PostflightBalanceToleranceMW = 0.001
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$worker = Join-Path $PSScriptRoot 'odms_dispatch_worker.py'
$requestPath = [System.IO.Path]::ChangeExtension($ResponseJson, '.request.json')
$request = @{
    repo_root = $repoRoot
    operating_snapshot = [System.IO.Path]::GetFullPath($OperatingSnapshot)
    response_json = [System.IO.Path]::GetFullPath($ResponseJson)
    store_sv = [bool]$StoreSV
    readback_tolerance_mw = $ReadbackToleranceMW
    postflight_balance_tolerance_mw = $PostflightBalanceToleranceMW
    mismatch_distribution = $MismatchDistribution
}
$request | ConvertTo-Json | Set-Content -LiteralPath $requestPath -Encoding UTF8

$odmsDirectory = 'C:\Program Files\PTI\ODMS 14.2'
$odmsExecutable = Join-Path $odmsDirectory 'ODMS.exe'
$python313 = 'C:\Users\Duc\AppData\Local\Programs\Python\Python313'
$savedPath = $env:Path
try {
    $env:Path = "$python313;$python313\DLLs;$savedPath"
    $arguments = "server=$Server model=$Model script=`"$worker`" script_params=`"$requestPath`" hide_gui"
    $process = Start-Process -FilePath $odmsExecutable -ArgumentList $arguments `
        -WorkingDirectory $odmsDirectory -WindowStyle Hidden -PassThru -Wait
    if ($process.ExitCode -ne 0) {
        throw "ODMS process failed with exit code $($process.ExitCode)"
    }
}
finally {
    $env:Path = $savedPath
}
Get-Content -Raw -LiteralPath $ResponseJson
