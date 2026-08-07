#!/usr/bin/env bash
#
# Runs the budget ledger tests against a disposable MySQL instance.
#
# This never touches a MySQL server you already run. It starts its own container, on its
# own port, under its own name, and removes it afterwards. If that container name is
# already taken the script stops rather than reusing it, because reusing it would mean
# asserting against somebody else's data.
#
# What it proves that a mock cannot: that SELECT ... FOR UPDATE genuinely serializes two
# concurrent transactions. A mock that serializes them is a mock that assumes the answer.

set -euo pipefail

CONTAINER=playerbot-llm-ledger-itest
PORT=33062
DATABASE=playerbot_llm_ledger_itest
PASSWORD=itest
IMAGE=mysql:8.0

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SIDECAR="$(dirname "${HERE}")"

if docker ps -a --format '{{.Names}}' | grep -qx "${CONTAINER}"; then
  echo "container ${CONTAINER} already exists; remove it first" >&2
  exit 1
fi

cleanup() {
  if [ "${KEEP:-0}" != "1" ]; then
    docker rm -f "${CONTAINER}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

echo "starting disposable mysql on port ${PORT}"
docker run -d --name "${CONTAINER}" \
  -e "MYSQL_ROOT_PASSWORD=${PASSWORD}" \
  -e "MYSQL_DATABASE=${DATABASE}" \
  -p "127.0.0.1:${PORT}:3306" \
  "${IMAGE}" >/dev/null

ready=0
for _ in $(seq 1 60); do
  if docker exec "${CONTAINER}" mysqladmin ping -uroot "-p${PASSWORD}" --silent >/dev/null 2>&1; then
    ready=1
    break
  fi
  printf .
  sleep 2
done
echo
if [ "${ready}" -ne 1 ]; then
  echo "mysql never became ready" >&2
  exit 1
fi

# mysqladmin answers slightly before the server accepts real work.
sleep 5

export PLAYERBOT_LLM_TEST_MYSQL_DSN="127.0.0.1;${PORT};root;${PASSWORD};${DATABASE}"

cd "${SIDECAR}"
uv run python -m pytest tests/test_ledger_mysql.py -q -m mysql
