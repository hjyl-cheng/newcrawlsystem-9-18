#!/usr/bin/env python3
"""Extend the existing encrypted repository to every possible primary; no PG restart."""
from pathlib import Path
import runpy

ROOT = Path(__file__).resolve().parents[2]
m = runpy.run_path(str(ROOT / 'ops/backup/bootstrap-pgbackrest.py'))
run, put, key, authorize = (m[x] for x in ('run', 'put', 'key', 'authorize'))
NODES = {'s1': m['S1'], 's2': m['S2'], 's3': m['S3']}

if __name__ == '__main__':
    keys = {name: key('pgbackrest-' + name) for name in [*NODES, 'repo']}
    fingerprints = {ip: run(ip, 'cat /etc/ssh/ssh_host_ed25519_key.pub', capture=True).stdout.strip() for ip in NODES.values()}
    password = (m['PRIVATE'] / 'pgbackrest-cipher-pass').read_text().strip()
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
    # Repository and local postgres on S2 have separate configs and SSH identities.
    wrapper = '''#!/bin/sh
case "$(id -un)" in
 postgres) pg_home=/var/lib/postgresql ;;
 pgbackrest) pg_home=/var/lib/pgbackrest ;;
 *) exit 1 ;;
esac
exec /usr/bin/ssh -o BatchMode=yes -o ConnectTimeout=10 -o IdentitiesOnly=yes -o IdentityFile="$pg_home/.ssh/id_ed25519_crawl_pgbackrest" -o UserKnownHostsFile="$pg_home/.ssh/crawl_pgbackrest_known_hosts" -o StrictHostKeyChecking=yes "$@"
'''
    for name, ip in NODES.items():
        run(ip, f'sudo -n install -d -m 0700 -o postgres -g postgres /var/lib/postgresql/.ssh /var/lib/pgbackrest-lock-{name}')
        put(ip, '/usr/local/libexec/crawl-pgbackrest-ssh', wrapper, mode=0o755)
        put(ip, '/usr/local/libexec/crawl-pgbackrest-remote', (ROOT / 'ops/backup/pgbackrest-remote.py').read_text(), mode=0o755)
        put(ip, '/var/lib/postgresql/.ssh/id_ed25519_crawl_pgbackrest', keys[name][0], 'postgres')
        put(ip, '/var/lib/postgresql/.ssh/crawl_pgbackrest_known_hosts', m['S2'] + ' ' + fingerprints[m['S2']] + '\n', 'postgres')
        cfg = common + f'cmd-ssh=/usr/local/libexec/crawl-pgbackrest-ssh\nlock-path=/var/lib/pgbackrest-lock-{name}\n'
        cfg += f'repo1-host={m["S2"]}\nrepo1-host-user=pgbackrest\nrepo1-host-config=/etc/pgbackrest/pgbackrest.conf\n\n[crawler]\npg1-path=/var/lib/postgresql/17/main\n'
        put(ip, '/etc/pgbackrest/pg-node.conf', cfg, 'postgres')
        if name != 's2': put(ip, '/etc/pgbackrest/pgbackrest.conf', cfg, 'postgres')
        authorize(ip, 'postgres', keys['repo'][1], m['S2'])
        authorize(m['S2'], 'pgbackrest', keys[name][1], ip)
    repo = common + 'cmd-ssh=/usr/local/libexec/crawl-pgbackrest-ssh\nlock-path=/var/lib/pgbackrest-lock-repo\n\n[crawler]\n'
    for index, ip in enumerate(NODES.values(), 1):
        repo += f'pg{index}-host={ip}\npg{index}-host-user=postgres\npg{index}-host-config=/etc/pgbackrest/pg-node.conf\npg{index}-path=/var/lib/postgresql/17/main\n'
    put(m['S2'], '/var/lib/pgbackrest/.ssh/crawl_pgbackrest_known_hosts', ''.join(ip + ' ' + fp + '\n' for ip, fp in fingerprints.items()), 'pgbackrest')
    put(m['S2'], '/etc/pgbackrest/pgbackrest.conf', repo, 'pgbackrest')
    run(m['S2'], 'sudo -n -u pgbackrest pgbackrest --stanza=crawler check')
    run(m['S2'], 'sudo -n -u pgbackrest pgbackrest --stanza=crawler --type=diff backup')
    print('Repository discovers all three PostgreSQL nodes; fresh differential backup complete.')
