param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$Workspace,
    [int]$CorePort = 8765,
    [int]$CreatorPort = 8767,
    [switch]$WithLokiravia
)
$ErrorActionPreference = 'Stop'
$pythonPath = (Resolve-Path -LiteralPath $Python).Path
New-Item -ItemType Directory -Force -Path $Workspace | Out-Null
$studioRoot = (Resolve-Path -LiteralPath $Workspace).Path
if ($WithLokiravia -and $CorePort -eq $CreatorPort) { throw 'Core and Lokiravia need different ports.' }
foreach ($port in @($CorePort) + $(if ($WithLokiravia) { @($CreatorPort) } else { @() })) {
    if ($port -lt 1024 -or $port -gt 65535) { throw 'Use a port from 1024 to 65535.' }
    if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) {
        throw "Port $port is already in use. Existing services were left running."
    }
}
$env:PYTHONUTF8 = '1'
$env:LOKVETIA_MACHINE_KIND = 'this_pc'
$env:LOKVETIA_MACHINE_NAME = $env:COMPUTERNAME
$env:LOKIRAVIA_CORE_WORKSPACE = $studioRoot
$env:LOKIRAVIA_CORE_URL = "http://127.0.0.1:$CorePort"
$coreArguments = '-m agent_factory --workspace "{0}" --db "{0}\.agent-factory\state.db" web --host 127.0.0.1 --port {1}' -f $studioRoot, $CorePort
$coreProcess = Start-Process -FilePath $pythonPath -ArgumentList $coreArguments -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $studioRoot 'core.stdout.log') -RedirectStandardError (Join-Path $studioRoot 'core.stderr.log')
$services = @{core_pid=$coreProcess.Id; core_url="http://127.0.0.1:$CorePort/studio"}
if ($WithLokiravia) {
    $creatorArguments = '-m agentfactory_cloud.brief_web --data "{0}\creator" --port {1}' -f $studioRoot, $CreatorPort
    $creatorProcess = Start-Process -FilePath $pythonPath -ArgumentList $creatorArguments -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $studioRoot 'creator.stdout.log') -RedirectStandardError (Join-Path $studioRoot 'creator.stderr.log')
    $services.creator_pid = $creatorProcess.Id
    $services.creator_url = "http://127.0.0.1:$CreatorPort"
}
$services | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $studioRoot 'local-services.json') -Encoding utf8
$services | ConvertTo-Json
