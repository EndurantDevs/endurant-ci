#!/usr/bin/env bash
# Explicit boundaries keep commands in the caller's shell with errexit intact.
ci_phase_begin() {
    [[ "$#" == 1 && "$1" =~ ^[a-z][a-z0-9-]*$ && -z "${ci_phase_name:-}" ]] || {
        printf '%s\n' 'Invalid or overlapping CI phase.' >&2
        return 2
    }
    ci_phase_name=$1
    ci_phase_started=$SECONDS
    printf 'CI_PHASE start name=%s\n' "$ci_phase_name"
}

ci_phase_end() {
    local status=${1:-0}
    if [[ -n "${ci_phase_name:-}" ]]; then
        printf 'CI_PHASE end name=%s elapsed_seconds=%s exit_code=%s\n' \
            "$ci_phase_name" "$((SECONDS - ci_phase_started))" "$status"
        ci_phase_name=''
    fi
    return 0
}
