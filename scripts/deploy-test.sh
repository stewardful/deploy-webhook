#!/usr/bin/env bash
# GitHub runner transport only. Never embed Secrets in remote command strings.
set -Eeuo pipefail
umask 077
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
: "${TEST_SSH_PRIVATE_KEY:?Set TEST_SSH_PRIVATE_KEY in GitHub Secrets}"
: "${TEST_SSH_KNOWN_HOSTS:?Set verified TEST_SSH_KNOWN_HOSTS in GitHub Secrets}"
: "${TEST_SERVER_HOST:?Set TEST_SERVER_HOST in GitHub Secrets}"
: "${TEST_SERVER_USER:?Set TEST_SERVER_USER in GitHub Secrets}"
: "${CI_ARTIFACT_DIR:=$ROOT/dist}"
: "${TEST_SERVER_ARCH:=amd64}" "${TEST_SERVER_PORT:=22}" "${DEPLOY_WAIT_SECONDS:=1800}"
: "${GITHUB_RUN_ID:?}" "${GITHUB_RUN_ATTEMPT:?}" "${GITHUB_SHA:?}" "${RELEASE_VERSION:?}"
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
stage=/var/tmp/deploy-platform-ci-$run
printf '%s\n' 'Checking server OS, architecture, systemd, cgroup and sudo before HTTPS delivery'
{
cat "$ROOT/scripts/os.sh"
cat <<'PREPARE'
set -Eeuo pipefail
umask 077
command -v python3 >/dev/null
command -v systemd-run >/dev/null
sudo -n true
check_supported_os
case $(uname -m) in x86_64) arch=amd64;; aarch64|arm64) arch=arm64;; *) echo 'unsupported server architecture' >&2; exit 1;; esac
[[ $arch == "$2" ]] || { echo "server architecture is $arch, installer architecture is $2" >&2; exit 1; }
[[ -f /sys/fs/cgroup/cgroup.controllers && -d /run/systemd/system ]] || { echo 'systemd and cgroup v2 required' >&2; exit 1; }
systemctl show-environment >/dev/null
available=$(df -Pk /opt | awk 'NR==2 {print $4}')
(( available >= 2097152 )) || { echo 'at least 2 GiB available disk required' >&2; exit 1; }
printf 'PASS: supported OS and architecture %s; HTTPS download can begin\n' "$arch"
: # The root control entry point creates its own private stage after validation.
PREPARE
} | ssh_control bash -s -- "$run" "$TEST_SERVER_ARCH"
control_command=$(python3 - "$ROOT/scripts/remote-control.py" <<'PYCONTROL'
from pathlib import Path
import shlex,sys
print('sudo -n python3 -c '+shlex.quote(Path(sys.argv[1]).read_text()))
PYCONTROL
)
# The bootstrap contains only small control metadata and downloader source, never installer bytes.
python3 "$ROOT/scripts/artifact-request.py" authorize "$work/request.json" > "$work/control.json"
ssh_control "$control_command start" < "$work/control.json"
rm -f "$work/control.json"
last_progress='' revision=1
end=$((SECONDS + 10#$DEPLOY_WAIT_SECONDS))
while (( SECONDS < end )); do
    if status=$(ssh_control sudo -n cat "$stage/result.json" 2>/dev/null); then
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
                ssh_control "$control_command refresh" < "$work/control.json"
                rm -f "$work/control.json";;
            succeeded)
                printf '%s\n' 'PASS: test server installer committed and application verification passed'
                if [[ -n ${GITHUB_STEP_SUMMARY:-} ]]; then printf 'Test deployment succeeded: %s (run %s)\n' "$GITHUB_SHA" "$run" >> "$GITHUB_STEP_SUMMARY"; fi
                exit 0;;
            failed)
                printf '%s\n' 'Test deployment failed; private installer log follows.' >&2
                ssh_control sudo -n tail -n 100 "$stage/install.log" || true
                exit 1;;
        esac
    fi
    sleep 5
done
printf '%s\n' "Timed out waiting; remote unit deploy-platform-ci-$run may still be running. Reconnect and inspect it before retrying." >&2
exit 1
