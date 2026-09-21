#!/usr/bin/env python3
"""Small isolated memory-buffer test of drop_newest; not a disk capacity load test."""
import json,os,re,socket,subprocess,tempfile,threading,time,urllib.request
from http.server import BaseHTTPRequestHandler,HTTPServer
from pathlib import Path
import yaml
class Reject(BaseHTTPRequestHandler):
 def do_POST(self):
  self.rfile.read(int(self.headers.get('Content-Length',0)));self.send_response(503);self.end_headers()
 def log_message(self,*args):pass
server=HTTPServer(('127.0.0.1',0),Reject);threading.Thread(target=server.serve_forever,daemon=True).start()
with socket.socket() as s:s.bind(('127.0.0.1',0));metrics_port=s.getsockname()[1]
with tempfile.TemporaryDirectory(prefix='crawl-log-policy-') as folder:
 config={'data_dir':folder,'sources':{'events':{'type':'stdin'},'metrics':{'type':'internal_metrics','scrape_interval_secs':1}},'sinks':{'blocked':{'type':'http','inputs':['events'],'uri':'http://127.0.0.1:'+str(server.server_port),'encoding':{'codec':'json'},'batch':{'max_events':1,'timeout_secs':1},'buffer':{'type':'memory','max_events':2,'when_full':'drop_newest'},'request':{'concurrency':1,'retry_initial_backoff_secs':1,'retry_max_duration_secs':2}},'metrics_out':{'type':'prometheus_exporter','inputs':['metrics'],'address':'127.0.0.1:'+str(metrics_port)}}}
 path=Path(folder)/'vector.yaml';path.write_text(yaml.safe_dump(config));process=subprocess.Popen(['/tmp/crawl-vector','--config',str(path)],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env={**os.environ,'VECTOR_LOG':'error','VECTOR_THREADS':'1'})
 try:
  started=time.monotonic();process.stdin.write(('fixture-event '+('x'*1000)+'\n').encode()*4000);process.stdin.flush();elapsed=time.monotonic()-started
  total=0
  for _ in range(20):
   time.sleep(1)
   try:text=urllib.request.urlopen('http://127.0.0.1:'+str(metrics_port)+'/metrics',timeout=2).read().decode()
   except OSError:continue
   total=sum(float(l.split('} ')[1].split()[0]) for l in text.splitlines() if re.match(r'^vector_(buffer|component)_discarded_events_total\{',l) and 'component_id="blocked"' in l)
   if total:break
  assert total>0 and process.poll() is None and elapsed<10
  result={'status':'PASSED','isolatedBuffer':'memory/2 events','producerWriteSeconds':round(elapsed,3),'observedDiscardCounterSum':total,'eventsOffered':4000,'diskFullNotLoadTested':True}
  Path('ops/checks/2026-09-21-logging-buffer-policy.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
 finally:
  process.terminate()
  try:process.wait(timeout=3)
  except subprocess.TimeoutExpired:process.kill();process.wait()
server.shutdown()
