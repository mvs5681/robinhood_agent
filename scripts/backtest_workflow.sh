#!/usr/bin/env bash
# GEX Backtest Workflow — fetch historical data then run backtest
#
# Prerequisites:
#   1. pip install -e . (or pip install -r requirements, from the repo root)
#   2. Set POLYGON_API_KEY in .env (free tier is fine — uses --free-tier flag)
#
# Edit the variables below, then:
#   chmod +x scripts/backtest_workflow.sh
#   ./scripts/backtest_workflow.sh

set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────────────

TICKERS="SPY"            # space-separated, e.g. "SPY QQQ AAPL"
START_DATE="2026-01-20"  # first trading day to backtest (~6 months ago)
END_DATE="2026-07-17"    # last trading day (most recent close)
OUT_DIR="data/history"   # where fixture files are written
CAPITAL="2000"           # starting portfolio capital in USD

# ── Derived ──────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Load .env if present
if [[ -f "$REPO_ROOT/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$REPO_ROOT/.env"
    set +a
fi

if [[ -z "${POLYGON_API_KEY:-}" ]]; then
    echo "ERROR: POLYGON_API_KEY is not set."
    echo "Add it to $REPO_ROOT/.env:  POLYGON_API_KEY=your_key_here"
    exit 1
fi

cd "$REPO_ROOT"

echo "========================================"
echo " GEX Backtest Workflow"
echo "========================================"
echo "  Tickers   : $TICKERS"
echo "  Date range: $START_DATE → $END_DATE"
echo "  Capital   : \$$CAPITAL"
echo "  Fixtures  : $OUT_DIR"
echo "  Plan      : Polygon Starter (real per-contract OHLCV pricing)"
echo "========================================"
echo ""

# ── Step 1: Fetch historical fixtures ────────────────────────────────────────

echo "Step 1/2 — Fetching historical data from Polygon..."
echo "  (resumable: already-fetched dates are skipped automatically)"
echo ""

# shellcheck disable=SC2086
python scripts/fetch_polygon_history.py \
    --start "$START_DATE" \
    --end   "$END_DATE" \
    --tickers $TICKERS \
    --out   "$OUT_DIR"

echo ""
echo "Step 1/2 complete."
echo ""

# ── Step 2: Run backtest ──────────────────────────────────────────────────────

echo "Step 2/2 — Running backtest..."
echo ""

# shellcheck disable=SC2086
python scripts/run_backtest.py \
    --fixtures      "$OUT_DIR" \
    --start         "$START_DATE" \
    --end           "$END_DATE" \
    --tickers       $TICKERS \
    --capital       "$CAPITAL" \
    --max-positions 3

echo ""
echo "Done. Edit TICKERS / START_DATE / END_DATE at the top of this script to re-run."
