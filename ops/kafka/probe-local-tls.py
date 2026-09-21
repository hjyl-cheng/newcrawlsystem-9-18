#!/usr/bin/env python3
"""Executed as kafka on one broker; bounded read-only TLS/API tests."""

import json,socket,ssl,struct,subprocess,tempfile,sys
ip=sys.argv[1];base='/etc/kafka/tls/'
def recv(sock,n):
 result=b''
 while len(result)<n:
  part=sock.recv(n-len(result))
  if not part:raise RuntimeError('Unexpected EOF')
  result+=part
 return result
def api(ctx,name=ip):
 with socket.create_connection((ip,9094),timeout=4) as raw:
  with ctx.wrap_socket(raw,server_hostname=name) as tls:
   # ApiVersions v0, request header v1, no client id; bounded response.
   request=struct.pack('!hhih',18,0,417,-1)
   tls.sendall(struct.pack('!i',len(request))+request)
   size=struct.unpack('!i',recv(tls,4))[0];assert 10<=size<=100000
   response=recv(tls,size);correlation,error,count=struct.unpack('!ihi',response[:10])
   assert correlation==417 and error==0 and count>0
   return {'tls':tls.version(),'apiVersions':count,'certificateVerified':True}
def context(version=None,client=True,trust=True):
 ctx=ssl.create_default_context(cafile=base+'ca.crt') if trust else ssl.create_default_context()
 if version:ctx.minimum_version=version;ctx.maximum_version=version
 if client:ctx.load_cert_chain(base+'node.crt',base+'node.key')
 return ctx
out={}
for version in [ssl.TLSVersion.TLSv1_2,ssl.TLSVersion.TLSv1_3]:out[version.name]=api(context(version))
tests=[('wrongServerName',context(),'not-a-kafka-node.invalid'),('untrustedServer',context(trust=False),ip),
 ('missingClientCertificate',context(ssl.TLSVersion.TLSv1_2,client=False),ip)]
with tempfile.TemporaryDirectory() as temp:
 subprocess.run(['openssl','req','-x509','-newkey','ec','-pkeyopt','ec_paramgen_curve:P-256','-nodes',
  '-subj','/CN=untrusted-kafka-test','-addext','extendedKeyUsage=clientAuth',
  '-keyout',temp+'/key','-out',temp+'/crt','-days','1'],check=True,capture_output=True)
 ctx=context(ssl.TLSVersion.TLSv1_2,client=False);ctx.load_cert_chain(temp+'/crt',temp+'/key')
 tests.append(('untrustedClientCertificate',ctx,ip))
 for label,ctx,name in tests:
  try:api(ctx,name)
  except ssl.SSLError as error:
   reason=error.reason
   expected={'wrongServerName':{'CERTIFICATE_VERIFY_FAILED'},'untrustedServer':{'CERTIFICATE_VERIFY_FAILED'},
    'missingClientCertificate':{'SSLV3_ALERT_BAD_CERTIFICATE','SSLV3_ALERT_HANDSHAKE_FAILURE','TLSV13_ALERT_CERTIFICATE_REQUIRED'},
    'untrustedClientCertificate':{'SSLV3_ALERT_BAD_CERTIFICATE','TLSV1_ALERT_UNKNOWN_CA','SSLV3_ALERT_CERTIFICATE_UNKNOWN'}}
   assert reason in expected[label],(label,reason)
   if label=='wrongServerName':assert error.verify_code==62
   out[label]={'rejected':True,'reason':reason}
  else:raise AssertionError(label+' was accepted')
print(json.dumps(out))
