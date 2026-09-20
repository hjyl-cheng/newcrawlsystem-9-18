#!/usr/bin/python3
"""Forced SSH command: only permit pgBackRest's remote protocol, never a shell."""
import os
import shlex

args = shlex.split(os.environ.get('SSH_ORIGINAL_COMMAND', ''))
if not args or args[0] not in ('pgbackrest', '/usr/bin/pgbackrest'):
    raise SystemExit('Only pgBackRest remote protocol is permitted')
if not args[-1].endswith(':remote') or any(not a.startswith('--') for a in args[1:-1]):
    raise SystemExit('Invalid pgBackRest remote command')
os.execv('/usr/bin/pgbackrest', ['/usr/bin/pgbackrest', *args[1:]])
