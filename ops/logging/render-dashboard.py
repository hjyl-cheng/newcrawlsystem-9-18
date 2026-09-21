#!/usr/bin/env python3
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
ds={'type':'grafana-clickhouse-datasource','uid':'clickhouse-logs'}
def variable(name,label,query):return {'name':name,'label':label,'type':'query','datasource':ds,'query':{'rawSql':query,'queryType':'sql'},'refresh':2,'multi':True,'includeAll':True,'current':{'text':'All','value':['$__all']},'options':[]}
variables=[variable('node','服务器','SELECT DISTINCT Node FROM crawler_logs.events ORDER BY Node'),variable('service','服务','SELECT DISTINCT Service FROM crawler_logs.events WHERE $__timeFilter(Timestamp) ORDER BY Service LIMIT 200'),{'name':'search','label':'内容包含','type':'textbox','query':'','current':{'text':'','value':''}}]
where='WHERE $__timeFilter(Timestamp) AND Node IN (${node:sqlstring}) AND Service IN (${service:sqlstring}) AND positionCaseInsensitiveUTF8(Body, ${search:sqlstring}) > 0'
panels=[{'id':1,'title':'日志数量（按级别）','type':'timeseries','gridPos':{'x':0,'y':0,'w':24,'h':7},'datasource':ds,'targets':[{'refId':'A','queryType':'sql','editorType':'sql','format':0,'rawSql':'SELECT toStartOfInterval(Timestamp, INTERVAL $__interval_s SECOND) AS time, SeverityText AS metric, count() AS count FROM crawler_logs.events '+where+' GROUP BY time,metric ORDER BY time'}]}, {'id':2,'title':'日志详情（最近 200 条）','type':'logs','gridPos':{'x':0,'y':7,'w':24,'h':19},'datasource':ds,'targets':[{'refId':'A','queryType':'sql','editorType':'sql','format':2,'rawSql':'SELECT Timestamp AS timestamp, Body AS body, SeverityText AS level, Node, Service, Source, Namespace, Pod, Container FROM crawler_logs.events '+where+' ORDER BY Timestamp DESC LIMIT 200'}],'options':{'showTime':True,'wrapLogMessage':True,'sortOrder':'Descending','dedupStrategy':'none','showLabels':True,'enableLogDetails':True}}]
d={'uid':'crawl-logs','title':'爬虫平台 · 集中日志','schemaVersion':39,'version':1,'refresh':'30s','timezone':'Asia/Shanghai','time':{'from':'now-1h','to':'now'},'tags':['infrastructure','logs'],'templating':{'list':variables},'panels':panels}
(ROOT/'deploy/base/monitoring/logs.json').write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n')
