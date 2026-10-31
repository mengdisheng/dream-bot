param(
    [Parameter(Mandatory = $true)] [string] $HostAlias
)
$ErrorActionPreference = 'Stop'
# Use an existing, verified OpenSSH host alias. Authentication stays in SSH.
& ssh.exe -N -L '127.0.0.1:18443:127.0.0.1:8443' -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o StrictHostKeyChecking=yes $HostAlias
exit $LASTEXITCODE
