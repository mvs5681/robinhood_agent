#!/usr/bin/env bash
# GEX Backtest Workflow — two runs: H1 2025 (last year) + H1 2026 (recent)
#
# Prerequisites:
#   1. pip install -e .  (from repo root)
#   2. Set POLYGON_API_KEY in .env
#
# Edit the configuration block below, then:
#   bash scripts/backtest_workflow.sh

set -euo pipefail

# ── Setup ─────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Load POLYGON_API_KEY and other secrets from .env — but only if not already
# set in the environment (so the config block below always wins).
if [[ -f "$REPO_ROOT/.env" ]]; then
    while IFS='=' read -r key val; do
        # Skip comments and blank lines
        [[ "$key" =~ ^#.*$ || -z "$key" ]] && continue
        # Only export if the variable is not already set in the environment
        [[ -z "${!key+x}" ]] && export "$key=$val"
    done < "$REPO_ROOT/.env"
fi

if [[ -z "${POLYGON_API_KEY:-}" ]]; then
    echo "ERROR: POLYGON_API_KEY is not set."
    echo "Add it to $REPO_ROOT/.env:  POLYGON_API_KEY=your_key_here"
    exit 1
fi

cd "$REPO_ROOT"

PYTHON="$REPO_ROOT/.venv/bin/python3.12"
if [[ ! -x "$PYTHON" ]]; then
    echo "ERROR: $PYTHON not found. Run: python3.12 -m venv .venv && .venv/bin/pip install -e ."
    exit 1
fi

# ── Configuration (edit here — always overrides .env) ────────────────────────

TICKERS="SPY"            # space-separated, e.g. "SPY QQQ AAPL"
CAPITAL="2000"           # starting portfolio capital in USD
FIXTURES_DIR="data/history"
RESULTS_DIR="results"

# Run 1 — H1 2025 (last year baseline)
RUN1_LABEL="2025_h1"
RUN1_START="2025-01-02"
RUN1_END="2025-06-30"

# Run 2 — H1 2026 (recent 6 months)
RUN2_LABEL="2026_h1"
RUN2_START="2026-01-20"
RUN2_END="2026-07-17"

# ─────────────────────────────────────────────────────────────────────────────

echo "╔══════════════════════════════════════════╗"
echo "║       GEX Backtest — Dual Run            ║"
echo "╚══════════════════════════════════════════╝"
echo "  Tickers : $TICKERS"
echo "  Capital : \$$CAPITAL per run"
echo "  Run 1   : $RUN1_START → $RUN1_END  ($RUN1_LABEL)"
echo "  Run 2   : $RUN2_START → $RUN2_END  ($RUN2_LABEL)"
echo "  Fixtures: $FIXTURES_DIR"
echo "  Results : $RESULTS_DIR/{run}/"
echo ""

# ── Helper ───────────────────────────────────────────────────────────────────

run_period() {
    local label="$1"
    local start="$2"
    local end="$3"

    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo " Step 1/$label — Fetching $start → $end"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  (resumable — already-fetched dates skipped)"
    echo ""

    # shellcheck disable=SC2086
    "$PYTHON" scripts/fetch_polygon_history.py \
        --start   "$start" \
        --end     "$end" \
        --tickers $TICKERS \
        --out     "$FIXTURES_DIR"

    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo " Step 2/$label — Running backtest"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""

    # shellcheck disable=SC2086
    "$PYTHON" scripts/run_backtest.py \
        --fixtures      "$FIXTURES_DIR" \
        --start         "$start" \
        --end           "$end" \
        --tickers       $TICKERS \
        --capital       "$CAPITAL" \
        --max-positions 3 \
        --csv-out       "$RESULTS_DIR/$label"

    echo ""
    echo "  ✓ $label complete — CSVs in $RESULTS_DIR/$label/"
    echo ""
}

# ── Run both periods ──────────────────────────────────────────────────────────

run_period "$RUN1_LABEL" "$RUN1_START" "$RUN1_END"
run_period "$RUN2_LABEL" "$RUN2_START" "$RUN2_END"

# ── Summary ───────────────────────────────────────────────────────────────────

echo "╔══════════════════════════════════════════╗"
echo "║  Both runs complete                      ║"
echo "╚══════════════════════════════════════════╝"
echo ""
echo "  CSV output:"
echo "    $RESULTS_DIR/$RUN1_LABEL/trades.csv   — per-trade records (H1 2025)"
echo "    $RESULTS_DIR/$RUN1_LABEL/equity.csv   — daily portfolio value (H1 2025)"
echo "    $RESULTS_DIR/$RUN1_LABEL/summary.csv  — key metrics (H1 2025)"
echo ""
echo "    $RESULTS_DIR/$RUN2_LABEL/trades.csv   — per-trade records (H1 2026)"
echo "    $RESULTS_DIR/$RUN2_LABEL/equity.csv   — daily portfolio value (H1 2026)"
echo "    $RESULTS_DIR/$RUN2_LABEL/summary.csv  — key metrics (H1 2026)"
echo ""

"$PYTHON" - <<'PYEOF'
import csv
from pathlib import Path

runs = [("H1 2025", "results/2025_h1"), ("H1 2026", "results/2026_h1")]
print("  ┌─────────────────────────────────────────────────────────────┐")
print("  │ Side-by-side comparison                                     │")
print("  ├──────────────────────────┬──────────────────┬──────────────┤")
print(f"  │ {'Metric':<26}│ {'H1 2025':>16} │ {'H1 2026':>12} │")
print("  ├──────────────────────────┼──────────────────┼──────────────┤")

metrics_to_show = [
    ("trade_count",          "Trades"),
    ("win_rate",             "Win rate"),
    ("avg_pnl_pct",          "Avg P&L %"),
    ("initial_capital",      "Capital ($)"),
    ("final_value",          "Final value ($)"),
    ("total_pnl_dollars",    "Total P&L ($)"),
    ("total_return_pct",     "Total return %"),
    ("max_drawdown_dollars", "Max drawdown ($)"),
]

data = {}
for label, path in runs:
    summary = Path(path) / "summary.csv"
    if not summary.exists():
        data[label] = {}
        continue
    with open(summary) as f:
        data[label] = {row[0]: row[1] for row in csv.reader(f) if len(row) == 2}

for key, display in metrics_to_show:
    v1 = data.get("H1 2025", {}).get(key, "n/a")
    v2 = data.get("H1 2026", {}).get(key, "n/a")
    if key in ("win_rate", "avg_pnl_pct", "total_return_pct"):
        try: v1 = f"{float(v1):+.1%}"
        except: pass
        try: v2 = f"{float(v2):+.1%}"
        except: pass
    elif key in ("initial_capital", "final_value", "total_pnl_dollars", "max_drawdown_dollars"):
        try: v1 = f"${float(v1):,.2f}"
        except: pass
        try: v2 = f"${float(v2):,.2f}"
        except: pass
    print(f"  │ {display:<26}│ {str(v1):>16} │ {str(v2):>12} │")

print("  └──────────────────────────┴──────────────────┴──────────────┘")
PYEOF
