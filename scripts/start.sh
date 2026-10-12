#!/bin/sh
set -eu

# Upstream interpolates the VNC password into a shell command.
# Restrict its input before starting any upstream process.
case "${VNC_PASSWORD:-}" in
    ????????) ;;
    *) printf '%s\n' 'ERROR: VNC_PASSWORD must be exactly 8 English letters or digits.' >&2; exit 64 ;;
esac
case "$VNC_PASSWORD" in
    *[!ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789]*)
        printf '%s\n' 'ERROR: VNC_PASSWORD must be exactly 8 English letters or digits.' >&2
        exit 64
        ;;
esac

exec "${APP_PATH:?Missing upstream APP_PATH}/mixins/scripts/run.sh"
