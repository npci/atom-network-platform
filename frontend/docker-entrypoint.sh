#!/bin/sh
set -e
: "${CONTEXT_PATH:=a2a}"
CONTEXT_PATH=$(echo "$CONTEXT_PATH" | sed 's|^/||;s|/$||')
export CONTEXT_PATH
envsubst '$CONTEXT_PATH' < /etc/nginx/conf.d/default.conf.template \
  > /etc/nginx/conf.d/default.conf
exec nginx -g 'daemon off;'
