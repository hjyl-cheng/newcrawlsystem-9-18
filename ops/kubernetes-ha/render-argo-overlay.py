#!/usr/bin/env python3
"""Generate reviewable resource/scheduling patches over the pinned official HA bundle."""
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[2]
patches=[]
for d in yaml.safe_load_all((ROOT/'deploy/argocd/ha/vendor/install.yaml').read_text()):
    if not d or d['kind'] not in ('Deployment','StatefulSet'):continue
    name=d['metadata']['name'];spec=d['spec']['template']['spec']
    containers=[]
    for c in spec['containers']:
        if name=='argocd-redis-ha-server':
            budgets={'redis':('25m','64Mi','250m','192Mi'),'sentinel':('10m','16Mi','100m','64Mi'),'split-brain-fix':('5m','8Mi','100m','32Mi')}
            budget=budgets[c['name']]
        elif name=='argocd-application-controller':budget=('100m','192Mi','1','768Mi')
        elif name=='argocd-repo-server':budget=('50m','128Mi','500m','512Mi')
        elif name=='argocd-server':budget=('50m','64Mi','500m','384Mi')
        elif name=='argocd-redis-ha-haproxy':budget=('10m','16Mi','100m','64Mi')
        else:budget=('10m','32Mi','250m','192Mi')
        containers.append({'name':c['name'],'resources':{'requests':{'cpu':budget[0],'memory':budget[1]},'limits':{'cpu':budget[2],'memory':budget[3]}}})
    pod={'containers':containers,'nodeSelector':{'node-role.kubernetes.io/control-plane':''},
         'tolerations':[{'key':'node-role.kubernetes.io/control-plane','operator':'Exists','effect':'NoSchedule'}]}
    if name=='argocd-application-controller':
        # Stable single-active controller. Preference spreads it off A1, never hard-pins it.
        pod['affinity']={'nodeAffinity':{'preferredDuringSchedulingIgnoredDuringExecution':[{'weight':100,'preference':{'matchExpressions':[{'key':'kubernetes.io/hostname','operator':'In','values':['a2']}]}}]}}
    if name=='argocd-redis-ha-haproxy':
        # Two proxies suffice; Redis/Sentinel still require three voting members.
        replicas=2
    else:replicas=d['spec'].get('replicas',1)
    patch={'apiVersion':d['apiVersion'],'kind':d['kind'],'metadata':{'name':name},'spec':{'replicas':replicas,'template':{'spec':pod}}}
    patches.append({'target':{'kind':d['kind'],'name':name},'patch':yaml.safe_dump(patch,sort_keys=False)})
config={'apiVersion':'kustomize.config.k8s.io/v1beta1','kind':'Kustomization','namespace':'argocd',
 'resources':['vendor/install.yaml','availability.yaml'],
 'images':[
 {'name':'quay.io/argoproj/argocd','digest':'sha256:17c471916f3e14c01c599a534944c83a7905c1ba42a486e4ef9b87f58c788658'},
 {'name':'ghcr.io/dexidp/dex','digest':'sha256:bc7cfce7c17f52864e2bb2a4dc1d2f86a41e3019f6d42e81d92a301fad0c8a1d'},
 {'name':'public.ecr.aws/docker/library/redis','newName':'docker.io/library/redis','digest':'sha256:c9d92d840fd011c908f040592857c724ae6d877f2aba5c40ad963276507386b2'},
 {'name':'public.ecr.aws/docker/library/haproxy','newName':'docker.io/library/haproxy','digest':'sha256:14772bafca418146ade0acba20ebb17a06ac2ad720a93baa6d6792ce1e0f57d9'}],
 'patches':patches}
(ROOT/'deploy/argocd/ha/kustomization.yaml').write_text(yaml.safe_dump(config,sort_keys=False))
