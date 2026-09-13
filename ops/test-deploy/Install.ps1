param(
    [Parameter(Mandatory=$true)][string]$ServerRoot,
    [Parameter(Mandatory=$true)][string]$Python,
    [switch]$ConfigureOnly,
    [switch]$StartGateway
)
$ErrorActionPreference = 'Stop'
$server = (Resolve-Path -LiteralPath $ServerRoot).Path
$root = Join-Path $server 'autodeploy'
$source = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../..')).Path
$taskName = 'Lokvetia-Test-Autodeploy'
# A controller that is already running holds the very files this script replaces,
# and it would keep running the old ones afterwards, because a re-registered task
# ignores a second start. Stop it first. An interrupted rollout is recovered on
# the next start: routing returns to the previous release and the attempt is
# recorded as a failure, so no half-activated release is left behind.
$wasRunning = $null -ne (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)
if ($wasRunning) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    $deadline = (Get-Date).AddMinutes(3)
    while ((Get-ScheduledTask -TaskName $taskName).State -eq 'Running' -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 2 }
    if ((Get-ScheduledTask -TaskName $taskName).State -eq 'Running') {
        throw 'The autodeploy controller did not stop; end it before re-installing so it cannot keep running replaced files'
    }
    Write-Output 'Stopped the running controller before replacing the files it runs from.'
}
foreach ($folder in @($root, "$root/runtime", "$root/runtime/progress/scripts", "$root/runtime/progress/src/agent_factory", "$root/public", "$root/secrets")) {
    New-Item -ItemType Directory -Path $folder -Force | Out-Null
}
Copy-Item -LiteralPath "$source/scripts/autodeploy.py" -Destination "$root/controller.py"
Copy-Item -LiteralPath "$source/docs/deploy-dashboard.html" -Destination "$root/public/dashboard.html"
Copy-Item -LiteralPath "$source/docs/progress-dashboard.html" -Destination "$root/public/progress.html"
Copy-Item -LiteralPath "$source/scripts/progress_report.py" -Destination "$root/runtime/progress/scripts/progress_report.py"
foreach ($name in @('__init__.py','backlog.py','progress.py')) {
    Copy-Item -LiteralPath "$source/src/agent_factory/$name" -Destination "$root/runtime/progress/src/agent_factory/$name"
}
foreach ($name in @('serve.py','domain_adapter.py','snapshot.py','gateway.py','Dockerfile.core','Dockerfile.cloud')) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination "$root/runtime/$name"
}
# Which checkout these copies came from. The controller republishes it, so a
# machine that was never re-installed says so on the deployment page instead of
# failing later with a message that reads like a fault in the release.
$revision = ''
try { $revision = (& git -C $source rev-parse HEAD 2>$null) } catch { $revision = '' }
if ($LASTEXITCODE -ne 0) { $revision = '' }
$installed = @{revision=("$revision").Trim();installed_at=(Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ');source=$source}
[IO.File]::WriteAllText("$root/installed.json", ($installed | ConvertTo-Json -Depth 3), [Text.UTF8Encoding]::new($false))
function Write-PrivateSecret([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) {
        $bytes = New-Object byte[] 48
        $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
        try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
        [IO.File]::WriteAllText($Path, [Convert]::ToBase64String($bytes), [Text.UTF8Encoding]::new($false))
    }
}
foreach ($name in @('identity-token.txt','core-sso.txt','cloud-sso.txt')) { Write-PrivateSecret "$root/secrets/$name" }
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls "$root/secrets" /inheritance:r /grant:r "${user}:(OI)(CI)F" 'SYSTEM:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not protect identity credential files' }
if (-not (Test-Path -LiteralPath "$root/secrets/clients.json")) {
    $clients = @{
        core = @{name='Lokvetia Core';origin='https://test.lokvetia.com';secret=[IO.File]::ReadAllText("$root/secrets/core-sso.txt")}
        cloud = @{name='Lokiravia';origin='https://test.lokiravia.com';secret=[IO.File]::ReadAllText("$root/secrets/cloud-sso.txt")}
    }
    [IO.File]::WriteAllText("$root/secrets/clients.json", ($clients | ConvertTo-Json -Depth 5), [Text.UTF8Encoding]::new($false))
}
# The configuration is built on every run, but an existing file belongs to the
# operator: only keys it does not have are added to it. That way a setting
# somebody changed on purpose survives a re-install, while a key a newer release
# needs still arrives without anyone editing JSON by hand.
# The application runs in a container here, and says so in every report:
# a hardware scan from this process describes the container, not a user's PC.
$common = @{LOKVETIA_IDENTITY_ORIGIN='https://id.lokvetia.com';LOKVETIA_IDENTITY_INTERNAL='http://lokvetia-deploy-gateway:8080';LOKVETIA_ORGANIZATION='lokvetia';LOKVETIA_SSO_SECRET_FILE='/run/secrets/sso_client';LOKVETIA_MACHINE_KIND='web_container'}
# Each deployment signs its reports with its own host, never the other one's.
$coreEnv = $common.Clone(); $coreEnv.LOKVETIA_SSO_CLIENT='core'; $coreEnv.LOKVETIA_SSO_ORIGIN='https://test.lokvetia.com'; $coreEnv.LOKVETIA_MACHINE_NAME='test.lokvetia.com'
$cloudEnv = $common.Clone(); $cloudEnv.LOKVETIA_SSO_CLIENT='cloud'; $cloudEnv.LOKVETIA_SSO_ORIGIN='https://test.lokiravia.com'; $cloudEnv.LOKVETIA_MACHINE_NAME='test.lokiravia.com'
$config = @{
    state_root=$root;runtime_bundle="$root/runtime";network='lokvetia-test_default';poll_seconds=60;max_retained_containers_per_project=8;keep_releases=2
    progress=@{projects=@(
        @{id='core';name='Lokvetia Core';repository='HappyMiha/Lokvetia-Core';manifests=@('examples/development-backlog.json','examples/game-creator-backlog.json','examples/autonomous-mission-backlog.json','docs/evolution/backlog.json')},
        @{id='cloud';name='Lokiravia';repository='HappyMiha/Lokiravia';manifests=@('examples/agentfactory-cloud-backlog.json','docs/evolution/backlog.json')}
    )}
    initial_routes=@{
        'test.lokvetia.com'=@{container='lokvetia-test-lokvetia-1';sha='e74cb1a';project='core'}
        'test.lokiravia.com'=@{container='lokvetia-test-lokiravia-1';sha='ff76420';project='cloud'}
    }
    projects=@(
        @{id='identity';name='Lokvetia Account';repository='HappyMiha/Lokvetia-Core';service='identity';host='id.lokvetia.com';volume='lokvetia-identity-data';access_token_file="$root/secrets/identity-token.txt";environment=@{AGENT_FACTORY_API_ACTOR='HappyDucky02-test';LOKVETIA_ORGANIZATION='lokvetia'};secret_mounts=@(@{source="$root/secrets/clients.json";target='/run/secrets/identity_clients'})},
        @{id='core';name='Lokvetia Core';repository='HappyMiha/Lokvetia-Core';service='lokvetia';host='test.lokvetia.com';volume='lokvetia-test_lokvetia-data';access_token_file="$server/secrets/lokvetia-token.txt";environment=$coreEnv;secret_mounts=@(@{source="$root/secrets/core-sso.txt";target='/run/secrets/sso_client'})},
        @{id='cloud';name='Lokiravia';repository='HappyMiha/Lokiravia';service='lokiravia';host='test.lokiravia.com';volume='lokvetia-test_lokiravia-data';access_token_file="$server/secrets/lokiravia-token.txt";environment=$cloudEnv;secret_mounts=@(@{source="$root/secrets/cloud-sso.txt";target='/run/secrets/sso_client'})}
    )
}
$configPath = "$root/config.json"
if (-not (Test-Path -LiteralPath $configPath)) {
    [IO.File]::WriteAllText($configPath, ($config | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
    Write-Output 'Wrote a new controller configuration.'
} else {
    $existing = [IO.File]::ReadAllText($configPath) | ConvertFrom-Json
    $added = New-Object 'System.Collections.Generic.List[string]'
    foreach ($name in @('state_root','runtime_bundle','network','poll_seconds','max_retained_containers_per_project','keep_releases')) {
        if ($null -eq $existing.PSObject.Properties[$name]) {
            $existing | Add-Member -NotePropertyName $name -NotePropertyValue $config[$name]
            $added.Add($name)
        }
    }
    foreach ($project in @($existing.projects)) {
        $wanted = @($config.projects | Where-Object { $_.id -eq $project.id })[0]
        if ($null -eq $wanted -or $null -eq $wanted.environment) { continue }
        if ($null -eq $project.PSObject.Properties['environment']) {
            $project | Add-Member -NotePropertyName 'environment' -NotePropertyValue ([PSCustomObject]@{})
        }
        foreach ($key in @($wanted.environment.Keys)) {
            if ($null -eq $project.environment.PSObject.Properties[$key]) {
                $project.environment | Add-Member -NotePropertyName $key -NotePropertyValue $wanted.environment[$key]
                $added.Add("$($project.id).$key")
            }
        }
    }
    if ($added.Count -gt 0) {
        Copy-Item -LiteralPath $configPath -Destination "$configPath.previous" -Force
        [IO.File]::WriteAllText($configPath, ($existing | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
        Write-Output ('Added to the existing configuration (the previous file is kept beside it): ' + ($added -join ', '))
    } else {
        Write-Output 'The existing configuration already carries every key this release needs.'
    }
}
if ($ConfigureOnly) {
    if ($wasRunning) { Start-ScheduledTask -TaskName $taskName }
    Write-Output 'Configuration and controller files refreshed. No application container was restarted.'
    exit 0
}
& docker volume inspect lokvetia-identity-data *> $null
if ($LASTEXITCODE -ne 0) {
    & docker volume create lokvetia-identity-data | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not create identity data volume' }
    & docker run --rm --network none --user 0 --mount type=volume,source=lokvetia-identity-data,target=/data,volume-nocopy --entrypoint python lokvetia-core:test-e74cb1a -c "import os;os.chown('/data',10001,10001)"
    if ($LASTEXITCODE -ne 0) { throw 'Could not initialize new identity volume' }
}
if ($StartGateway) {
    $routes = [IO.File]::ReadAllText("$root/public/routes.json") | ConvertFrom-Json
    $image = $routes.'test.lokvetia.com'.image
    if (-not $image) { throw 'Run one successful controller cycle before starting the gateway' }
    & docker container inspect lokvetia-deploy-gateway *> $null
    if ($LASTEXITCODE -ne 0) {
        & docker run -d --name lokvetia-deploy-gateway --network lokvetia-test_default --restart unless-stopped --read-only --cap-drop ALL --security-opt no-new-privileges:true --memory 256m --publish 127.0.0.1:8780:8080 --mount "type=bind,source=$root/public,target=/state,readonly" --entrypoint python $image /app/gateway.py
        if ($LASTEXITCODE -ne 0) { throw 'Could not start streaming gateway' }
    }
}
$runner = @"
`$ErrorActionPreference = 'Stop'
& '$Python' '$root/controller.py' --config '$root/config.json' --watch *>> '$root/controller.log'
exit `$LASTEXITCODE
"@
[IO.File]::WriteAllText("$root/Run-Controller.ps1", $runner, [Text.UTF8Encoding]::new($false))
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -File `"$root/Run-Controller.ps1`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -User $user -RunLevel Limited -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Output ("Autodeploy controller running from " + $(if ($installed.revision) { $installed.revision.Substring(0, [Math]::Min(12, $installed.revision.Length)) } else { 'an unrecorded revision' }) + ". Applications and workers were not restarted.")
