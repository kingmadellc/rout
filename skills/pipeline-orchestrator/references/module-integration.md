# Module Integration Guide

## How to Register New Modules

When adding a new analysis module to the pipeline, it must be registered with the orchestrator.
Every module needs:

1. **Unique ID** — snake_case, descriptive (e.g., `crypto_scanner`, `earnings_calendar`)
2. **Type classification:**
   - `scanner` — Actively searches for opportunities. MUST complete before any negative verdict.
   - `deep_analysis` — Runs model comparisons or complex analysis. MUST complete before negative verdict.
   - `context` — Provides supporting data (calendar, portfolio). Can report independently.
   - `enrichment` — Adds detail to existing results. Does NOT block verdicts.
3. **Expected latency range** — Used for timeout calculation and user messaging
4. **Data sources** — What external APIs/feeds this module consumes
5. **Output schema** — What fields the module returns

## Module Communication Contract

Every module must return a standardized response:

```json
{
  "module_id": "edge_engine",
  "status": "complete",
  "timestamp": "2026-03-02T14:30:00Z",
  "latency_ms": 28400,
  "results": [
    {
      "opportunity_id": "gas-price-above-3130",
      "domain": "market_edge",
      "edge_pct": 12,
      "confidence_pct": 70,
      "market_price_pct": 53,
      "model_price_pct": 65,
      "direction": "YES",
      "expiry_days": 37,
      "volume": 1362,
      "spread_pct": null,
      "summary": "Will average gas prices be above $3.130?"
    }
  ],
  "metadata": {
    "model_version": "qwen-v1.8",
    "markets_scanned": 847,
    "opportunities_evaluated": 23,
    "passed_threshold": 2
  }
}
```

## Priority Scoring Formula

Priority = (edge_pct * 2) + (confidence_pct * 0.5) + volume_bonus - spread_penalty

Where:
- volume_bonus = log10(volume) * 5
- spread_penalty = spread_pct * 0.3 (if spread > 20%)

Thresholds:
- HIGH priority: score >= 40
- MEDIUM priority: score >= 20
- LOW priority: score < 20

## Verdict Dependency Matrix

Which modules must report before which verdicts can be issued:

| Verdict Type | Required Modules | Optional Modules |
|---|---|---|
| "No macro opportunities" | broad_market_scan, edge_engine | calendar |
| "No sports opportunities" | sports_scan | — |
| "Nothing available anywhere" | ALL scanner + deep_analysis types | ALL context types |
| "Best opportunity is X" | ALL scanner + deep_analysis types | portfolio_tracker |
| "Current portfolio outperforms available" | ALL scanners, portfolio_tracker | calendar |
