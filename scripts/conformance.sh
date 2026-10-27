#!/bin/sh
# Proves every edition produces the expected payload from the fixtures,
# and that the Function edition carries the same collector as python/.
# Needs python3; pwsh and docker are used when present (CI installs
# pwsh, so the PowerShell edition is checked there too).
set -eu
cd "$(dirname "$0")/.."
out="$(mktemp -d)"

python3 -I python/stashless_collector.py --fixtures fixtures --out "$out/python.json"
python3 scripts/diff-payload.py "$out/python.json" fixtures/expected-payload.json python

cmp python/stashless_collector.py azure-function/stashless_collector.py && echo "azure-function: carries the same collector as python/"

python3 scripts/check-permission-set.py ifs/CMDB_READ-permission-set.xml

if command -v pwsh >/dev/null 2>&1; then
    pwsh -NoProfile -NonInteractive -File powershell/Stashless-Collector.ps1 -Fixtures fixtures -Out "$out/powershell.json"
    python3 scripts/diff-payload.py "$out/powershell.json" fixtures/expected-payload.json powershell
else
    echo "powershell: skipped, pwsh is not installed"
fi

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    docker build -q -f docker/Dockerfile -t stashless-collector-conformance . >/dev/null
    docker run --rm -v "$PWD/fixtures:/fixtures:ro" stashless-collector-conformance --fixtures /fixtures --print > "$out/docker.json" 2>/dev/null
    python3 scripts/diff-payload.py "$out/docker.json" fixtures/expected-payload.json docker
else
    echo "docker: skipped, docker is not available"
fi
