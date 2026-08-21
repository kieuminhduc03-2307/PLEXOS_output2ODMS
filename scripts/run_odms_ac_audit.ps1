param(
    [Parameter(Mandatory=$true)][string]$ResponseJson,
    [string]$Server = '.\SQLEXPRESS',
    [string]$Model = 'RTS-GMLC'
)

$ErrorActionPreference = 'Stop'
$responseDirectory = Split-Path -Parent ([System.IO.Path]::GetFullPath($ResponseJson))
New-Item -ItemType Directory -Force -Path $responseDirectory | Out-Null
$worker = Join-Path $PSScriptRoot 'odms_ac_audit_worker.py'
$requestPath = [System.IO.Path]::ChangeExtension($ResponseJson, '.request.json')
$request = @{
    response_json = [System.IO.Path]::GetFullPath($ResponseJson)
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
