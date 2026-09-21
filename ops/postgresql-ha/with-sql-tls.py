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
os.execvpe(sys.argv[1], sys.argv[1:], env)
