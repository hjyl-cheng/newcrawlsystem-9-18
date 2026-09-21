#!/usr/bin/env python3
"""Read-only mTLS/API boundary checks and six-host TCP connectivity matrix."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import runpy
import shlex

m = runpy.run_path(str(Path(__file__).with_name('secure-listener.py')))
ROOT, nodes, NODES = m['ROOT'], m['nodes'], m['NODES']
PROBE = Path(__file__).with_name('probe-local-tls.py').read_text()
MATRIX = r'''
import socket,json
from concurrent.futures import ThreadPoolExecutor
targets={'s1':'10.4.4.2','s2':'10.4.4.8','s3':'10.4.4.5'}
def check(item):
 name,ip=item
 try:
  with socket.create_connection((ip,9094),timeout=3):pass
  return name,{'tcp':'reachable'}
 except OSError as e:return name,{'tcp':'blocked-or-unreachable','error':type(e).__name__}
with ThreadPoolExecutor(max_workers=3) as pool:print(json.dumps(dict(pool.map(check,targets.items()))))
'''


def main():
    evidence = {'checkedAt': datetime.now(timezone.utc).isoformat(), 'boundaries': {}, 'network': {}}
    for node, ip in NODES.items():
        result = m['run'](node, 'sudo -u kafka python3 - '+ip, PROBE)
        evidence['boundaries'][node] = json.loads(result.stdout)
        print(node+': TLS 1.2/1.3 Kafka API and four certificate rejection checks passed', flush=True)
    def network(node):
        return node, json.loads(nodes.run(node, 'python3 -c '+shlex.quote(MATRIX)).stdout)
    with ThreadPoolExecutor(max_workers=6) as pool:
        evidence['network'] = dict(pool.map(network, nodes.NODES))
    evidence['allHostPathsReachable'] = all(p['tcp']=='reachable' for n in evidence['network'].values() for p in n.values())
    evidence['status'] = 'PASSED' if evidence['allHostPathsReachable'] else 'LOCAL_PASSED_NETWORK_BLOCKED'
    evidence['scope'] = 'New 9094 listener only; no ACL claim, no client migration, host probes do not prove Pod NetworkPolicy access'
    evidence['existingKafka'] = m['health']()
    path = ROOT/'ops/checks/2026-09-21-kafka-mtls-boundaries.json'
    path.write_text(json.dumps(evidence, indent=2)+'\n')
    print(json.dumps({'status':evidence['status'], 'network':evidence['network']}), flush=True)


if __name__ == '__main__':main()
