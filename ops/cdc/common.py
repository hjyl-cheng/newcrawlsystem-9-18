"""Infrastructure helpers; credentials stay in ignored files or protected stdin."""
import base64,json,runpy,ssl,urllib.request,shlex,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
h=runpy.run_path(str(ROOT/'ops/backup/bootstrap-pgbackrest.py'))
run,put=h['run'],h['put']
NODES={'s1':'10.4.4.2','s2':'10.4.4.8','s3':'10.4.4.5'}
PRIVATE=ROOT/'secrets/postgresql-ha'
def api(node,path,body=None,method=None,timeout=60):
 c=ssl.create_default_context(cafile=str(PRIVATE/'rest-pki/ca.crt'))
 c.load_cert_chain(str(PRIVATE/'rest-pki/client.crt'),str(PRIVATE/'rest-pki/client.key'))
 headers={}
 if body is not None:headers={'Content-Type':'application/json','Authorization':'Basic '+base64.b64encode(('operator:'+(PRIVATE/'rest-password').read_text().strip()).encode()).decode()}
 req=urllib.request.Request('https://'+NODES[node]+':8008'+path,method=method,headers=headers,data=json.dumps(body).encode() if body is not None else None)
 with urllib.request.urlopen(req,context=c,timeout=timeout) as r:
  text=r.read().decode()
  try:return json.loads(text)
  except json.JSONDecodeError:return text

def members():return api('s1','/cluster')['members']
def leader():return next(m['name'] for m in members() if m['role']=='leader')
def sql(node,statement,db='postgres'):
 return run(NODES[node],'sudo -u postgres psql -XAt -v ON_ERROR_STOP=1 -d '+shlex.quote(db),statement,capture=True).stdout.strip()
def wait_members():
 for _ in range(90):
  m=members()
  if len(m)==3 and all(x['state'] in ['running','streaming'] for x in m) and any(x['role']=='sync_standby' for x in m):return m
  time.sleep(1)
 raise RuntimeError('PG cluster not healthy within deadline')
