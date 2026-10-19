# shellcheck shell=bash
# Shared by the installer bootstrap, host checks and CI SSH preflight.
supported_os() {
    [[ $1 == ubuntu && ( $2 == 24.04 || $2 == 26.04 ) ]]
}
check_supported_os() {
    local ID='' VERSION_ID=''
    # Trusted host metadata, never deployment input.
    # shellcheck disable=SC1091
    . /etc/os-release
    supported_os "$ID" "$VERSION_ID" || {
        printf 'Supported systems: Ubuntu 24.04 or 26.04 LTS; detected %s %s\n' "$ID" "$VERSION_ID" >&2
        return 1
    }
}
