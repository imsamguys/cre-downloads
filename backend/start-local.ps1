param([int]$Port = 8788)
Set-Location -LiteralPath $PSScriptRoot
$defaults = Join-Path $PSScriptRoot '.env.example'
Get-Content -LiteralPath $defaults | ForEach-Object {
  if ($_ -match '^(CRE_SUPABASE_URL|CRE_SUPABASE_PUBLISHABLE_KEY|CRE_WEB_ORIGINS)=(.+)$') {
    if (-not [Environment]::GetEnvironmentVariable($matches[1], 'Process')) {
      [Environment]::SetEnvironmentVariable($matches[1], $matches[2], 'Process')
    }
  }
}
if (-not $env:CRE_SUPABASE_URL -or -not $env:CRE_SUPABASE_PUBLISHABLE_KEY) {
  Write-Error 'Set CRE_SUPABASE_URL and CRE_SUPABASE_PUBLISHABLE_KEY first. See .env.example.'
  exit 1
}
python -m uvicorn cre_local_api:app --host 127.0.0.1 --port $Port
