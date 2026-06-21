#!/usr/bin/env bash
# set-title.sh — set the terminal window/tab title.
#
# Usage:
#   ./set-title.sh                 # title = "WulfKolbe/htmldrill" (the GitHub name)
#   ./set-title.sh "my title"      # title = whatever you pass
#   source set-title.sh            # same, but also defines `title` for repeated use
#
# How it works: emits the OSC escape  ESC ] 0 ; <text> BEL  which terminals
# interpret as "set the icon name AND window title". It must reach the real TTY,
# so run this at your own shell prompt (not captured by another tool).
#
# Note: some host programs (e.g. an interactive CLI that manages the title) may
# overwrite it on their next redraw. To make it durable, add the same `printf`
# line to your ~/.bashrc, or set the tab title in your terminal emulator's
# profile, which most hosts won't override.

DEFAULT_TITLE="WulfKolbe/htmldrill"

title() {
    # Write the OSC sequence to the controlling terminal if we can, else stdout.
    local t="${1:-$DEFAULT_TITLE}"
    # `-w /dev/tty` is NOT enough: the device node can exist and look writable
    # yet fail to OPEN when there is no controlling terminal. Probe by actually
    # opening it; fall back to stdout (which, at a real prompt, is the TTY too).
    if { : > /dev/tty; } 2>/dev/null; then
        printf '\033]0;%s\007' "$t" > /dev/tty
    else
        printf '\033]0;%s\007' "$t"
    fi
}

# When executed (not sourced), set the title immediately from $1 (or the default).
# `return` works only in a sourced context; the `|| true` keeps execution clean.
( return 0 2>/dev/null ) || title "$1"
