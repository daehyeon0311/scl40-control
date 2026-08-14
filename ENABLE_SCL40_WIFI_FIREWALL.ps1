$ErrorActionPreference = 'Stop'
$ruleName = 'SCL-40 GUI WiFi 8765'

$existing = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
if ($existing) {
    $existing | Remove-NetFirewallRule
}

New-NetFirewallRule `
    -DisplayName $ruleName `
    -Description 'Allow SCL-40 GUI only from the local Wi-Fi subnet to this PC Wi-Fi address' `
    -Direction Inbound `
    -Action Allow `
    -Enabled True `
    -Profile Any `
    -Protocol TCP `
    -LocalAddress '172.30.148.225' `
    -LocalPort 8765 `
    -RemoteAddress '172.30.148.0/24'

Write-Host ''
Write-Host 'SCL-40 GUI firewall rule enabled.' -ForegroundColor Green
Write-Host 'Remote URL: http://172.30.148.225:8765/'
Write-Host 'Allowed source subnet: 172.30.148.0/24'
Read-Host 'Press Enter to close'
