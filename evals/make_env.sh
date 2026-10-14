#!/bin/sh
# Usage: make_env.sh <run_dir> [missing_worker...]
# Builds an isolated environment for one eval run: fake claude/pi/opencode
# shims first on PATH, its own relay state folder, and a small git repo.
set -e
RUN=$1; shift
FIXTURES=$(cd "$(dirname "$0")/../tests/fixtures" && pwd)
mkdir -p "$RUN/bin" "$RUN/logs" "$RUN/crew"
for w in claude pi opencode; do
  skip=0; for m in "$@"; do [ "$m" = "$w" ] && skip=1; done
  [ $skip = 1 ] && continue
  cat > "$RUN/bin/$w" <<SH
#!/bin/sh
export FAKE_CLAUDE_LOG="$RUN/logs/claude.jsonl" FAKE_WORKER_LOG="$RUN/logs/$w.jsonl"
exec "$(command -v python3)" "$FIXTURES/fake_$w.py" "\$@"
SH
  chmod +x "$RUN/bin/$w"
done
REPO="$RUN/repo"; mkdir -p "$REPO/src" "$REPO/tests"
cat > "$REPO/src/calc.py" <<'PY'
def average(values):
    return sum(values) / len(values)


def percent(part, whole):
    return part / whole * 100
PY
cat > "$REPO/tests/test_calc.py" <<'PY'
from src.calc import average


def test_average():
    assert average([1, 2, 3]) == 2
PY
printf '# Calc\n\nTiny maths helpers.\n' > "$REPO/README.md"
git -C "$REPO" init -q && git -C "$REPO" add -A && git -C "$REPO" -c user.name=Eval -c user.email=eval@example.com -c commit.gpgsign=false commit -qm init
cat > "$RUN/env.sh" <<SH
export PATH="$RUN/bin:\$PATH" CLAUDE_CREW_HOME="$RUN/crew"
unset CLAUDE_CREW_BIN CREW_PI_BIN CREW_OPENCODE_BIN
cd "$RUN/repo"
SH
echo "$RUN/env.sh"
