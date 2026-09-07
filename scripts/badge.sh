#!/usr/bin/env bash
# Generate a shields.io-style coverage badge SVG.
# Usage: bash scripts/badge.sh [percent] > badges/coverage.svg
#
# Reads coverage/lcov.info if no percent given on command line.
# Skips test files (matches *.test.*, *.spec.*, tests/ paths).

set -euo pipefail

if [[ $# -ge 1 ]]; then
  pct="$1"
else
  total_lf=0
  total_lh=0
  while IFS= read -r line; do
    case "$line" in
      SF:*)
        file="${line#SF:}"
        [[ "$file" =~ (^tests/|\.(test|spec)\.) ]] && skip=1 || skip=0
        ;;
      LF:*) [[ $skip -eq 0 ]] && lf="${line#LF:}" || lf=0 ;;
      LH:*)
        [[ $skip -eq 0 ]] && lh="${line#LH:}" || lh=0
        total_lf=$((total_lf + lf))
        total_lh=$((total_lh + lh))
        ;;
    esac
  done < coverage/lcov.info
  if [[ "$total_lf" -gt 0 ]]; then
    pct=$((total_lh * 100 / total_lf))
  else
    pct=0
  fi
fi

# Pick color: green ≥80, yellow ≥60, red below
if   [[ "$pct" -ge 80 ]]; then color="#4c1"
elif [[ "$pct" -ge 60 ]]; then color="#dfb317"
else color="#e05d44"
fi

# Shields.io dimensions: label ~70px + value ~48px = ~118px total
# Adjust value width for 2 vs 3 digit percentages
if [[ "$pct" -ge 100 ]]; then vw=54; else vw=48; fi
lw=70
tw=$((lw + vw))

cat <<SVG
<svg xmlns="http://www.w3.org/2000/svg" width="$tw" height="20" role="img">
  <title>coverage: ${pct}%</title>
  <linearGradient id="s" x2="0" y2="100%">
    <stop offset="0" stop-color="#bbb" stop-opacity=".1"/>
    <stop offset="1" stop-opacity=".1"/>
  </linearGradient>
  <clipPath id="r"><rect width="$tw" height="20" rx="3" fill="#fff"/></clipPath>
  <g clip-path="url(#r)">
    <rect width="$lw" height="20" fill="#555"/>
    <rect x="$lw" width="$vw" height="20" fill="$color"/>
    <rect width="$tw" height="20" fill="url(#s)"/>
  </g>
  <g fill="#fff" text-anchor="middle" font-family="Verdana,Geneva,DejaVu Sans,sans-serif" font-size="11">
    <text x="$((lw / 2))" y="14">coverage</text>
    <text x="$((lw + vw / 2))" y="14">${pct}%</text>
  </g>
</svg>
SVG
