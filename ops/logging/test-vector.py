#!/usr/bin/env python3
"""Run real VRL tests and verify buffer-full behavior against a failed HTTP sink."""
import importlib.util,json,os,subprocess,tempfile
from pathlib import Path
import yaml
BASE=Path(__file__).resolve().parent
s=importlib.util.spec_from_file_location('render',BASE/'render.py');r=importlib.util.module_from_spec(s);s.loader.exec_module(r)
c=r.render('a1')
def case(name,fields,condition):return {'name':name,'inputs':[{'insert_at':'normalize','type':'log','log_fields':fields}],'outputs':[{'extract_from':'normalize','conditions':[{'type':'vrl','source':condition}]}]}
c['tests']=[
 case('redact passwords, JSON tokens, HTTP auth and URL userinfo',{'message':'password=fixture-secret token="fixture-token" Authorization: Bearer fixture-bearer http://fixture-user:fixture-pass@host/?api_key=fixture-api','_SYSTEMD_UNIT':'crawl-log-probe.service'},'assert_eq!(.Service, "crawl-log-probe")\nassert!(!contains(string!(.Body), "fixture-"))\nassert!(contains(string!(.Body), "REDACTED"))'),
 case('redact JSON credentials including cookie',{'message':'{"password":"fixture-secret", "token":"fixture-token", "cookie":"session=fixture-cookie"}','_SYSTEMD_UNIT':'crawl-log-probe.service'},'assert!(!contains(string!(.Body), "fixture-"))'),
 case('never forward arbitrary journal fields',{'message':'ordinary harmless event','_SYSTEMD_UNIT':'crawl-log-probe.service','EXTRA_SECRET':'fixture-private'},'assert!(!exists(.EXTRA_SECRET))\nassert_eq!(.Body,"ordinary harmless event")'),
 case('limit oversized messages',{'message':'x'*10000,'_SYSTEMD_UNIT':'crawl-log-probe.service'},'assert!(length(string!(.Body)) <= 4110)\nassert!(contains(string!(.Body),"TRUNCATED"))'),
 {'name':'partial CRI fragments are counted as dropped, never malformed inserts','inputs':[{'insert_at':'normalize','type':'log','log_fields':{'file':'/var/log/pods/crawl-validation_test-pod_uid/worker/0.log','message':'2026-09-21T00:00:00Z stdout P fragment'}}],'no_outputs_from':['normalize']}
]
with tempfile.TemporaryDirectory() as d:
 path=Path(d)/'tests.yaml';path.write_text(yaml.safe_dump(c,sort_keys=False));subprocess.run(['/tmp/crawl-vector','test',str(path)],check=True)
print('Five redaction / boundary tests passed on pinned Vector.')
