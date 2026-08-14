# Shared interpreter resolution for the workflow control plane.
#
# Resolution order: the CI runtime that scripts/bootstrap provisions, then an
# explicit PYTHON_BIN, then python3, then python. Each candidate is executed
# before being accepted -- on Windows `python3` resolves to a Microsoft Store
# stub that is present on PATH but exits 49 without running anything, so a
# `command -v` check alone silently selects a non-functional interpreter.

# True when the candidate exists and runs a supported Python. Works for a bare
# name on PATH and for an absolute path: `command -v` accepts both.
python_runs() {
  local candidate="$1"

  [[ -n "$candidate" ]] || return 1
  command -v "$candidate" >/dev/null 2>&1 || return 1
  "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}

# A working interpreter that is not the CI runtime. scripts/bootstrap needs this
# rather than resolve_python: it creates the CI runtime, and rebuilds it by
# deleting it first, so it cannot depend on the interpreter living inside it.
resolve_system_python() {
  local candidate

  for candidate in "${PYTHON_BIN:-}" python3 python; do
    if python_runs "$candidate"; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  echo "No working Python 3.10+ interpreter found." >&2
  echo "Install Python 3.10+ or set PYTHON_BIN to one. On Windows a bare 'python3'" >&2
  echo "is often the Microsoft Store stub, which is on PATH but runs nothing." >&2
  return 1
}

resolve_python() {
  local home="${CLAUDE_CI_HOME:-$HOME/.local/share/claude-code-ci/v2}"
  local candidate

  # CLAUDE_CI_PYTHON is probed like every other candidate. An interpreter that
  # exists and is executable but does not run is exactly the failure this file
  # exists to prevent, and the value can come from the environment.
  for candidate in "${CLAUDE_CI_PYTHON:-}" "$home/venv/bin/python" "$home/venv/Scripts/python.exe"; do
    if python_runs "$candidate"; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  resolve_system_python
}
