#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_SRC="${REPO_DIR}/klippy/extras/pn5180.py"
DEFAULT_KLIPPER_DIR="${HOME}/klipper"
KLIPPER_DIR="${KLIPPER_DIR:-${DEFAULT_KLIPPER_DIR}}"
KLIPPER_EXTRAS_DIR="${KLIPPER_DIR}/klippy/extras"
PLUGIN_DST="${KLIPPER_EXTRAS_DIR}/pn5180.py"

info() {
    printf '[PN5180] %s\n' "$*"
}

die() {
    printf '[PN5180] ERROR: %s\n' "$*" >&2
    exit 1
}

is_git_repo() {
    git -C "${REPO_DIR}" rev-parse --is-inside-work-tree >/dev/null 2>&1
}

update_repo() {
    local branch upstream local_rev remote_rev

    if ! is_git_repo; then
        info "This is not a git checkout; skipping update check."
        return
    fi

    if ! git -C "${REPO_DIR}" remote get-url origin >/dev/null 2>&1; then
        info "No origin remote configured; skipping update check."
        return
    fi

    info "Checking GitHub for updates..."
    git -C "${REPO_DIR}" fetch --quiet origin

    branch="$(git -C "${REPO_DIR}" symbolic-ref --quiet --short HEAD || true)"
    if [ -z "${branch}" ]; then
        info "Detached HEAD; skipping automatic update."
        return
    fi

    upstream="$(git -C "${REPO_DIR}" rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
    if [ -z "${upstream}" ]; then
        upstream="origin/${branch}"
    fi

    if ! git -C "${REPO_DIR}" rev-parse --verify "${upstream}" >/dev/null 2>&1; then
        info "No upstream branch found for ${branch}; skipping automatic update."
        return
    fi

    local_rev="$(git -C "${REPO_DIR}" rev-parse HEAD)"
    remote_rev="$(git -C "${REPO_DIR}" rev-parse "${upstream}")"

    if [ "${local_rev}" = "${remote_rev}" ]; then
        info "Already up to date."
        return
    fi

    if ! git -C "${REPO_DIR}" diff --quiet || ! git -C "${REPO_DIR}" diff --cached --quiet; then
        info "Local changes found; skipping automatic update."
        info "Commit/stash your changes, then run install.sh again."
        return
    fi

    if git -C "${REPO_DIR}" merge-base --is-ancestor HEAD "${upstream}"; then
        info "Updates found; fast-forwarding ${branch}..."
        git -C "${REPO_DIR}" merge --ff-only --quiet "${upstream}"
        info "Repository updated."
    else
        info "Local branch has commits not on ${upstream}; skipping automatic update."
    fi
}

install_plugin() {
    [ -f "${PLUGIN_SRC}" ] || die "Plugin source not found: ${PLUGIN_SRC}"
    [ -d "${KLIPPER_EXTRAS_DIR}" ] || die "Klipper extras directory not found: ${KLIPPER_EXTRAS_DIR}"

    if [ -L "${PLUGIN_DST}" ]; then
        local current_target
        current_target="$(readlink "${PLUGIN_DST}")"
        if [ "${current_target}" = "${PLUGIN_SRC}" ]; then
            info "Existing symlink is already correct."
            return
        fi
        info "Replacing existing symlink: ${PLUGIN_DST} -> ${current_target}"
        rm -f "${PLUGIN_DST}"
    elif [ -e "${PLUGIN_DST}" ]; then
        info "Removing existing file: ${PLUGIN_DST}"
        rm -f "${PLUGIN_DST}"
    fi

    ln -s "${PLUGIN_SRC}" "${PLUGIN_DST}"
    info "Installed symlink: ${PLUGIN_DST} -> ${PLUGIN_SRC}"
}

main() {
    info "Repository: ${REPO_DIR}"
    info "Klipper directory: ${KLIPPER_DIR}"
    update_repo
    install_plugin
    info "Done. Restart Klipper after changing configuration."
}

main "$@"
