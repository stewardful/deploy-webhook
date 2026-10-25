#!/usr/bin/env bash
# Install this receiver only. Platform ownership stays with independent cloudctl.
set -Eeuo pipefail
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
[[ $(id -u) == 0 ]] || { echo 'sudo required' >&2; exit 1; }
BINARY=${1:?pass a trusted deploy-webhook Linux executable after checking its SHA256}
[[ -f $BINARY && ! -L $BINARY ]] || exit 2
[[ $(readlink -f /usr/local/bin/cloudctl) == /opt/cloudctl/releases/*/cloudctl ]] || {
    echo 'Install the independent cloudctl from the deploy-platform release first (--cli-only).' >&2; exit 1;
}
install -d -m 0700 /etc/deploy-webhook
SECRET=/etc/deploy-webhook/webhook.secret
if [[ ! -e $SECRET && ! -L $SECRET ]]; then
    (umask 077; set -o noclobber; openssl rand -hex 32 > "$SECRET")
fi
[[ -f $SECRET && ! -L $SECRET ]] || exit 1
case $(stat -c '%a:%u' "$SECRET") in 600:0|400:0) ;; *) echo 'Secret must be root-owned 0600/0400; it was not changed.' >&2; exit 1;; esac
install -m 0755 "$BINARY" /usr/local/bin/deploy-webhook
for file in deploy-webhook.service deploy-webhook-worker.service deploy-webhook-worker.path; do
    install -m 0644 "$HERE/$file" "/etc/systemd/system/$file"
done
systemctl daemon-reload
systemctl enable --now deploy-webhook.service deploy-webhook-worker.path deploy-webhook-worker.service
printf 'Webhook receiver installed on loopback 8090. Configure HTTPS and the GitHub Releases Webhook.\n'
printf 'No platform installation was requested. Local cloudctl automation defaults off.\n'
