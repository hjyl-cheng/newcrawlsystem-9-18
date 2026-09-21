#!/usr/bin/env python3
"""Render the small, pinned monitoring installation without a Helm dependency."""
import json,runpy
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[2]
BASE=ROOT/'deploy/base/monitoring'
BASE.mkdir(parents=True,exist_ok=True)
runpy.run_path(str(ROOT/'ops/logging/render-dashboard.py'))
IMAGES=json.loads((ROOT/'ops/monitoring/images.json').read_text())
TUNNELS=['192.168.141.132','192.168.78.192','192.168.65.64']
NODES={'a1':'10.4.4.12','a2':'10.4.4.3','a3':'10.4.4.17','s1':'10.4.4.2','s2':'10.4.4.8','s3':'10.4.4.5'}
def write(path,value):path.write_text(yaml.safe_dump(value,sort_keys=False,allow_unicode=True))
def resource(kind,name,spec=None,api='v1',**extra):
 d={'apiVersion':api,'kind':kind,'metadata':{'name':name}}
 if spec is not None:d['spec']=spec
 d.update(extra);return d
objects=[]
cluster=[]
for component,nodes,size in [('prometheus',['a2','a3'],'5Gi'),('alertmanager',['a1','a2','a3'],'1Gi')]:
 for node in nodes:
  cluster.append(resource('PersistentVolume','crawl-'+component+'-'+node,{'capacity':{'storage':size},'volumeMode':'Filesystem','accessModes':['ReadWriteOnce'],'persistentVolumeReclaimPolicy':'Retain','storageClassName':'crawl-monitoring-local','local':{'path':'/srv/crawlsystem/monitoring/'+component},'nodeAffinity':{'required':{'nodeSelectorTerms':[{'matchExpressions':[{'key':'kubernetes.io/hostname','operator':'In','values':[node]}]}]}}}))
for pv in cluster:pv['metadata']['labels']={'crawl-monitoring-component':pv['metadata']['name'].rsplit('-',1)[0].removeprefix('crawl-')}
cluster.append(resource('StorageClass','crawl-monitoring-local',api='storage.k8s.io/v1',provisioner='kubernetes.io/no-provisioner',volumeBindingMode='WaitForFirstConsumer'))
role=resource('ClusterRole','crawl-monitoring-state',api='rbac.authorization.k8s.io/v1',rules=[{'apiGroups':[''],'resources':['nodes','pods','namespaces','persistentvolumeclaims','persistentvolumes'],'verbs':['list','watch']},{'apiGroups':['apps'],'resources':['deployments','statefulsets','daemonsets','replicasets'],'verbs':['list','watch']}])
cluster.append(role)
cluster.append(resource('ClusterRoleBinding','crawl-monitoring-state',api='rbac.authorization.k8s.io/v1',roleRef={'apiGroup':'rbac.authorization.k8s.io','kind':'ClusterRole','name':'crawl-monitoring-state'},subjects=[{'kind':'ServiceAccount','name':'kube-state-metrics','namespace':'crawl-monitoring'}]))
(ROOT/'ops/monitoring/cluster-resources.yaml').write_text(yaml.safe_dump_all(cluster,sort_keys=False))
objects.append(resource('ServiceAccount','kube-state-metrics'))

def service(name,port,headless=False,gossip=False):
 ports=[{'name':'http','port':port,'targetPort':port}]
 if gossip:ports += [{'name':'gossip-tcp','port':9094,'protocol':'TCP'},{'name':'gossip-udp','port':9094,'protocol':'UDP'}]
 spec={'selector':{'app':name},'ports':ports}
 if headless:spec.update(clusterIP='None',publishNotReadyAddresses=True)
 objects.append(resource('Service',name+('-headless' if headless else ''),spec))

def volume(name,path,kind='configMap'):
 return {'name':name,kind:{'name' if kind=='configMap' else 'secretName':name}}, {'name':name,'mountPath':path,'readOnly':True}

def workload(name,image,port,replicas=2,args=None,env=None,volumes=None,mounts=None,mem='256Mi',cpu='300m',uid=65534,stateful=False,health='/',command=None,sa=None):
 container={'name':name,'image':IMAGES[image]['image'],'ports':[{'containerPort':port,'name':'http'}],'resources':{'requests':{'cpu':'50m','memory':'64Mi'},'limits':{'cpu':cpu,'memory':mem}},'securityContext':{'allowPrivilegeEscalation':False,'readOnlyRootFilesystem':True,'capabilities':{'drop':['ALL']}},'readinessProbe':{'httpGet':{'path':health,'port':'http'},'periodSeconds':10},'startupProbe':{'httpGet':{'path':health,'port':'http'},'periodSeconds':5,'failureThreshold':60},'livenessProbe':{'httpGet':{'path':health,'port':'http'},'periodSeconds':20,'failureThreshold':6}}
 if args:container['args']=args
 if command:container['command']=command
 if env:container['env']=env
 if mounts:container['volumeMounts']=mounts
 pod={'automountServiceAccountToken':bool(sa),'nodeSelector':{'node-role.kubernetes.io/control-plane':''},'tolerations':[{'key':'node-role.kubernetes.io/control-plane','operator':'Exists','effect':'NoSchedule'}],'securityContext':{'runAsNonRoot':True,'runAsUser':uid,'runAsGroup':uid,'fsGroup':uid,'seccompProfile':{'type':'RuntimeDefault'}},'containers':[container],'affinity':{'podAntiAffinity':{'requiredDuringSchedulingIgnoredDuringExecution':[{'labelSelector':{'matchLabels':{'app':name}},'topologyKey':'kubernetes.io/hostname'}]}},'terminationGracePeriodSeconds':60}
 if sa:pod['serviceAccountName']=sa
 if volumes:pod['volumes']=volumes
 spec={'replicas':replicas,'revisionHistoryLimit':2,'selector':{'matchLabels':{'app':name}},'template':{'metadata':{'labels':{'app':name}},'spec':pod}}
 if stateful:
  spec.update(serviceName=name+'-headless',podManagementPolicy='Parallel',volumeClaimTemplates=[{'metadata':{'name':'data'},'spec':{'accessModes':['ReadWriteOnce'],'storageClassName':'crawl-monitoring-local','selector':{'matchLabels':{'crawl-monitoring-component':name}},'resources':{'requests':{'storage':'5Gi' if name=='prometheus' else '1Gi'}}}}])
 else:spec['strategy']={'type':'RollingUpdate','rollingUpdate':{'maxSurge':0,'maxUnavailable':1}}
 objects.append(resource('StatefulSet' if stateful else 'Deployment',name,spec,'apps/v1'))
 objects.append(resource('PodDisruptionBudget',name,{'minAvailable':replicas-1,'selector':{'matchLabels':{'app':name}}},'policy/v1'))
 service(name,port)
 if stateful:service(name,port,True,name=='alertmanager')
 return container,pod

def envs(data):return [{'name':k,'value':str(v)} for k,v in data.items()]

tls={'ca_file':'/etc/monitoring-tls/ca.crt','cert_file':'/etc/monitoring-tls/prometheus-client.crt','key_file':'/etc/monitoring-tls/prometheus-client.key'}
prom={'global':{'scrape_interval':'30s','scrape_timeout':'10s','evaluation_interval':'15s','external_labels':{'cluster':'crawl-validation','replica':'${POD_NAME}'}},'rule_files':['/etc/prometheus/rules.yaml'],'alerting':{'alert_relabel_configs':[{'action':'labeldrop','regex':'replica'}],'alertmanagers':[{'static_configs':[{'targets':['alertmanager-'+str(i)+'.alertmanager-headless:9093' for i in range(3)]}]}]},'scrape_configs':[{'job_name':'nodes','scheme':'https','tls_config':tls,'static_configs':[{'targets':[ip+':9100'],'labels':{'node':n}} for n,ip in NODES.items()]}]}
for name,port in [('prometheus',9090),('alertmanager',9093),('kube-state-metrics',8080),('kafka-exporter',9308),('infra-exporter',9189)]:
 targets=[name+':'+str(port)]
 if name in ['prometheus','alertmanager']:targets=[name+'-'+str(i)+'.'+name+'-headless:'+str(port) for i in range(2 if name=='prometheus' else 3)]
 prom['scrape_configs'].append({'job_name':name,'static_configs':[{'targets':targets}]})
write(BASE/'prometheus.yaml',prom)

rules=[]
def rule(name,expr,duration,summary,severity='warning'):
 rules.append({'alert':name,'expr':expr,'for':duration,'labels':{'severity':severity},'annotations':{'summary':summary,'description':'instance={{ $labels.instance }} node={{ $labels.node }} value={{ $value }}；排查步骤见 ops/monitoring/README.md。'}})
rule('MonitoringTargetDown','up == 0','1m','监控目标无法访问','critical')
rule('MonitoringTargetMissing','count(up{job="nodes"}) != 6','2m','节点采集目标数量不等于六')
rule('LocalProbeFailed','crawl_collector_success == 0','2m','本地基础设施探针失败','critical')
rule('LocalProbeStale','time() - crawl_collector_last_run_timestamp_seconds > 120','1m','本地探针数据停止刷新','critical')
rule('HostDiskLow','node_filesystem_avail_bytes{mountpoint="/",fstype!="tmpfs"} / node_filesystem_size_bytes{mountpoint="/",fstype!="tmpfs"} < 0.15','5m','系统盘剩余空间低于 15%')
rule('HostMemoryLow','node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes < 0.10','5m','可用内存低于 10%')
rule('HostCpuHigh','1 - avg by (instance,node)(rate(node_cpu_seconds_total{mode="idle"}[5m])) > 0.9','10m','CPU 持续超过 90%')
rule('CriticalServiceDown','crawl_service_up == 0','1m','基础服务停止','critical')
rule('PostgresPrimaryCount','sum(crawl_pg_is_primary) != 1','1m','PostgreSQL 主库数量异常','critical')
rule('PostgresReplicationLag','crawl_pg_replica_replay_lag_bytes > 16777216','2m','PG 副本回放积压超过 16MiB')
rule('PostgresSyncReplicaMissing','(crawl_pg_synchronous_replicas == 0) and on(node) (crawl_pg_is_primary == 1)','1m','PG 主库没有同步副本','critical')
rule('PostgresConnectionPressure','crawl_pg_connections / crawl_pg_max_connections > 0.8','5m','PG 连接使用超过 80%')
rule('CdcSlotMissing','crawl_pg_cdc_slot_present == 0','2m','CDC 槽缺失','critical')
rule('CdcSlotInvalid','crawl_pg_slot_valid{slot="crawl_cdc_validation"} == 0 or crawl_pg_slot_temporary{slot="crawl_cdc_validation"} == 1','1m','CDC 槽无效或为临时槽','critical')
rule('ReplicationSlotRetainsWal','crawl_pg_slot_retained_bytes > 1073741824','5m','复制槽滞留 WAL 超过 1GiB')
rule('ReplicationSlotWalBudgetLow','crawl_pg_slot_safe_wal_size < 268435456','2m','复制槽剩余 WAL 预算低于 256MiB','critical')
rule('CdcGuardNotReady','crawl_cdc_guard_ready == 0 or time() - crawl_cdc_guard_last_check_timestamp_seconds > 90','1m','CDC guard 未就绪或停止刷新','critical')
rule('CdcBarrierMismatch','crawl_cdc_guard_barrier_matches == 0','1m','CDC 等待屏障与候选名单不一致','critical')
rule('KafkaIsrBelowMinimum','kafka_topic_partition_in_sync_replica < 2','1m','Kafka ISR 低于写入最低副本数','critical')
rule('KafkaReplicaDegraded','kafka_topic_partition_in_sync_replica < kafka_topic_partition_replicas','3m','Kafka 分区副本未全部同步')
rule('KafkaConsumerLag','sum by(consumergroup,topic)(kafka_consumergroup_lag) > 1000','5m','Kafka 消费积压持续超过 1000 条')
rule('ConnectNotRunning','crawl_connect_connector_running == 0 or crawl_connect_tasks_running == 0','1m','CDC Connector 或 Task 未运行','critical')
rule('ApplicationProbeFailed','crawl_http_probe_success == 0','1m','应用就绪接口检查失败','critical')
rule('ApplicationProbeStale','time() - crawl_http_probe_last_run_timestamp_seconds > 90','1m','应用探针停止刷新','critical')
rule('KubernetesNodeNotReady','kube_node_status_condition{condition="Ready",status="true"} != 1','2m','Kubernetes 节点未就绪','critical')
rule('KubernetesDeploymentUnavailable','kube_deployment_status_replicas_available < kube_deployment_spec_replicas','5m','Deployment 可用副本不足')
rule('KubernetesStatefulSetUnavailable','kube_statefulset_status_replicas_ready < kube_statefulset_replicas','5m','StatefulSet 可用副本不足')
rule('KubernetesContainerRestarting','increase(kube_pod_container_status_restarts_total[15m]) > 3','2m','容器频繁重启')
rule('BackupStale','(time() - crawl_backup_last_success_timestamp_seconds{backup!="seaweedfs"}) > 108000','2m','备份超过 30 小时未成功','critical')
rule('BackupMissing','crawl_backup_archive_present == 0','2m','备份归档缺失','critical')
rule('BackupScheduleDisabled','crawl_backup_scheduled{backup!="seaweedfs"} == 0','2m','定时备份未启用','critical')
rule('BackupLastRunFailed','crawl_backup_last_run_success == 0','2m','最近一次备份任务失败','critical')
rule('SeaweedContinuousBackupPending','max by(backup)(crawl_backup_scheduled{backup="seaweedfs"}) == 0','1m','已知待办：SeaweedFS 目前只有维护备份')
rule('LogBufferHigh','crawl_vector_buffer_size_bytes{component="clickhouse"} > 402653184','5m','日志缓冲超过 75%，请检查接收端')
rule('LogEventsDiscarded','increase(crawl_vector_component_discarded_events_total[5m]) > 0 or increase(crawl_vector_buffer_discarded_events_total[5m]) > 0','0s','日志被丢弃：检查限流、解析、缓冲或写入错误')
rule('LogSinkErrors','increase(crawl_vector_component_errors_total{component="clickhouse"}[5m]) > 0','1m','日志写入端连续报错')
rule('LogHeartbeatMissing','time() - crawl_logs_last_heartbeat_timestamp_seconds > 180','2m','节点日志心跳未到达中央日志库')
rule('LogRetentionStale','time() - crawl_logs_retention_checked_timestamp_seconds > 600','2m','日志容量清理任务停止刷新')
rule('LogStorageHigh','crawl_logs_active_bytes > 2147483648','10m','集中日志超过容量清理阈值')
rule('AlertmanagerClusterDegraded','alertmanager_cluster_members < 3','2m','Alertmanager 集群成员不足')
write(BASE/'rules.yaml',{'groups':[{'name':'crawl-infrastructure','rules':rules}]})
write(BASE/'alertmanager.yaml',{'global':{'resolve_timeout':'5m'},'route':{'receiver':'platform-only','group_by':['alertname','node','instance'],'group_wait':'15s','group_interval':'1m','repeat_interval':'4h'},'receivers':[{'name':'platform-only'}]})

v,m=volume('prometheus-config','/etc/prometheus');tv,tm=volume('monitoring-client-tls','/etc/monitoring-tls','secret')
tv['secret']['defaultMode']=0o440
workload('prometheus','prometheus',9090,stateful=True,mem='640Mi',cpu='500m',health='/-/ready',args=['--config.file=/etc/prometheus/prometheus.yaml','--storage.tsdb.path=/prometheus','--storage.tsdb.retention.time=3d','--storage.tsdb.retention.size=3GB'],env=[{'name':'POD_NAME','valueFrom':{'fieldRef':{'fieldPath':'metadata.name'}}}],volumes=[v,tv],mounts=[m,tm,{'name':'data','mountPath':'/prometheus'}])
v,m=volume('alertmanager-config','/etc/alertmanager')
workload('alertmanager','alertmanager',9093,replicas=3,stateful=True,mem='128Mi',health='/-/ready',args=['--config.file=/etc/alertmanager/alertmanager.yaml','--storage.path=/alertmanager','--cluster.listen-address=0.0.0.0:9094','--cluster.advertise-address=$(POD_IP):9094']+['--cluster.peer=alertmanager-'+str(i)+'.alertmanager-headless:9094' for i in range(3)],env=[{'name':'POD_IP','valueFrom':{'fieldRef':{'fieldPath':'status.podIP'}}}],volumes=[v],mounts=[m,{'name':'data','mountPath':'/alertmanager'}])
workload('kube-state-metrics','kube-state-metrics',8080,mem='192Mi',health='/healthz',args=['--resources=nodes,pods,namespaces,deployments,statefulsets,daemonsets,replicasets,persistentvolumeclaims,persistentvolumes','--namespaces=argocd,crawl-validation,crawl-monitoring,kube-system'],sa='kube-state-metrics')
workload('kafka-exporter','kafka-exporter',9308,mem='128Mi',health='/metrics',args=['--kafka.server='+NODES[n]+':9092' for n in ['s1','s2','s3']]+['--kafka.version=3.9.0',r'--topic.filter=^(crawler\..*|crawl-connect-.*)$','--group.filter=^crawler-results-apply-validation-v1$','--refresh.metadata=30s'])
v,m=volume('infra-exporter-code','/opt/exporter')
workload('infra-exporter','python',9189,mem='96Mi',health='/health',command=['python3','-B','/opt/exporter/infra-exporter.py'],volumes=[v],mounts=[m])

write(BASE/'datasources.yaml',{'apiVersion':1,'datasources':[{'name':'Prometheus','uid':'prometheus','type':'prometheus','access':'proxy','url':'http://prometheus:9090','isDefault':True,'editable':False},{'name':'Alertmanager','uid':'alertmanager','type':'alertmanager','access':'proxy','url':'http://alertmanager:9093','jsonData':{'implementation':'prometheus','handleGrafanaManagedAlerts':False},'editable':False}]})
datasources=yaml.safe_load((BASE/'datasources.yaml').read_text())
datasources['datasources'].append(yaml.safe_load((ROOT/'ops/logging/grafana-datasource.yaml').read_text()))
write(BASE/'datasources.yaml',datasources)
write(BASE/'dashboards.yaml',{'apiVersion':1,'providers':[{'name':'Infrastructure','folder':'基础设施','type':'file','disableDeletion':True,'editable':False,'options':{'path':'/var/lib/grafana/dashboards'}}]})
panels=[]
def panel(title,expr,kind='timeseries',unit='short'):
 i=len(panels);panels.append({'id':i+1,'title':title,'type':kind,'gridPos':{'x':(i%2)*12,'y':(i//2)*8,'w':12,'h':8},'datasource':{'type':'prometheus','uid':'prometheus'},'targets':[{'refId':'A','expr':expr,'legendFormat':'{{node}} {{instance}} {{service}} {{backup}} {{topic}} {{alertname}}'}],'fieldConfig':{'defaults':{'unit':unit},'overrides':[]},'options':{'legend':{'displayMode':'list','placement':'bottom'},'tooltip':{'mode':'multi'}}})
panel('六台主机采集状态（1=正常）','up{job="nodes"}','stat')
panel('当前告警（维护备份待办也会显示）','ALERTS{alertstate="firing"}','table')
panel('CPU 使用率','1 - avg by(node)(rate(node_cpu_seconds_total{mode="idle"}[5m]))',unit='percentunit')
panel('可用内存','node_memory_MemAvailable_bytes',unit='bytes')
panel('系统盘可用空间','node_filesystem_avail_bytes{mountpoint="/"}',unit='bytes')
panel('关键系统服务（1=正常）','crawl_service_up','table')
panel('PG 当前主库（1=主库）','crawl_pg_is_primary','stat')
panel('PG 副本回放积压','crawl_pg_replica_replay_lag_bytes',unit='bytes')
panel('CDC guard 就绪','crawl_cdc_guard_ready','stat')
panel('复制槽保留 WAL','crawl_pg_slot_retained_bytes',unit='bytes')
panel('Kafka 每分区 ISR','kafka_topic_partition_in_sync_replica')
panel('Kafka 消费积压','sum by(topic,consumergroup)(kafka_consumergroup_lag)')
panel('Connect / Ingestor 就绪探针','crawl_http_probe_success')
panel('距离最近备份的时间','time() - crawl_backup_last_success_timestamp_seconds',unit='s')
panel('Deployment 可用副本','kube_deployment_status_replicas_available')
panel('StatefulSet 就绪副本','kube_statefulset_status_replicas_ready')
(BASE/'overview.json').write_text(json.dumps({'uid':'crawl-infrastructure','title':'爬虫平台 · 基础设施总览','schemaVersion':39,'version':1,'refresh':'30s','time':{'from':'now-1h','to':'now'},'timezone':'Asia/Shanghai','tags':['infrastructure'],'panels':panels},ensure_ascii=False,indent=2)+'\n')
vols=[];mounts=[]
for name,path in [('grafana-datasources','/etc/grafana/provisioning/datasources'),('grafana-dashboard-provider','/etc/grafana/provisioning/dashboards'),('grafana-dashboards','/var/lib/grafana/dashboards')]:
 v,m=volume(name,path);vols.append(v);mounts.append(m)
vols += [{'name':'cache','emptyDir':{}},{'name':'tmp','emptyDir':{}}]
mounts += [{'name':'cache','mountPath':'/var/lib/grafana'},{'name':'tmp','mountPath':'/tmp'}]
grafana_env={'GF_DATABASE_TYPE':'postgres','GF_DATABASE_HOST':'postgres-rw.crawl-validation.svc:5432','GF_DATABASE_NAME':'crawler_grafana','GF_DATABASE_USER':'crawl_grafana','GF_DATABASE_SSL_MODE':'disable','GF_DATABASE_MAX_OPEN_CONN':'10','GF_DATABASE_MAX_IDLE_CONN':'2','GF_DATABASE_LOCKING_ATTEMPT_TIMEOUT_SEC':'60','GF_SECURITY_ADMIN_USER':'admin','GF_USERS_ALLOW_SIGN_UP':'false','GF_AUTH_ANONYMOUS_ENABLED':'false','GF_ANALYTICS_REPORTING_ENABLED':'false','GF_ANALYTICS_CHECK_FOR_UPDATES':'false','GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES':'false','GF_PLUGINS_PREINSTALL_DISABLED':'true','GF_UNIFIED_ALERTING_EXECUTE_ALERTS':'false','GF_SERVER_ROOT_URL':'http://localhost:3000/','GF_LOG_MODE':'console'}
c,p=workload('grafana','grafana',3000,mem='384Mi',cpu='500m',uid=472,health='/api/health',env=envs(grafana_env),volumes=vols,mounts=mounts)
c['envFrom']=[{'secretRef':{'name':'grafana-private'}},{'secretRef':{'name':'grafana-logs-private'}}]
p['volumes'].append({'name':'clickhouse-plugin','hostPath':{'path':'/opt/crawlsystem/grafana-plugins/4.21.3/grafana-clickhouse-datasource','type':'Directory'}})
c['volumeMounts'].append({'name':'clickhouse-plugin','mountPath':'/var/lib/grafana/plugins/grafana-clickhouse-datasource','readOnly':True})

# Default deny plus explicit per-component access. No workload receives Secret read RBAC.
objects.append(resource('NetworkPolicy','default-deny',{'podSelector':{},'policyTypes':['Ingress','Egress']},'networking.k8s.io/v1'))
def ns(namespace,app=None):
 d={'namespaceSelector':{'matchLabels':{'kubernetes.io/metadata.name':namespace}}}
 if app:d['podSelector']={'matchLabels':{'app':app}}
 return d
def same(app):return {'podSelector':{'matchLabels':{'app':app}}}
def ports(*numbers):return [{'protocol':'TCP','port':p} for p in numbers]
dns={'to':[ns('kube-system')],'ports':ports(53)+[{'protocol':'UDP','port':53}]}
for name,port in [('prometheus',9090),('alertmanager',9093),('grafana',3000),('kube-state-metrics',8080),('kafka-exporter',9308),('infra-exporter',9189)]:
 ingress=[{'from':[same('prometheus')],'ports':ports(port)}]
 egress=[dns]
 if name=='prometheus':
  ingress += [{'from':[same('grafana')],'ports':ports(port)}]
  egress += [{'to':[same(app)],'ports':ports(p)} for app,p in [('prometheus',9090),('alertmanager',9093),('kube-state-metrics',8080),('kafka-exporter',9308),('infra-exporter',9189)]]
  egress += [{'to':[{'ipBlock':{'cidr':ip+'/32'}} for ip in NODES.values()],'ports':ports(9100)}]
 if name=='alertmanager':
  ingress += [{'from':[same('grafana')],'ports':ports(9093)},{'from':[same('alertmanager')],'ports':ports(9094)+[{'protocol':'UDP','port':9094}]}]
  egress += [{'to':[same('alertmanager')],'ports':ports(9094)+[{'protocol':'UDP','port':9094}]}]
 if name=='grafana':
  ingress += [{'from':[{'ipBlock':{'cidr':ip+'/32'}} for n,ip in NODES.items() if n.startswith('a')]+[{'ipBlock':{'cidr':ip+'/32'}} for ip in TUNNELS],'ports':ports(3000)}]
  egress += [{'to':[same('prometheus')],'ports':ports(9090)},{'to':[same('alertmanager')],'ports':ports(9093)},{'to':[ns('crawl-validation','postgresql-entry')],'ports':ports(5432)}]
 if name=='grafana':egress += [{'to':[{'ipBlock':{'cidr':'10.4.4.5/32'}}],'ports':ports(8443)}]
 if name=='kube-state-metrics':egress += [{'to':[{'ipBlock':{'cidr':ip+'/32'}} for n,ip in NODES.items() if n.startswith('a')]+[{'ipBlock':{'cidr':'10.96.0.1/32'}}],'ports':ports(443,6443)}]
 if name=='kafka-exporter':egress += [{'to':[{'ipBlock':{'cidr':ip+'/32'}} for n,ip in NODES.items() if n.startswith('s')],'ports':ports(9092)}]
 if name=='infra-exporter':egress += [{'to':[ns('crawl-validation','kafka-connect')],'ports':ports(8083)},{'to':[ns('crawl-validation','data-ingestor')],'ports':ports(8080)}]
 objects.append(resource('NetworkPolicy',name,{'podSelector':{'matchLabels':{'app':name}},'policyTypes':['Ingress','Egress'],'ingress':ingress,'egress':egress},'networking.k8s.io/v1'))
(BASE/'resources.yaml').write_text(yaml.safe_dump_all(objects,sort_keys=False))
(BASE/'infra-exporter.py').write_text((ROOT/'ops/monitoring/infra-exporter.py').read_text())
write(BASE/'kustomization.yaml',{'apiVersion':'kustomize.config.k8s.io/v1beta1','kind':'Kustomization','resources':['resources.yaml'],'configMapGenerator':[{'name':n,'files':f} for n,f in [('prometheus-config',['prometheus.yaml','rules.yaml']),('alertmanager-config',['alertmanager.yaml']),('infra-exporter-code',['infra-exporter.py']),('grafana-datasources',['datasources.yaml']),('grafana-dashboard-provider',['dashboards.yaml']),('grafana-dashboards',['overview.json','logs.json'])]]})
overlay=ROOT/'deploy/overlays/validation/monitoring';overlay.mkdir(parents=True,exist_ok=True)
write(overlay/'kustomization.yaml',{'apiVersion':'kustomize.config.k8s.io/v1beta1','kind':'Kustomization','namespace':'crawl-monitoring','resources':['../../../base/monitoring']})
project=resource('AppProject','crawl-monitoring',{'sourceRepos':['https://github.com/hjyl-cheng/newcrawlsystem-9-18.git'],'destinations':[{'server':'https://kubernetes.default.svc','namespace':'crawl-monitoring'}],'clusterResourceWhitelist':[],'namespaceResourceWhitelist':[{'group':g,'kind':k} for g,k in [('','ConfigMap'),('','Service'),('','ServiceAccount'),('','PersistentVolumeClaim'),('apps','Deployment'),('apps','StatefulSet'),('networking.k8s.io','NetworkPolicy'),('policy','PodDisruptionBudget')]]},'argoproj.io/v1alpha1');project['metadata']['namespace']='argocd'
write(ROOT/'deploy/argocd/bootstrap/monitoring-project.yaml',project)
app=resource('Application','crawl-monitoring',{'project':'crawl-monitoring','source':{'repoURL':'https://github.com/hjyl-cheng/newcrawlsystem-9-18.git','targetRevision':'main','path':'deploy/overlays/validation/monitoring'},'destination':{'server':'https://kubernetes.default.svc','namespace':'crawl-monitoring'},'syncPolicy':{'automated':{'prune':True,'selfHeal':True},'syncOptions':['PruneLast=true'],'retry':{'limit':3,'backoff':{'duration':'5s','factor':2,'maxDuration':'30s'}}}},'argoproj.io/v1alpha1');app['metadata']['namespace']='argocd'
write(ROOT/'deploy/argocd/bootstrap/monitoring.yaml',app)
print('Rendered monitoring manifests and separate cluster bootstrap resources.')
