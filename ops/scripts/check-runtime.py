#!/usr/bin/env python3
"""Check every runtime replica and its Service from all three A nodes."""
import json
import subprocess
import sys


def run(args):
    return subprocess.check_output(args, text=True, timeout=20).strip()


if len(sys.argv) != 2:
    raise SystemExit("Usage: check-runtime.py <expected-full-source-git-sha>")
expected = sys.argv[1]
namespace = "crawl-validation"


def pods(label):
    return json.loads(run([
        "kubectl", "get", "pods", "-n", namespace, "-l", label, "-o", "json"
    ]))["items"]


runtime = pods("app=runtime-smoke")
assert len(runtime) == 2, "Expected two runtime replicas after rollout"
assert len({p["spec"]["nodeName"] for p in runtime}) == 2, "Replicas must span nodes"
for pod in runtime:
    assert any(c["type"] == "Ready" and c["status"] == "True"
               for c in pod["status"]["conditions"]), pod["metadata"]["name"]
    assert "@sha256:" in pod["spec"]["containers"][0]["image"]
probes = pods("app=infra-smoke")
assert {p["spec"]["nodeName"] for p in probes} == {"a1", "a2", "a3"}
targets = [p["status"]["podIP"] for p in runtime]
targets.append("runtime-smoke.crawl-validation.svc.cluster.local")
for probe in probes:
    prefix = ["kubectl", "exec", "-n", namespace, probe["metadata"]["name"],
              "--", "wget", "-qO-", "-T", "5"]
    for target in targets:
        base = f"http://{target}:8080"
        for path, status in [("/health/live", "ok"), ("/health/ready", "ready")]:
            assert json.loads(run(prefix + [base + path]))["status"] == status
        version = json.loads(run(prefix + [base + "/version"]))
        assert version["revision"] == expected, version
        print(f"{probe['spec']['nodeName']} -> {target}: health/version PASS ({version['version']})")
