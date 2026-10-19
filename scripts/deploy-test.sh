#!/usr/bin/env bash
# GitHub runner transport only. Never embed Secrets in remote command strings.
set -Eeuo pipefail
umask 077
export PYTHONDONTWRITEBYTECODE=1
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
: "${TEST_SSH_PRIVATE_KEY:?Set TEST_SSH_PRIVATE_KEY in GitHub Secrets}"
: "${TEST_SSH_KNOWN_HOSTS:?Set verified TEST_SSH_KNOWN_HOSTS in GitHub Secrets}"
: "${TEST_SERVER_HOST:?Set TEST_SERVER_HOST in GitHub Secrets}"
: "${TEST_SERVER_USER:?Set TEST_SERVER_USER in GitHub Secrets}"
: "${CI_ARTIFACT_DIR:=$ROOT/dist}"
: "${TEST_SERVER_ARCH:=amd64}" "${TEST_SERVER_PORT:=22}" "${DEPLOY_WAIT_SECONDS:=1800}"
: "${GITHUB_REPOSITORY:?}" "${GITHUB_RUN_ID:?}" "${GITHUB_RUN_ATTEMPT:?}" "${GITHUB_SHA:?}" "${RELEASE_VERSION:?}"
[[ $TEST_SERVER_ARCH == amd64 || $TEST_SERVER_ARCH == arm64 ]]
[[ $TEST_SERVER_PORT =~ ^[0-9]+$ && ${#TEST_SERVER_PORT} -le 5 ]] && (( 10#$TEST_SERVER_PORT >= 1 && 10#$TEST_SERVER_PORT <= 65535 ))
[[ $GITHUB_RUN_ID =~ ^[0-9]{1,20}$ && $GITHUB_RUN_ATTEMPT =~ ^[0-9]{1,6}$ && $GITHUB_SHA =~ ^[a-f0-9]{40}$ ]]
[[ $RELEASE_VERSION =~ ^[0-9]+\.[0-9]+\.[0-9]+$ && $TEST_SERVER_USER =~ ^[a-z_][a-z0-9_-]{0,31}$ ]]
[[ $DEPLOY_WAIT_SECONDS =~ ^[0-9]{1,5}$ ]] && (( 10#$DEPLOY_WAIT_SECONDS >= 60 && 10#$DEPLOY_WAIT_SECONDS <= 7200 ))
python3 - <<'PY'
import ipaddress, os, re
host=os.environ['TEST_SERVER_HOST']
if '%' in host: raise SystemExit('scoped IPv6 is not supported')
try: ipaddress.ip_address(host)
except ValueError:
    if len(host)>253 or not all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label) for label in host.split('.')):
        raise SystemExit('invalid TEST_SERVER_HOST')
PY
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
printf '%s\n' "$TEST_SSH_PRIVATE_KEY" | tr -d '\r' > "$work/key"
printf '%s\n' "$TEST_SSH_KNOWN_HOSTS" | tr -d '\r' > "$work/known_hosts"
unset TEST_SSH_PRIVATE_KEY TEST_SSH_KNOWN_HOSTS
ssh-keygen -y -P '' -f "$work/key" >/dev/null
ssh-keygen -l -f "$work/known_hosts" >/dev/null
export CI_ARTIFACT_DIR TEST_SERVER_ARCH
# This metadata file has no credentials. URL issuance happens only after host preflight.
python3 "$ROOT/scripts/artifact-request.py" prepare "$work/request.json"
options=(-F /dev/null -i "$work/key" -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$work/known_hosts" -o GlobalKnownHostsFile=/dev/null -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=3)
destination=$TEST_SERVER_USER@$TEST_SERVER_HOST
ssh_control() { env -u GH_TOKEN -u GITHUB_TOKEN ssh "${options[@]}" -p "$TEST_SERVER_PORT" "$destination" "$@"; }
run=$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT
printf '%s\n' 'Checking independently installed delivery tool and server policy'
# Fixed installed command only. Never transfer executable source or bootstrap remotely.
if ! ssh_control sudo -n /usr/local/bin/deploy-delivery preflight "$TEST_SERVER_ARCH" > "$work/preflight.json"; then
    echo 'Install/configure deploy-delivery on the server first; see deploy-delivery installation guide.' >&2
    exit 1
fi
python3 - "$work/preflight.json" <<'PREFLIGHT'
import json,os,sys
value=json.load(open(sys.argv[1]))
if value.get('protocol') != 1 or value.get('phase') != 'ready' or value.get('arch') != os.environ['TEST_SERVER_ARCH'] or value.get('repository') != os.environ['GITHUB_REPOSITORY']:
    raise SystemExit('installed delivery protocol or repository policy differs')
PREFLIGHT
python3 "$ROOT/scripts/artifact-request.py" authorize "$work/request.json" > "$work/control.json"
ssh_control sudo -n /usr/local/bin/deploy-delivery start < "$work/control.json"
rm -f "$work/control.json"
last_progress='' revision=1
end=$((SECONDS + 10#$DEPLOY_WAIT_SECONDS))
while (( SECONDS < end )); do
    if status=$(ssh_control sudo -n /usr/local/bin/deploy-delivery status "$GITHUB_RUN_ID" "$GITHUB_RUN_ATTEMPT" 2>/dev/null); then
        phase=$(printf '%s' "$status" | python3 -c 'import json,sys; print(json.load(sys.stdin)["phase"])')
        progress=$(printf '%s' "$status" | python3 -c 'import json,sys; s=json.load(sys.stdin); print(s["phase"], str(s.get("downloaded_bytes",0))+"/"+str(s.get("total_bytes",0))+" bytes",s.get("error_code",""))')
        if [[ $progress != "$last_progress" ]]; then printf '%s\n' "$progress"; last_progress=$progress; fi
        case $phase in
            awaiting_url)
                requested=$(printf '%s' "$status" | python3 -c 'import json,sys; print(int(json.load(sys.stdin)["url_revision"]))')
                if (( requested < revision )); then sleep 1; continue; fi
                revision=$((requested+1))
                if (( revision > 4 )); then echo 'Download authorization refresh limit exceeded; inspect remote result' >&2; exit 1; fi
                URL_REVISION=$revision python3 "$ROOT/scripts/artifact-request.py" authorize "$work/request.json" > "$work/control.json"
                ssh_control sudo -n /usr/local/bin/deploy-delivery refresh < "$work/control.json"
                rm -f "$work/control.json";;
            succeeded)
                printf '%s\n' 'PASS: test server installer committed and application verification passed'
                if [[ -n ${GITHUB_STEP_SUMMARY:-} ]]; then printf 'Test deployment succeeded: %s (run %s)\n' "$GITHUB_SHA" "$run" >> "$GITHUB_STEP_SUMMARY"; fi
                exit 0;;
            failed)
                printf '%s\n' 'Test deployment failed; private installer log follows.' >&2
                ssh_control sudo -n /usr/local/bin/deploy-delivery logs "$GITHUB_RUN_ID" "$GITHUB_RUN_ATTEMPT" || true
                exit 1;;
        esac
    fi
    sleep 5
done
printf '%s\n' "Timed out waiting; remote unit deploy-platform-ci-$run may still be running. Reconnect and inspect it before retrying." >&2
exit 1
