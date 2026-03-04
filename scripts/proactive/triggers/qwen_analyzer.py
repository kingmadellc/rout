"""Local Qwen probability estimation for prediction markets.

Takes a Kalshi market + real-world context (Polygon data, X signals)
and outputs an independent probability estimate. This is the core of
the edge calculation: edge = |market_price - qwen_estimate|.

Runs entirely local via Ollama — zero API cost, unlimited queries.
"""

from __future__ import annotations

import json
import subprocess
import time
from typing import Optional

from proactive.base import _log


# ── Qwen Interface ──────────────────────────────────────────────────────────

def _run_qwen(prompt: str, timeout: int = 60) -> dict | None:
    """Run a prompt through local Qwen via Ollama, return parsed JSON."""
    try:
        result = subprocess.run(
            ["ollama", "run", "qwen3:latest", "--format", "json", prompt],
            capture_output=True, timeout=timeout, text=True,
        )
        if result.returncode != 0:
            _log(f"Qwen error (rc={result.returncode}): {result.stderr[:200]}")
            return None

        # Parse JSON from output (Qwen sometimes includes preamble)
        output = result.stdout.strip()
        # Try direct parse first
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            # Try to find JSON in output
            start = output.find("{")
            end = output.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(output[start:end])
        return None

    except subprocess.TimeoutExpired:
        _log("Qwen timeout — model may be loading or overloaded")
        return None
    except FileNotFoundError:
        _log("Qwen error: ollama not found — install with 'brew install ollama'")
        return None
    except Exception as e:
        _log(f"Qwen error: {e}")
        return None


# ── Probability Estimation ──────────────────────────────────────────────────

def estimate_probability(
    market_title: str,
    market_price_cents: int,
    days_to_close: float | None,
    news_context: list[dict] | None = None,
    economic_context: dict | None = None,
    x_signal: dict | None = None,
) -> dict | None:
    """Estimate true probability for a prediction market.

    Core estimation function. Qwen receives:
    - Market title + current price
    - Real-world news (Polygon)
    - Economic indicators (Polygon)
    - X/Twitter signals (existing scanner)

    Returns:
        {
            "estimated_probability": 0.0-1.0,
            "confidence": 0.0-1.0,
            "reasoning": "one line",
            "direction": "overpriced" | "underpriced" | "fair",
            "edge_pct": float,  # absolute difference in percentage points
        }
    """
    # Build context blocks
    context_parts = []

    if news_context:
        news_block = "\n".join(
            f"  - [{n.get('source', '?')}] {n.get('title', '')}"
            for n in news_context[:5]
        )
        context_parts.append(f"RECENT NEWS:\n{news_block}")

    if economic_context:
        econ_lines = []
        if "sp500" in economic_context:
            sp = economic_context["sp500"]
            econ_lines.append(f"  S&P 500: ${sp.get('close', '?')} ({sp.get('change_pct', 0):+.1f}%)")
        if "btc" in economic_context:
            btc = economic_context["btc"]
            econ_lines.append(f"  Bitcoin: ${btc.get('price', '?'):,.0f} ({btc.get('change_pct', 0):+.1f}%)")
        if "vix_proxy" in economic_context:
            vix = economic_context["vix_proxy"]
            econ_lines.append(f"  VIX proxy (VIXY): ${vix.get('close', '?')} ({vix.get('change_pct', 0):+.1f}%)")
        if "gold_proxy" in economic_context:
            gold = economic_context["gold_proxy"]
            econ_lines.append(f"  Gold (GLD): ${gold.get('close', '?')} ({gold.get('change_pct', 0):+.1f}%)")
        if econ_lines:
            context_parts.append(f"ECONOMIC INDICATORS:\n" + "\n".join(econ_lines))

    if x_signal:
        sig_line = f"  X/Twitter signal: {x_signal.get('direction', '?')} on '{x_signal.get('topic', '?')}' — {x_signal.get('summary', '')}"
        context_parts.append(f"SOCIAL SIGNAL:\n{sig_line}")

    context_block = "\n\n".join(context_parts) if context_parts else "(No additional context available)"

    days_str = f"{days_to_close:.0f} days" if days_to_close is not None else "unknown timeframe"
    market_implied = market_price_cents / 100.0

    prompt = f"""You are an expert prediction market analyst. Your job is to estimate the TRUE probability of an event, independent of what the market says.

MARKET: {market_title}
CURRENT MARKET PRICE: {market_price_cents}¢ YES (implies {market_implied:.0%} probability)
TIME TO RESOLUTION: {days_str}

{context_block}

INSTRUCTIONS:
1. Based ONLY on the evidence above and your knowledge, estimate the true probability this event resolves YES.
2. Do NOT anchor on the market price. The market may be wrong.
3. Consider: base rates, recent developments, timing, political dynamics, economic conditions.
4. If you lack information to form a strong opinion, say so with low confidence.
5. Be calibrated: 70% means it happens 7 out of 10 times.

Respond in JSON:
{{
  "estimated_probability": <float 0.0-1.0>,
  "confidence": <float 0.0-1.0, how confident you are in your estimate>,
  "reasoning": "<one sentence explaining your estimate>",
  "key_factors": ["<factor 1>", "<factor 2>", "<factor 3>"]
}}"""

    result = _run_qwen(prompt, timeout=60)
    if not result:
        return None

    try:
        est_prob = float(result.get("estimated_probability", 0.5))
        confidence = float(result.get("confidence", 0.3))
        reasoning = result.get("reasoning", "")
        key_factors = result.get("key_factors", [])

        # Clamp values
        est_prob = max(0.01, min(0.99, est_prob))
        confidence = max(0.0, min(1.0, confidence))

        # Calculate edge
        edge_pct = abs(est_prob - market_implied) * 100
        if est_prob > market_implied:
            direction = "underpriced"  # market is too low, YES is a buy
        elif est_prob < market_implied:
            direction = "overpriced"   # market is too high, NO is a buy
        else:
            direction = "fair"

        return {
            "estimated_probability": round(est_prob, 4),
            "market_implied": round(market_implied, 4),
            "confidence": round(confidence, 4),
            "reasoning": reasoning[:200],
            "key_factors": key_factors[:3],
            "direction": direction,
            "edge_pct": round(edge_pct, 1),
        }

    except (ValueError, TypeError, KeyError) as e:
        _log(f"Qwen parse error: {e}")
        return None


# ── Batch Estimation ────────────────────────────────────────────────────────

def estimate_batch(
    markets: list[dict],
    economic_context: dict | None = None,
    max_markets: int = 15,
) -> list[dict]:
    """Run probability estimation on a batch of markets.

    Each market dict should have: title, yes_price (cents), days_to_close, news (optional).
    Returns list of estimation results with market metadata attached.
    """
    results = []

    for m in markets[:max_markets]:
        title = m.get("title", "?")
        price = m.get("yes_price", 50)
        days = m.get("days_to_close")
        news = m.get("news", [])
        x_sig = m.get("x_signal")

        _log(f"  Qwen analyzing: {title[:50]}...")
        est = estimate_probability(
            market_title=title,
            market_price_cents=price,
            days_to_close=days,
            news_context=news if news else None,
            economic_context=economic_context,
            x_signal=x_sig,
        )

        if est and est["confidence"] > 0.2:  # Drop very low confidence
            results.append({
                **m,  # Pass through market metadata
                **est,  # Add estimation fields
            })

        # Small delay to avoid hammering Ollama
        time.sleep(0.5)

    return results
