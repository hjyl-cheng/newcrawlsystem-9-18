#!/usr/bin/env python3
"""Issue separate client identities and safely apply only their Kubernetes Secrets."""
import base64
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys

m=runpy.run_path(str(Path(__file__).with_name('secure-listener.py')))
PRIVATE=m['PRIVATE']


def main():
    if sys.argv[1:]!=['--execute']:raise SystemExit('Requires --execute')
    os.umask(0o077)
    assert (PRIVATE/'ca.key').exists() and (PRIVATE/'ca.crt').exists()
    for name,namespace,secret in [('ingestor','crawl-validation','kafka-ingestor-tls'),
        ('connect','crawl-validation','kafka-connect-tls'),('monitoring','crawl-monitoring','kafka-monitoring-tls')]:
        if not (PRIVATE/(name+'.crt')).exists():
            assert not (PRIVATE/(name+'.key')).exists(),'Partial identity exists: '+name
            m['openssl']('req','-new','-newkey','ec','-pkeyopt','ec_paramgen_curve:P-256','-nodes',
                '-subj','/CN=crawl-kafka-'+name,'-keyout',PRIVATE/(name+'.key'),'-out',PRIVATE/(name+'.csr'))
            (PRIVATE/(name+'.ext')).write_text('basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=clientAuth\n')
            m['openssl']('x509','-req','-in',PRIVATE/(name+'.csr'),'-CA',PRIVATE/'ca.crt','-CAkey',PRIVATE/'ca.key',
                '-CAcreateserial','-days','365','-sha256','-extfile',PRIVATE/(name+'.ext'),'-out',PRIVATE/(name+'.crt'))
        m['openssl']('verify','-CAfile',PRIVATE/'ca.crt',PRIVATE/(name+'.crt'))
        m['openssl']('x509','-in',PRIVATE/(name+'.crt'),'-checkend','2592000','-noout')
        files={'ca.crt':(PRIVATE/'ca.crt').read_bytes(),'client.crt':(PRIVATE/(name+'.crt')).read_bytes(),
            'client.key':(PRIVATE/(name+'.key')).read_bytes()}
        if name=='connect':files['client.pem']=files['client.key']+files['client.crt']
        body={'apiVersion':'v1','kind':'Secret','metadata':{'name':secret,'namespace':namespace},'type':'Opaque',
            'data':{k:base64.b64encode(v).decode() for k,v in files.items()}}
        subprocess.run(['kubectl','apply','--server-side','--field-manager=crawl-kafka-tls','-f','-'],
            input=json.dumps(body),text=True,capture_output=True,check=True)
        print(namespace+'/'+secret+': separate client certificate installed',flush=True)


if __name__=='__main__':main()
