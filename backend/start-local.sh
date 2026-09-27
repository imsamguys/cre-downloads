#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
if [ -f .env.example ]; then
  while IFS='=' read -r key value; do
    case "$key" in
      CRE_SUPABASE_URL) CRE_SUPABASE_URL="${CRE_SUPABASE_URL:-$value}" ;;
      CRE_SUPABASE_PUBLISHABLE_KEY) CRE_SUPABASE_PUBLISHABLE_KEY="${CRE_SUPABASE_PUBLISHABLE_KEY:-$value}" ;;
      CRE_WEB_ORIGINS) CRE_WEB_ORIGINS="${CRE_WEB_ORIGINS:-$value}" ;;
    esac
  done < .env.example
  export CRE_SUPABASE_URL CRE_SUPABASE_PUBLISHABLE_KEY CRE_WEB_ORIGINS
fi
: "${CRE_SUPABASE_URL:?Set CRE_SUPABASE_URL}"
: "${CRE_SUPABASE_PUBLISHABLE_KEY:?Set CRE_SUPABASE_PUBLISHABLE_KEY}"
exec python -m uvicorn cre_local_api:app --host 127.0.0.1 --port "${CRE_PORT:-8788}"
