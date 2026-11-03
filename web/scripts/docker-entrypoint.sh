#!/bin/sh
# Container entrypoint: bring the better-auth schema up to date, then run the Next.js server as PID 1 (exec) so
# SIGTERM from `docker stop` reaches it. A failed migration stops the container before it serves traffic.
set -eu
node --disable-warning=MODULE_TYPELESS_PACKAGE_JSON scripts/migrate.mjs
exec node server.js
