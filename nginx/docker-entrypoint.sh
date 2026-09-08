#!/bin/sh
set -e
: "${AUTHORITY_CONTEXT:=a2a}"
: "${PARTNER_CONTEXT:=a2a-partner}"
: "${CERTSIM_CONTEXT:=a2a-certsim}"
# Fail closed. This used to default to the literal "dev-internal-token" — the
# exact value app/core/config.py blocklists as _LEGACY_DEV_INTERNAL_TOKEN, and a
# published one. docker-compose.yml passes it with `:?`, but anyone running this
# entrypoint directly got a working credential baked in from a public file.
if [ -z "${CERTSIM_INTERNAL_TOKEN:-}" ]; then
  echo "ERROR: CERTSIM_INTERNAL_TOKEN must be set — it authenticates the internal" >&2
  echo "       simulator path and has no safe default." >&2
  exit 1
fi
: "${APISIM_CONTEXT:=a2a-apisim}"
# WIRE-PINNED: the two default path segments below are deployed URLs a partner
# install already calls, so changing the VALUE is a routing change that 404s
# every in-flight integration — only the setting names are renameable.
#
# The old BANKAGENT_/BANKSIM_ names are therefore still ACCEPTED as an input
# fallback, so an existing install that sets them keeps routing. Nothing
# downstream reads them any more: the templates consume PARTNERAGENT_CONTEXT /
# PARTNERSIM_CONTEXT.
: "${PARTNERAGENT_CONTEXT:=${BANKAGENT_CONTEXT:-a2a-bankagent}}"
: "${PARTNERSIM_CONTEXT:=${BANKSIM_CONTEXT:-a2a-banksim}}"
export AUTHORITY_CONTEXT PARTNER_CONTEXT CERTSIM_CONTEXT CERTSIM_INTERNAL_TOKEN APISIM_CONTEXT
export PARTNERAGENT_CONTEXT PARTNERSIM_CONTEXT
envsubst '$AUTHORITY_CONTEXT $PARTNER_CONTEXT $CERTSIM_CONTEXT $CERTSIM_INTERNAL_TOKEN $APISIM_CONTEXT $PARTNERAGENT_CONTEXT $PARTNERSIM_CONTEXT' \
  < /etc/nginx/conf.d/default.conf.template \
  > /etc/nginx/conf.d/default.conf
exec nginx -g 'daemon off;'
