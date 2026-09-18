#!/usr/bin/env python3
"""Resolve a CI image, check its source identity, and pin the validation overlay."""
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

repository = "hjyl-cheng/newcrawlsystem-runtime"
if len(sys.argv) != 2 or not re.fullmatch(r"[0-9a-f]{40}", sys.argv[1]):
    raise SystemExit("Usage: pin-runtime-image.py <full-source-git-sha>")
revision = sys.argv[1]


def read(url, headers=None):
    with urlopen(Request(url, headers=headers or {}), timeout=30) as response:
        return response.read()


try:
    token = json.loads(read(
        f"https://ghcr.io/token?service=ghcr.io&scope=repository:{repository}:pull"
    ))["token"]
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": ", ".join([
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
            "application/vnd.docker.distribution.manifest.v2+json",
        ]),
    }
    base = f"https://ghcr.io/v2/{repository}"
    raw = read(f"{base}/manifests/sha-{revision}", headers)
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    manifest = json.loads(raw)
    if "manifests" in manifest:
        amd64 = next(m for m in manifest["manifests"]
                     if m.get("platform", {}).get("architecture") == "amd64"
                     and m.get("platform", {}).get("os") == "linux")
        manifest = json.loads(read(f"{base}/manifests/{amd64['digest']}", headers))
    config = json.loads(read(f"{base}/blobs/{manifest['config']['digest']}", headers))["config"]
    env = dict(item.split("=", 1) for item in config.get("Env", []))
    if (config.get("Labels", {}).get("org.opencontainers.image.revision") != revision
            or env.get("APP_REVISION") != revision or not env.get("APP_VERSION")):
        raise SystemExit("Image revision/version metadata does not match the expected source")
except HTTPError as error:
    raise SystemExit(f"GHCR returned HTTP {error.code}; check package visibility and the completed CI run") from None

root = Path(__file__).resolve().parents[2]
overlay = root / "deploy/overlays/validation/runtime-smoke/kustomization.yaml"
overlay.parent.mkdir(parents=True, exist_ok=True)
overlay.write_text(
    "apiVersion: kustomize.config.k8s.io/v1beta1\n"
    "kind: Kustomization\n"
    "namespace: crawl-validation\n"
    "resources:\n  - ../../../base/runtime-smoke\n"
    "images:\n"
    f"  - name: ghcr.io/{repository}\n"
    f"    digest: {digest}\n"
)
print(f"Pinned ghcr.io/{repository}@{digest}")
print(f"Source: {revision}; version: {env['APP_VERSION']}")
