#!/bin/sh
set -eu
# This value is deliberately public synthetic verification data, never a secret.
printf '%s\n' "${POCKETDEPLOY_TEST:-initial}" > /usr/share/nginx/html/generation
