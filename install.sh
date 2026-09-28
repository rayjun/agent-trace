#!/usr/bin/env bash
# Install the agent-trace adapters onto this machine.
#
#   ./install.sh hermes            # copy the plugin into a profile + enable it
#   ./install.sh hermes ai         # ...into the "ai" profile
#   ./install.sh hermes all        # ...into every profile that has one
#   ./install.sh codex             # wire the codex hook
#   ./install.sh pi                # register the pi extension (pi install)
#   ./install.sh all
#
# The hermes adapter is installed as a *copy*, so it must be re-run after every
# change to hermes/__init__.py. Editing the repo alone has no effect on a
# running agent: a stale copy is exactly how a duration-unit fix ended up
# "fixed" in git while the live traces kept writing floats.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

TARGET_PROFILES=()
install_hermes() {
  local name="${1:-default}"
  local home
  if [ "$name" = "default" ]; then
    home="$HOME/.hermes"
  else
    home="$HOME/.hermes/profiles/$name"
  fi

  if [ ! -d "$home" ]; then
    echo "! profile home not found: $home" >&2
    return 1
  fi

  mkdir -p "$home/plugins/agent-trace"
  cp hermes/__init__.py hermes/plugin.yaml "$home/plugins/agent-trace/"

  # Verify the deployed bytes, not just the exit code of cp. (A stale
  # __pycache__ is not a problem: CPython invalidates .pyc by source
  # mtime+size, so the freshly copied source is always recompiled.)
  if ! cmp -s hermes/__init__.py "$home/plugins/agent-trace/__init__.py"; then
    echo "FAIL: deployed copy differs from hermes/__init__.py" >&2
    return 1
  fi
  echo "synced hermes plugin -> $home/plugins/agent-trace"

  if command -v hermes >/dev/null 2>&1; then
    local flag=()
    [ "$name" != "default" ] && flag=(--profile "$name")
    hermes "${flag[@]}" plugins enable agent-trace >/dev/null 2>&1 \
      && echo "  enabled (profile=$name)" \
      || echo "  ! enable failed; run: hermes ${flag[*]:-} plugins enable agent-trace"
  fi
}

install_cli() {
  # Symlink rather than copy: the CLI is the thing being iterated on, and a
  # copied binary would silently keep running the old code. `agenttrace` is
  # documented throughout the README but is not part of the hermes/codex/pi
  # agent installs, so it needs its own explicit step.
  mkdir -p "$HOME/.local/bin"
  ln -sf "$PWD/cli/agenttrace.py" "$HOME/.local/bin/agenttrace"
  chmod +x cli/agenttrace.py
  echo "linked $HOME/.local/bin/agenttrace -> $PWD/cli/agenttrace.py"
  if command -v agenttrace >/dev/null 2>&1; then
    echo "  works: $(agenttrace --help 2>&1 | head -1)"
  else
    echo "  ! ~/.local/bin is not on PATH"
  fi
}

install_codex() {
  python3 codex/install_hook.py
}

install_pi() {
  # Pi ships its own installer: `pi install <dir>` registers the directory
  # in settings.json packages (a live path reference into the repo, not a
  # copy — edits take effect on the next pi run). `pi list` verifies the
  # registration; a re-run just rewrites the same reference.
  if ! command -v pi >/dev/null 2>&1; then
    echo "! pi not found on PATH — skipping the pi extension" >&2
    return 1
  fi
  if ! pi install "$PWD/pi" </dev/null; then
    echo "FAIL: pi install $PWD/pi failed" >&2
    return 1
  fi
  if ! pi list 2>/dev/null | grep -qi "agent-trace"; then
    echo "FAIL: pi did not register agent-trace (pi list shows no match)" >&2
    return 1
  fi
  echo "installed pi extension: $(pi list 2>/dev/null | grep -i agent-trace | head -1)"
}

case "${1:-all}" in
  hermes)
    name="${2:-default}"
    if [ "$name" = "all" ]; then
      for d in "$HOME"/.hermes/profiles/*/; do
        [ -d "$d" ] || continue
        install_hermes "$(basename "$d")"
      done
      install_hermes default
    else
      install_hermes "$name"
    fi
    ;;
  codex) install_codex ;;
  pi)    install_pi ;;
  cli)   install_cli ;;
  all)
    name="${2:-default}"
    if [ "$name" = "all" ]; then
      for d in "$HOME"/.hermes/profiles/*/; do
        [ -d "$d" ] || continue
        install_hermes "$(basename "$d")"
      done
      install_hermes default
    else
      install_hermes "$name"
    fi
    install_cli
    install_codex
    install_pi
    ;;
  *)
    echo "usage: $0 {hermes|codex|pi|cli|all} [profile|all]" >&2
    exit 2
    ;;
esac
