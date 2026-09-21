#!/usr/bin/env python3
"""Signed synthetic object roundtrip and bucket authorization on all gateways."""
import boto3,json,hashlib,urllib.request,urllib.error,uuid
from pathlib import Path
from botocore.config import Config
from botocore.exceptions import ClientError
keys=json.loads((Path(__file__).resolve().parents[2]/'secrets/seaweedfs/s3-credentials.json').read_text())
bucket='crawl-validation-objects'
def client(ip,identity='operator'):
 k=keys[identity]
 return boto3.client('s3',endpoint_url='http://'+ip+':8333',aws_access_key_id=k['accessKey'],aws_secret_access_key=k['secretKey'],region_name='us-east-1',config=Config(signature_version='s3v4',s3={'addressing_style':'path'},connect_timeout=3,read_timeout=15,retries={'max_attempts':2}))
c=client('10.4.4.2')
if bucket not in [b['Name'] for b in c.list_buckets()['Buckets']]:c.create_bucket(Bucket=bucket)
if 'crawl-infra-private' not in [b['Name'] for b in c.list_buckets()['Buckets']]:c.create_bucket(Bucket='crawl-infra-private')
payload=b'seaweed-ha-authenticated-roundtrip-v1\n'*1024
c.put_object(Bucket=bucket,Key='infra/ha-seed.bin',Body=payload)
r=[]
for ip in ['10.4.4.2','10.4.4.8','10.4.4.5']:
 w=client(ip,'worker'); d=w.get_object(Bucket=bucket,Key='infra/ha-seed.bin')['Body'].read();assert d==payload
 assert 'infra/ha-seed.bin' in [x['Key'] for x in w.list_objects_v2(Bucket=bucket)['Contents']]
 try:urllib.request.urlopen('http://'+ip+':8333/',timeout=5);raise AssertionError('Anonymous access')
 except urllib.error.HTTPError as e: assert e.code==403
 assert 'crawl-infra-private' not in [b['Name'] for b in w.list_buckets()['Buckets']]
 try:w.list_objects_v2(Bucket='crawl-infra-private');raise AssertionError('Worker private bucket allowed')
 except ClientError as e:assert e.response['ResponseMetadata']['HTTPStatusCode']==403
 bad=boto3.client('s3',endpoint_url='http://'+ip+':8333',aws_access_key_id='invalid-key',aws_secret_access_key='invalid-secret',region_name='us-east-1',config=Config(signature_version='s3v4',connect_timeout=3,read_timeout=5))
 try:bad.list_buckets();raise AssertionError('Invalid key allowed')
 except ClientError as e:assert e.response['ResponseMetadata']['HTTPStatusCode']==403
 r.append({'invalidKeyDenied':True,'node':ip,'readListMatch':True,'anonymousDenied':True,'workerOtherBucketDenied':True,'bucketListFiltered':True})
key='infra/auth-mutation-'+uuid.uuid4().hex+'.bin'
client('10.4.4.2','worker').put_object(Bucket=bucket,Key=key,Body=b'before')
assert client('10.4.4.5','worker').get_object(Bucket=bucket,Key=key)['Body'].read()==b'before'
client('10.4.4.8','worker').put_object(Bucket=bucket,Key=key,Body=b'after')
for ip in ['10.4.4.2','10.4.4.8','10.4.4.5']:
 assert client(ip,'worker').get_object(Bucket=bucket,Key=key)['Body'].read()==b'after'
client('10.4.4.5','worker').delete_object(Bucket=bucket,Key=key)
for ip in ['10.4.4.2','10.4.4.8','10.4.4.5']:
 try:client(ip,'worker').get_object(Bucket=bucket,Key=key);raise AssertionError('Deleted object visible')
 except ClientError as e:assert e.response['ResponseMetadata']['HTTPStatusCode']==404
print(json.dumps({'status':'PASSED','gateways':r,'crossNodeOverwriteDelete':'PASSED','bytes':len(payload),'sha256':hashlib.sha256(payload).hexdigest()},indent=2))
