#!/usr/bin/env bash
# Install the agent-trace adapters onto this machine.
#
#   ./install.sh hermes            # copy the plugin into a profile + enable it
#   ./install.sh hermes ai         # ...into the "ai" profile
#   ./install.sh hermes all        # ...into every profile that has one
#   ./install.sh codex             # wire the codex hook
#   ./install.sh pi                # register the pi extension (pi install)
#   ./install.sh status         # what is installed, and what is STALE
#   ./install.sh all
#
# The hermes adapter is installed as a *copy*, so it must be re-run after every
# change to hermes/__init__.py or common/agenttrace_common.py. Editing the repo
# alone has no effect on a running agent: a stale copy is exactly how a
# duration-unit fix ended up "fixed" in git while the live traces kept writing
# floats.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

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
  # agenttrace_common.py ships alongside: the plugin cannot reach back into
  # the repo once copied, and hermes/__init__.py probes for it next to itself
  # first, then at ../common.
  cp hermes/__init__.py hermes/plugin.yaml "$home/plugins/agent-trace/"
  cp common/agenttrace_common.py "$home/plugins/agent-trace/"

  # Verify the deployed bytes, not just the exit code of cp. (A stale
  # __pycache__ is not a problem: CPython invalidates .pyc by source
  # mtime+size, so the freshly copied source is always recompiled.)
  local src
  for src in hermes/__init__.py common/agenttrace_common.py; do
    if ! cmp -s "$src" "$home/plugins/agent-trace/$(basename "$src")"; then
      echo "FAIL: deployed copy differs from $src" >&2
      return 1
    fi
  done
  echo "synced hermes plugin -> $home/plugins/agent-trace"

  if command -v hermes >/dev/null 2>&1; then
    local flag=()
    [ "$name" != "default" ] && flag=(--profile "$name")
    # </dev/null + timeout: an installer must never block waiting for input.
    # `hermes plugins enable` has been observed to prompt when the plugin was
    # already enabled, which hung the e2e suite on a prompt nobody was there
    # to answer.
    timeout 30 hermes "${flag[@]}" plugins enable agent-trace </dev/null >/dev/null 2>&1 \
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

# Report what is installed and, for the copy-installed pieces, whether the
# deployed bytes still match this repo.
#
# Hermes is the reason this exists: it is installed as a *copy*, so editing the
# repo alone has no effect on the running agent — a duration-unit fix sat
# "fixed" in git while live traces kept writing floats, because nobody could
# see that ~/.hermes/plugins/agent-trace/ had drifted. Comparing the bytes is
# the only way to catch that from outside the agent.
status() {
  local stale=0
  echo "agent-trace $(cat VERSION 2>/dev/null || echo '?')"
  echo

  local home name dest label src
  local homes=()
  homes+=("$HOME/.hermes")
  local d
  for d in "$HOME"/.hermes/profiles/*/; do
    [ -d "$d" ] && homes+=("${d%/}")
  done

  for home in "${homes[@]}"; do
    if [ "$home" = "$HOME/.hermes" ]; then
      name="default"
    else
      name="profile=$(basename "$home")"
    fi
    dest="$home/plugins/agent-trace"
    if [ ! -d "$dest" ]; then
      printf '  %-26s %-14s\n' "hermes $name" "not installed"
      continue
    fi
    local missing=() staleparts=() note
    for src in hermes/__init__.py hermes/plugin.yaml common/agenttrace_common.py; do
      if [ ! -f "$dest/$(basename "$src")" ]; then
        missing+=("$(basename "$src")")
      elif ! cmp -s "$src" "$dest/$(basename "$src")"; then
        staleparts+=("$(basename "$src")")
      fi
    done
    note=""
    [ ${#missing[@]} -gt 0 ] && note="missing ${missing[*]}"
    [ ${#staleparts[@]} -gt 0 ] && note="${note:+$note; }stale ${staleparts[*]}"
    if [ -n "$note" ]; then
      # Both are reported, not just the first kind: a half-synced deploy
      # (new __init__.py, forgotten agenttrace_common.py) fails at import
      # inside a running agent, and "stale" alone would hide that.
      printf '  %-26s %-14s %s\n' "hermes $name" "SYNC NEEDED" "$note"
      stale=1
    else
      printf '  %-26s %-14s %s\n' "hermes $name" "ok" "$dest"
    fi
  done

  # The CLI is a symlink, so it cannot go stale — only disappear or point at
  # a checkout that has moved.
  local link="$HOME/.local/bin/agenttrace"
  if [ -L "$link" ]; then
    if [ "$(readlink -f "$link")" = "$(readlink -f "$PWD/cli/agenttrace.py")" ]; then
      printf '  %-26s %-14s %s\n' "cli" "ok" "$link"
    else
      printf '  %-26s %-14s points at %s\n' "cli" "STALE" "$(readlink -f "$link")"
      stale=1
    fi
  elif [ -x "$link" ]; then
    printf '  %-26s %-14s %s (copied, not linked)\n' "cli" "ok" "$link"
  else
    printf '  %-26s %-14s\n' "cli" "not installed"
  fi

  if [ -f "$HOME/.codex/hooks.json" ] && grep -q codex_import.py "$HOME/.codex/hooks.json" 2>/dev/null; then
    printf '  %-26s %-14s %s\n' "codex" "ok" "$HOME/.codex/hooks.json"
  else
    printf '  %-26s %-14s\n' "codex" "not installed"
  fi

  if command -v pi >/dev/null 2>&1 && pi list 2>/dev/null | grep -qi agent-trace; then
    printf '  %-26s %-14s registered by pi install\n' "pi" "ok"
  else
    printf '  %-26s %-14s\n' "pi" "not installed"
  fi

  echo
  echo "  traces: ~/.hermes/traces  ~/.codex/traces  ~/.pi/agent/traces"
  if [ "$stale" -ne 0 ]; then
    echo
    echo "  STALE means the repo changed but the deployed copy did not — rerun"
    echo "  install.sh so the running agent picks it up." >&2
    return 1
  fi
  return 0
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
  status) status ;;
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
    echo "usage: $0 {hermes|codex|pi|cli|status|all} [profile|all]" >&2
    exit 2
    ;;
esac
