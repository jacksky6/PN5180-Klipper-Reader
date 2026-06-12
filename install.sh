#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_SRC="${REPO_DIR}/klippy/extras/pn5180.py"
CONFIG_SRC="${REPO_DIR}/config/pn5180.cfg"
DEFAULT_KLIPPER_HOME="${HOME}/klipper"
DEFAULT_KLIPPER_CONFIG_HOME="${HOME}/printer_data/config"
KLIPPER_HOME="${KLIPPER_HOME:-${DEFAULT_KLIPPER_HOME}}"
KLIPPER_CONFIG_HOME="${KLIPPER_CONFIG_HOME:-${DEFAULT_KLIPPER_CONFIG_HOME}}"

info() {
    printf '[PN5180] %s\n' "$*"
}

die() {
    printf '[PN5180] ERROR: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<EOF
Usage: ./install.sh [-k <klipper_home>] [-c <klipper_config_home>]

Options:
  -k <path>  Klipper source directory. Default: ${DEFAULT_KLIPPER_HOME}
  -c <path>  Klipper config directory. Default: ${DEFAULT_KLIPPER_CONFIG_HOME}
  -h         Show this help.

The installer links pn5180.py into Klipper extras and copies config/pn5180.cfg
only when the target config file does not already exist.
EOF
}

parse_args() {
    while getopts ':k:c:h' opt; do
        case "${opt}" in
            k)
                KLIPPER_HOME="${OPTARG}"
                ;;
            c)
                KLIPPER_CONFIG_HOME="${OPTARG}"
                ;;
            h)
                usage
                exit 0
                ;;
            :)
                die "Option -${OPTARG} requires an argument."
                ;;
            \?)
                die "Unknown option: -${OPTARG}"
                ;;
        esac
    done
    shift $((OPTIND - 1))
    if [ "$#" -ne 0 ]; then
        die "Unexpected argument: $1"
    fi
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
    local klipper_extras_dir plugin_dst current_target
    klipper_extras_dir="${KLIPPER_HOME}/klippy/extras"
    plugin_dst="${klipper_extras_dir}/pn5180.py"

    [ -f "${PLUGIN_SRC}" ] || die "Plugin source not found: ${PLUGIN_SRC}"
    [ -d "${klipper_extras_dir}" ] || die "Klipper extras directory not found: ${klipper_extras_dir}"

    if [ -L "${plugin_dst}" ]; then
        current_target="$(readlink "${plugin_dst}")"
        if [ "${current_target}" = "${PLUGIN_SRC}" ]; then
            info "Existing symlink is already correct."
            return
        fi
        info "Replacing existing symlink: ${plugin_dst} -> ${current_target}"
        rm -f "${plugin_dst}"
    elif [ -e "${plugin_dst}" ]; then
        info "Removing existing file: ${plugin_dst}"
        rm -f "${plugin_dst}"
    fi

    ln -s "${PLUGIN_SRC}" "${plugin_dst}"
    info "Installed symlink: ${plugin_dst} -> ${PLUGIN_SRC}"
}

install_config() {
    local config_dst
    config_dst="${KLIPPER_CONFIG_HOME}/pn5180.cfg"

    [ -f "${CONFIG_SRC}" ] || die "Example config not found: ${CONFIG_SRC}"

    if [ ! -d "${KLIPPER_CONFIG_HOME}" ]; then
        info "Config directory not found; skipping config install: ${KLIPPER_CONFIG_HOME}"
        info "Use -c <path> if your Klipper config directory is elsewhere."
        return
    fi

    if [ -e "${config_dst}" ]; then
        info "Config already exists; not overwriting: ${config_dst}"
        return
    fi

    cp "${CONFIG_SRC}" "${config_dst}"
    info "Installed config: ${config_dst}"
}

main() {
    parse_args "$@"
    info "Repository: ${REPO_DIR}"
    info "Klipper home: ${KLIPPER_HOME}"
    info "Klipper config: ${KLIPPER_CONFIG_HOME}"
    update_repo
    install_plugin
    install_config
    info "Done. Restart Klipper after changing configuration."
}

main "$@"
