#!/usr/bin/env python3
"""Prepare encrypted S2 repository and S1/S3 clients; never restart PG or restore in place."""
import json
from pathlib import Path
import secrets
import shlex
import subprocess

ROOT = Path(__file__).resolve().parents[2]
PRIVATE = ROOT / 'secrets/backup'
PRIVATE.mkdir(mode=0o700, exist_ok=True)
SSH = ['ssh', '-i', '/home/ubuntu/.ssh/id_ed25519_crawl_infra', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10']
S1, S2, S3 = '10.4.4.2', '10.4.4.8', '10.4.4.5'

def run(ip, command, data=None, capture=False):
    return subprocess.run(SSH + ['ubuntu@' + ip, command], input=data, text=True,
                          check=True, capture_output=capture)

def put(ip, path, content, owner='root', mode=0o600):
    writer = '''import json,os,pwd,sys,tempfile
d=json.load(sys.stdin); parent=os.path.dirname(d['path']); os.makedirs(parent,exist_ok=True)
u=pwd.getpwnam(d['owner']); fd,tmp=tempfile.mkstemp(dir=parent)
try:
 os.fchmod(fd,d['mode']); os.fchown(fd,u.pw_uid,u.pw_gid)
 with os.fdopen(fd,'w') as f: f.write(d['content']); f.flush(); os.fsync(f.fileno())
 os.replace(tmp,d['path'])
finally:
 if os.path.exists(tmp): os.unlink(tmp)
'''
    run(ip, 'sudo -n python3 -c ' + shlex.quote(writer), json.dumps(dict(path=path, content=content, owner=owner, mode=mode)))

def key(name):
    path = PRIVATE / name
    if not path.exists():
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', name, '-f', str(path)], check=True)
    path.chmod(0o600)
    return path.read_text(), path.with_suffix('.pub').read_text().strip()

def authorize(ip, user, public, sources):
    script = '''import json,pwd,pathlib,os,sys
d=json.load(sys.stdin); u=pwd.getpwnam(d['user']); directory=pathlib.Path(u.pw_dir)/'.ssh'
directory.mkdir(mode=0o700,exist_ok=True); os.chown(directory,u.pw_uid,u.pw_gid)
p=directory/'authorized_keys'; old=p.read_text() if p.exists() else ''
line='restrict,from="'+d['sources']+'",command="/usr/local/libexec/crawl-pgbackrest-remote" '+d['public']
if line not in old.splitlines(): p.write_text(old.rstrip()+'\\n'+line+'\\n')
p.chmod(0o600); os.chown(p,u.pw_uid,u.pw_gid)
'''
    run(ip, 'sudo -n python3 -c ' + shlex.quote(script), json.dumps(dict(user=user, public=public, sources=sources)))

if __name__ == '__main__':
    cipher = PRIVATE / 'pgbackrest-cipher-pass'
    if not cipher.exists(): cipher.write_text(secrets.token_hex(32) + '\n')
    cipher.chmod(0o600)
    password = cipher.read_text().strip()
    assert len(password) == 64 and all(c in '0123456789abcdef' for c in password)
    keys = {name: key('pgbackrest-' + name) for name in ['s1', 'repo', 's3']}
    run(S2, "id pgbackrest >/dev/null 2>&1 || sudo -n useradd --system --create-home --home-dir /var/lib/pgbackrest --shell /bin/bash pgbackrest")
    run(S2, 'sudo -n install -d -m 0750 -o pgbackrest -g pgbackrest /var/lib/pgbackrest')
    fingerprints = {ip: run(ip, 'cat /etc/ssh/ssh_host_ed25519_key.pub', capture=True).stdout.strip() for ip in [S1, S2, S3]}
    common = f'''[global]
repo1-path=/srv/crawlsystem/backups/pgbackrest
repo1-cipher-type=aes-256-cbc
repo1-cipher-pass={password}
repo1-retention-full=2
repo1-retention-diff=6
compress-type=gz
compress-level=3
process-max=1
start-fast=y
log-level-console=info
log-level-file=off
archive-timeout=120
'''
    for ip, name, user, home in [(S1, 's1', 'postgres', '/var/lib/postgresql'),
                                  (S2, 'repo', 'pgbackrest', '/var/lib/pgbackrest'),
                                  (S3, 's3', 'postgres', '/var/lib/postgresql')]:
        run(ip, f"sudo -n install -d -m 0700 -o {user} -g {user} {home}/.ssh /var/lib/pgbackrest-lock-{name}")
        put(ip, '/usr/local/libexec/crawl-pgbackrest-remote', (ROOT / 'ops/backup/pgbackrest-remote.py').read_text(), mode=0o755)
        keypath = home + '/.ssh/id_ed25519_crawl_pgbackrest'
        put(ip, keypath, keys[name][0], user)
        # Dedicated config file avoids overwriting any existing postgres SSH settings.
        dest = S1 if name == 'repo' else S2
        targetuser = 'postgres' if name == 'repo' else 'pgbackrest'
        put(ip, home + '/.ssh/crawl_pgbackrest_known_hosts', dest + ' ' + fingerprints[dest] + '\n', user)
        sshwrapper = f'''#!/bin/sh
exec /usr/bin/ssh -o BatchMode=yes -o ConnectTimeout=10 -o IdentitiesOnly=yes -o IdentityFile={keypath} -o UserKnownHostsFile={home}/.ssh/crawl_pgbackrest_known_hosts -o StrictHostKeyChecking=yes "$@"
'''
        put(ip, '/usr/local/libexec/crawl-pgbackrest-ssh', sshwrapper, mode=0o755)
        cfg = common + f'cmd-ssh=/usr/local/libexec/crawl-pgbackrest-ssh\nlock-path=/var/lib/pgbackrest-lock-{name}\n'
        if name == 'repo':
            cfg += '\n[crawler]\npg1-host=' + S1 + '\npg1-host-user=postgres\npg1-path=/var/lib/postgresql/17/main\n'
            run(ip, 'sudo -n install -d -m 0700 -o pgbackrest -g pgbackrest /srv/crawlsystem/backups/pgbackrest')
        else:
            cfg += '\nrepo1-host=' + S2 + '\nrepo1-host-user=pgbackrest\n\n[crawler]\npg1-path=/var/lib/postgresql/17/main\n'
        put(ip, '/etc/pgbackrest/pgbackrest.conf', cfg, user)
    authorize(S1, 'postgres', keys['repo'][1], S2)
    authorize(S2, 'pgbackrest', keys['s1'][1], S1)
    authorize(S2, 'pgbackrest', keys['s3'][1], S3)
    print('Encrypted repository and restricted SSH clients prepared; PG not restarted.')
