$ErrorActionPreference = "Stop"
$creApp = Join-Path $PSScriptRoot "app.py"

if (-not (Test-Path -LiteralPath $creApp)) {
    throw "CRE app.py was not found at $creApp"
}

python -m streamlit run $creApp --server.address=127.0.0.1 --server.headless=true --server.enableXsrfProtection=true --server.enableCORS=true
