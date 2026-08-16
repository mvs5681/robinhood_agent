# Project instructions

## Keep the architecture doc in sync

`docs/ARCHITECTURE.md` documents how the agent actually works — every
pipeline node, gate, threshold, exit rule, capture mechanism, and the
backtest harness — with Mermaid diagrams for each. It is the reference for
understanding the system without re-reading every module.

**This is a hard rule, not a suggestion**: whenever a change alters the
*behavior* the doc describes, update the relevant section of
`docs/ARCHITECTURE.md` in the **same commit/PR** as the code change. Do not
defer it to a follow-up. Treat a stale diagram as a bug, not a nice-to-have.

This applies to changes such as:
- Adding, removing, or reordering a node in the LangGraph pipeline
  (`src/trader/graph/agent.py::build_graph`)
- Changing a gate's logic or a threshold/default (kill-switch, position/
  premium/sector caps, flow lookback window, DTE/delta window, exit
  priority order or its percentages, GEX regime thresholds)
- Adding, removing, or changing a live loop's polling cadence, or adding a
  new loop to `run_live.py`
- Changing what data a capture mechanism writes, where, or when
  (`CaptureLoop`, `StateCaptureLoop`, `FlowAlertCapture`)
- Changing `BacktestHarness`/`BacktestLoop` semantics (`run()` vs.
  `step_forward()`, what `bypass_flow_gate` does, window/coverage defaults)
- Adding or changing a dashboard tab or its data source
- Changing which `execution_status` / `ExitReason` values exist or what
  triggers them

Before marking such a change done, re-read the relevant section(s) of
`docs/ARCHITECTURE.md` and confirm every number, node name, and diagram
edge still matches the code you just wrote. If a diagram would need
restructuring, restructure it — don't leave a diagram that quietly
describes the old behavior next to text describing the new one.

If you're unsure whether a change is "architecturally significant" enough
to warrant an update, err toward updating — a redundant-but-accurate note
costs nothing; a stale diagram costs someone real debugging time later.
