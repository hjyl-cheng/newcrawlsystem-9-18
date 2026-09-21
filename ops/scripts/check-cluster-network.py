#!/usr/bin/env python3
"""Verify Argo-managed probes on A1/A2/A3; no credentials required."""
import json
import subprocess
import sys
from pathlib import Path


def run(args):
    return subprocess.check_output(args, text=True, timeout=20).strip()


namespace = "crawl-validation"
expected = sys.argv[1] if len(sys.argv) > 1 else (Path(__file__).resolve().parents[2] / "deploy/base/infra-smoke/index.html").read_text().strip()
pods = json.loads(run([
    "kubectl", "get", "pods", "-n", namespace, "-l", "app=infra-smoke", "-o", "json"
]))["items"]
assert len(pods) == 3, "Expected three probe Pods"
assert {p["spec"]["nodeName"] for p in pods} == {"a1", "a2", "a3"}
for pod in pods:
    node = pod["spec"]["nodeName"]
    prefix = ["kubectl", "exec", "-n", namespace, pod["metadata"]["name"], "--"]
    for target in pods:
        ip = target["status"]["podIP"]
        value = run(prefix + ["wget", "-qO-", "-T", "5", f"http://{ip}:8080/"])
        assert value == expected, (node, ip, value)
        print(f"{node} -> {target['spec']['nodeName']} Pod HTTP: PASS")
    run(prefix + ["nslookup", "kubernetes.default.svc.cluster.local"])
    value = run(prefix + ["wget", "-qO-", "-T", "5",
                          "http://infra-smoke.crawl-validation.svc.cluster.local:8080/"])
    assert value == expected, (node, value)
    print(f"{node} -> cluster DNS + Service: PASS")
    # TCP reachability only; database authentication is tested separately.
    for host, ports in [("10.4.4.2", [5432, 6432, 9094]),
                        ("10.4.4.8", [5432, 9094]),
                        ("10.4.4.5", [5432, 9094, 8333, 8123, 9000])]:
        for port in ports:
            run(prefix + ["sh", "-c", 'nc -w 3 "$1" "$2" < /dev/null > /dev/null',
                          "probe", host, str(port)])
    print(f"{node} -> storage TCP endpoints: PASS")
