#!/bin/sh
set -e
: "${NPCI_CONTEXT:=a2a}"
: "${PARTNER_CONTEXT:=a2a-partner}"
: "${CERTSIM_CONTEXT:=a2a-certsim}"
: "${CERTSIM_INTERNAL_TOKEN:=dev-internal-token}"
: "${APISIM_CONTEXT:=a2a-apisim}"
: "${BANKAGENT_CONTEXT:=a2a-bankagent}"
: "${BANKSIM_CONTEXT:=a2a-banksim}"
export NPCI_CONTEXT PARTNER_CONTEXT CERTSIM_CONTEXT CERTSIM_INTERNAL_TOKEN APISIM_CONTEXT BANKAGENT_CONTEXT BANKSIM_CONTEXT
envsubst '$NPCI_CONTEXT $PARTNER_CONTEXT $CERTSIM_CONTEXT $CERTSIM_INTERNAL_TOKEN $APISIM_CONTEXT $BANKAGENT_CONTEXT $BANKSIM_CONTEXT' \
  < /etc/nginx/conf.d/default.conf.template \
  > /etc/nginx/conf.d/default.conf
exec nginx -g 'daemon off;'
