#!/usr/bin/env python3
"""Run an operator SQL client with verified TLS; accepts no credentials itself."""
import os
from pathlib import Path
import sys

ca = Path(__file__).resolve().parents[2] / 'secrets/postgresql-ha/sql-pki/ca.crt'
if len(sys.argv) < 2 or not ca.is_file():
    raise SystemExit('Usage: with-sql-tls.py COMMAND [ARGS...]; restore SQL CA first')
env = dict(os.environ, PGSSLMODE='verify-full', PGSSLROOTCERT=str(ca), NODE_EXTRA_CA_CERTS=str(ca))
# PGSSLROOTCERT is for libpq. Pure-JS pg reads PGSSLMODE and Node's startup CA trust.
# pg passes DNS hosts as TLS servername; use the existing pinned node host aliases.
aliases = {'10.4.4.2': 's1', '10.4.4.8': 's2', '10.4.4.5': 's3'}
for name in ['PGHOST', 'CONSUMER_PGHOST']:
    if env.get(name) in aliases:
        env[name] = aliases[env[name]]
os.execvpe(sys.argv[1], sys.argv[1:], env)
