#!/usr/bin/env bash
# Build only the pinned CPU runtime inside the caller's owned build directory.
set -euo pipefail
IFS=$'\n\t'

# Phase-only diagnostics; the Python caller bounds command output.
log() {
    printf '%s\n' "$*" >&2
}

# Preserve the prepared receipt and make incomplete builds visible.
die() {
    log "CPU build failed: $*"
    exit 1
}

main() {
    local root="${1:?}" git="${2:?}" cmake="${3:?}"
    local revision="${4:?}" jobs="${5:?}"
    local source="${root}/source"
    local origin head
    if [[ ! -e "${source}" ]]; then
        "${git}" init -q "${source}" || die 'initialize source directory'
    fi
    origin="$("${git}" -C "${source}" remote get-url origin 2>/dev/null || true)"
    if [[ -z "${origin}" ]]; then
        "${git}" -C "${source}" remote add origin \
            https://github.com/ggml-org/llama.cpp.git || die 'set source remote'
    else
        [[ "${origin}" == https://github.com/ggml-org/llama.cpp.git ]] \
            || die 'source remote differs'
    fi
    head="$("${git}" -C "${source}" rev-parse --verify HEAD 2>/dev/null || true)"
    if [[ -z "${head}" ]]; then
        log 'Fetch pinned llama.cpp source'
        "${git}" -C "${source}" fetch --quiet --depth 1 origin "${revision}" \
            || die 'fetch pinned source'
        "${git}" -C "${source}" -c advice.detachedHead=false \
            checkout --quiet --detach FETCH_HEAD || die 'select source commit'
    fi
    [[ "$("${git}" -C "${source}" rev-parse HEAD)" == "${revision}" ]] \
        || die 'source commit differs'
    "${git}" -C "${source}" diff --quiet || die 'source was edited'
    "${git}" -C "${source}" diff --cached --quiet || die 'source index was edited'
    "${cmake}" -E rm -rf "${source}/build/tools/ui/dist" \
        || die 'remove incomplete generated UI assets'
    log 'Configure CPU runtime'
    "${cmake}" -S "${source}" -B "${source}/build" \
        -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=OFF \
        -DGGML_VULKAN=OFF -DGGML_CUDA=OFF -DLLAMA_CURL=OFF \
        -DLLAMA_BUILD_UI=OFF -DLLAMA_USE_PREBUILT_UI=OFF || die 'configure'
    log 'Compile CPU runtime'
    "${cmake}" --build "${source}/build" --target llama-server \
        --parallel "${jobs}" || die 'compile'
    [[ -x "${source}/build/bin/llama-server" ]] || die 'executable missing'
}

main "$@"
