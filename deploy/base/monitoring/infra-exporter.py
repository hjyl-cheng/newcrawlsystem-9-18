#!/usr/bin/env python3
"""Read-only, bounded probes. Never expose Connect config, logs or identifiers."""
import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONNECT = 'http://kafka-connect.crawl-validation.svc:8083'
CONNECTOR = 'crawl-publication-validation'
INGESTOR = 'http://data-ingestor.crawl-validation.svc:8080/health/ready'
snapshot = b''


def fetch(url):
    with urllib.request.urlopen(url, timeout=3) as response:
        return json.load(response)


def collect():
    global snapshot
    while True:
        metrics = []
        try:
            status = fetch(CONNECT + '/connectors/' + CONNECTOR + '/status')
            running = status['connector']['state'] == 'RUNNING'
            tasks = status['tasks']
            metrics += [f'crawl_connect_connector_running {int(running)}',
                        f'crawl_connect_tasks_running {int(bool(tasks) and all(t["state"] == "RUNNING" for t in tasks))}',
                        'crawl_http_probe_success{component="connect"} 1']
        except Exception:
            metrics += ['crawl_http_probe_success{component="connect"} 0']
        try:
            fetch(INGESTOR)
            ok = 1
        except Exception:
            ok = 0
        metrics += [f'crawl_http_probe_success{{component="ingestor"}} {ok}',
                    f'crawl_http_probe_last_run_timestamp_seconds {time.time()}']
        snapshot = ('\n'.join(metrics) + '\n').encode()
        time.sleep(15)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ['/metrics', '/health']:
            self.send_error(404)
            return
        body = snapshot if self.path == '/metrics' else b'ok\n'
        self.send_response(200 if snapshot else 503)
        self.send_header('Content-Type', 'text/plain; version=0.0.4; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


if __name__ == '__main__':
    threading.Thread(target=collect, daemon=True).start()
    ThreadingHTTPServer(('0.0.0.0', 9189), Handler).serve_forever()
