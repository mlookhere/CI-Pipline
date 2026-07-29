# Shared interpreter resolution for the workflow control plane.
#
# Resolution order: the CI runtime that scripts/bootstrap provisions, then an
# explicit PYTHON_BIN, then python3, then python. Each candidate is executed
# before being accepted -- on Windows `python3` resolves to a Microsoft Store
# stub that is present on PATH but exits 49 without running anything, so a
# `command -v` check alone silently selects a non-functional interpreter.

resolve_python() {
  local home="${CLAUDE_CI_HOME:-$HOME/.local/share/claude-code-ci/v2}"
  local candidate

  for candidate in "${CLAUDE_CI_PYTHON:-}" "$home/venv/bin/python" "$home/venv/Scripts/python.exe"; do
    if [[ -n "$candidate" && -x "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  for candidate in "${PYTHON_BIN:-}" python3 python; do
    [[ -z "$candidate" ]] && continue
    if command -v "$candidate" >/dev/null 2>&1 \
      && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  echo "No working Python 3.10+ interpreter found." >&2
  echo "Run ./scripts/bootstrap, or set PYTHON_BIN to a valid interpreter." >&2
  return 1
}
