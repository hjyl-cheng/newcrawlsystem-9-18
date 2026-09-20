#!/usr/bin/env python3
"""Update discovery/addon configuration after all node-local proxies are healthy."""
import json,subprocess,yaml

def cm(namespace,name):return json.loads(subprocess.check_output(['kubectl','-n',namespace,'get','cm',name,'-o','json']))
def patch(namespace,name,data):
    subprocess.run(['kubectl','-n',namespace,'patch','cm',name,'--type=merge','--patch-file=/dev/stdin'],input=json.dumps({'data':data}),text=True,check=True)
obj=cm('kube-system','kubeadm-config');conf=yaml.safe_load(obj['data']['ClusterConfiguration']);conf['controlPlaneEndpoint']='k8s-api.crawl.internal:16443'
patch('kube-system','kubeadm-config',{'ClusterConfiguration':yaml.safe_dump(conf,sort_keys=False)})
for namespace,name,key in [('kube-system','kube-proxy','kubeconfig.conf'),('kube-public','cluster-info','kubeconfig')]:
    obj=cm(namespace,name);conf=yaml.safe_load(obj['data'][key])
    for cluster in conf['clusters']:cluster['cluster']['server']='https://k8s-api.crawl.internal:16443'
    patch(namespace,name,{key:yaml.safe_dump(conf,sort_keys=False)})
subprocess.run(['kubectl','-n','kube-system','rollout','restart','ds/kube-proxy'],check=True)
