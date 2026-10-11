#!/usr/bin/env python3
"""Publish the production dashboard snapshot to Tencent CloudBase when it changes."""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
from build_production_dashboard import capture_snapshot, sha256
ENV_ID = "tengtiao-calc-d8gpq679da44f9bc2"
CLOUD_PATH = "production-dashboard"
TCB = Path(
    "/Users/xianer/.local/share/fnm/node-versions/v24.21.0/installation/"
    "lib/node_modules/@cloudbase/cli/bin/tcb"
)
STATE = Path("/Users/xianer/.openclaw/state/production-parallel/cloudbase-dashboard-publish.json")
LOG = Path("/Users/xianer/.openclaw/logs/cloudbase-dashboard-publish.log")
LOCK = Path("/Users/xianer/.openclaw/state/production-parallel/cloudbase-dashboard-publish.lock")
PUBLIC_URL = f"https://tengtiao-calc-d8gpq679da44f9bc2-1497888928.tcloudbaseapp.com/{CLOUD_PATH}/index.html"
CALCULATOR_SOURCE = REPO / 'docs/teng-tiao-calculator/index.html'
CALCULATOR_CLOUD_PATH = "teng-tiao-calculator"
CALCULATOR_PUBLIC_URL = f"https://tengtiao-calc-d8gpq679da44f9bc2-1497888928.tcloudbaseapp.com/{CALCULATOR_CLOUD_PATH}/index.html"
COMPATIBILITY_FILES = {
    'production-dashboard.html': REPO / 'docs/production-dashboard.html',
}


def verify_public(expected: dict[str, str]) -> None:
    """A CLI success alone does not prove the files served to a phone changed."""
    for filename, expected_hash in expected.items():
        url = urllib.parse.urljoin(PUBLIC_URL, filename) + '?_verify=' + str(time.time_ns())
        request = urllib.request.Request(url, headers={'Cache-Control': 'no-cache'})
        with urllib.request.urlopen(request, timeout=20) as response:
            if response.status != 200 or sha256(response.read()) != expected_hash:
                raise RuntimeError(f'public {filename} differs from the published snapshot')


def write_state(state: dict) -> None:
    # Replace atomically so status readers never see half a JSON document.
    temporary = STATE.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(STATE)


def log(message: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S %z')} {message}\n")


def main() -> int:
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("w") as lock_fh:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        if not TCB.is_file():
            log('publish failed: tcb executable missing')
            return 1
        try:
            rendered, manifest, release = capture_snapshot()
            compatibility = {name: path.read_bytes() for name, path in COMPATIBILITY_FILES.items()}
            calculator_content = CALCULATOR_SOURCE.read_bytes() if CALCULATOR_SOURCE.is_file() else None
        except Exception as exc:
            log(f'publish failed: snapshot build {type(exc).__name__}: {str(exc)[:250]}')
            return 1
        current = release['source_digest']
        compatibility_hashes = {name: sha256(content) for name, content in compatibility.items()}
        # The domain root was a separate old production page. Update only its
        # index.html file, never deploy a directory to the domain root.
        compatibility_hashes['/index.html'] = compatibility_hashes['production-dashboard.html']
        if calculator_content is not None:
            compatibility_hashes[f'/{CALCULATOR_CLOUD_PATH}/index.html'] = sha256(calculator_content)
        previous = {}
        if STATE.is_file():
            try:
                previous = json.loads(STATE.read_text(encoding="utf-8"))
            except Exception:
                previous = {}
        previous_hashes = previous.get('public_sha256') or {}
        if (previous.get('digest') == current and previous_hashes
                and all(previous_hashes.get(name) == digest
                        for name, digest in compatibility_hashes.items())):
            # Keep the content digest stable; a generated timestamp alone never causes a deploy.
            if time.time() - previous.get('verified_unix', 0) < 300:
                return 0
            try:
                verify_public(previous['public_sha256'])
                previous['verified_unix'] = time.time()
                previous['verified_at'] = time.strftime('%Y-%m-%dT%H:%M:%S%z')
                write_state(previous)
                return 0
            except Exception as exc:
                log(f'public verification failed; republishing: {type(exc).__name__}')
        public_hashes = {'index.html': sha256(rendered), 'release.json': sha256(manifest),
                         **compatibility_hashes}
        with tempfile.TemporaryDirectory(prefix="cloudbase-production-dashboard-") as tmp:
            output = Path(tmp) / "index.html"
            output.write_bytes(rendered)
            (Path(tmp) / 'release.json').write_bytes(manifest)
            for filename, content in compatibility.items():
                (Path(tmp) / filename).write_bytes(content)
            if calculator_content is not None:
                calculator_file = Path(tmp) / 'teng-tiao-calculator.html'
                calculator_file.write_bytes(calculator_content)
            command = [
                str(TCB),
                "hosting",
                "deploy",
                tmp,
                CLOUD_PATH,
                "-e",
                ENV_ID,
                "--safe",
                "--verify",
                "--json",
            ]
            root_command = [
                str(TCB), "hosting", "deploy",
                str(Path(tmp) / 'production-dashboard.html'), "index.html",
                "-e", ENV_ID, "--safe", "--verify", "--json",
            ]
            publish_env = dict(os.environ)
            node_bin = "/Users/xianer/.local/share/fnm/node-versions/v24.21.0/installation/bin"
            publish_env["PATH"] = node_bin + ":" + publish_env.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
            deployments = [command, root_command]
            if calculator_content is not None:
                deployments.append([
                    str(TCB),
                    'hosting',
                    'deploy',
                    str(calculator_file),
                    f'{CALCULATOR_CLOUD_PATH}/index.html',
                    '-e', ENV_ID,
                    '--safe', '--verify', '--json',
                ])
            for deployment in deployments:
                try:
                    result = subprocess.run(deployment, text=True, capture_output=True, timeout=120, env=publish_env)
                except Exception as exc:
                    log(f'publish failed: {type(exc).__name__}')
                    return 1
                if result.returncode != 0:
                    detail = (result.stderr or result.stdout).strip().replace("\n", " ")
                    log(f"publish failed rc={result.returncode} {detail[:500]}")
                    return result.returncode
        # CDN propagation may take a few seconds. Never record success before readback matches.
        for attempt in range(4):
            try:
                verify_public(public_hashes)
                break
            except Exception as exc:
                if attempt == 3:
                    log(f'publish verification failed: {type(exc).__name__}: {str(exc)[:250]}')
                    return 1
                time.sleep(2)
        write_state({
            'digest': current,
            'published_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
            'verified_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
            'verified_unix': time.time(),
            'url': PUBLIC_URL,
            'public_sha256': public_hashes,
            'workbench_version': release['workbench_version'],
            'rules_release_id': release['rules_release_id'],
            'ledger_sha256': release['ledger_sha256'],
            'ledgers_sha256': release['ledgers_sha256'],
            'available_years': release['available_years'],
            'version_sha256': release['version_sha256'],
            'verification': 'public_readback_passed',
        })
        log(f"published and verified {release['workbench_version']} digest={current[:12]}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
