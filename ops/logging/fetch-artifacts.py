#!/usr/bin/env python3
"""Download only pinned official artifacts and verify published checksums."""
import hashlib,json,tarfile,urllib.request,zipfile
from pathlib import Path
release=json.loads(Path(__file__).with_name('releases.json').read_text())
for name,target in [('vector','/tmp/crawl-vector.tar.gz'),('grafana-clickhouse','/tmp/crawl-clickhouse-plugin.zip')]:
 entry=release[name];path=Path(target)
 if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest()!=entry['sha256']:urllib.request.urlretrieve(entry['url'],path)
 assert hashlib.sha256(path.read_bytes()).hexdigest()==entry['sha256']
 if name=='vector':
  with tarfile.open(path) as tar:
   member=next(m for m in tar.getmembers() if m.name.endswith('/bin/vector'));binary=tar.extractfile(member).read()
   assert hashlib.sha256(binary).hexdigest()==entry['binarySHA256']
   Path('/tmp/crawl-vector').write_bytes(binary);Path('/tmp/crawl-vector').chmod(0o755)
 else:
  with zipfile.ZipFile(path) as archive:
   assert all(not n.startswith('/') and '..' not in Path(n).parts for n in archive.namelist())
   archive.extractall('/tmp/crawl-clickhouse-plugin')
 print(name+': checksum verified')
