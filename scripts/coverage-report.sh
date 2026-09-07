#!/usr/bin/env bash
# Parse lcov.info and output a markdown coverage summary table.
# Usage: ./scripts/coverage-report.sh [lcov-file] > coverage.md

set -euo pipefail
LCOV="${1:-coverage/lcov.info}"

if [[ ! -f "$LCOV" ]]; then
  echo "❌ No coverage data found at $LCOV"
  exit 1
fi

echo "## 📊 Coverage Report"
echo

# Per-file table
echo "| File | Lines | Coverage |"
echo "|------|------|----------|"

total_lf=0
total_lh=0

while IFS= read -r line; do
  case "$line" in
    SF:*) file="${line#SF:}" ;;
    LF:*) lf="${line#LF:}" ;;
    LH:*)
      lh="${line#LH:}"
      if [[ "$lf" -gt 0 ]]; then
        pct=$((lh * 100 / lf))
      else
        pct=0
      fi
      total_lf=$((total_lf + lf))
      total_lh=$((total_lh + lh))
      printf "| \`%s\` | %d/%d | %d%% |\n" "$file" "$lh" "$lf" "$pct"
      ;;
  esac
done < "$LCOV"

echo

# Overall
if [[ "$total_lf" -gt 0 ]]; then
  overall=$((total_lh * 100 / total_lf))
else
  overall=0
fi

echo "**Overall: ${overall}%** (${total_lh}/${total_lf} lines)"
