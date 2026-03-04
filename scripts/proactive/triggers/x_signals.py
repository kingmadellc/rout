"""X/Twitter signal scanner trigger."""

import json
import subprocess
import time
from proactive.base import (
    PROACTIVE_CFG,
    OPENCLAW_DIR,
    _log,
    _send_message,
    _record_send,
)

# Personality-aware send
try:
    from proactive.personality.engine import personality_send as _personality_send
    _HAS_PERSONALITY = True
except ImportError:
    _HAS_PERSONALITY = False


def _search_x_posts(topic: str) -> list:
    """Search for recent X/Twitter posts via ddgs (Brave backend) or fallback."""
    # Strategy 1: ddgs package (uses Brave Search, handles anti-bot)
    try:
        from ddgs import DDGS
        d = DDGS()
        results = list(d.text(f"site:x.com {topic}", max_results=5))
        return [r.get("body", "") for r in results if r.get("body")]
    except ImportError:
        pass
    except Exception:
        pass

    # Strategy 2: deprecated duckduckgo_search (v8.x)
    try:
        from duckduckgo_search import DDGS as DDGS_OLD
        with DDGS_OLD() as ddgs:
            results = list(ddgs.text(f"site:x.com {topic}", max_results=5))
        return [r.get("body", "") for r in results if r.get("body")]
    except ImportError:
        pass
    except Exception:
        pass

    return []


def _analyze_signals_local(topic: str, posts: list) -> dict:
    """Stage 1: Analyze posts for tradeable signals using local Qwen via Ollama."""
    if not posts:
        return {"confidence": 0, "signal": None}

    try:
        combined = "\n".join(f"- {p}" for p in posts[:3])
        prompt = (
            f"You are a prediction market analyst. Given these recent X/Twitter posts about '{topic}', "
            f"determine if there's a tradeable signal.\n\n{combined}\n\n"
            f"Respond in JSON: {{\"has_signal\": true/false, \"confidence\": 0.0-1.0, "
            f"\"direction\": \"bullish/bearish/neutral\", \"summary\": \"one line\"}}"
        )

        result = subprocess.run(
            ["ollama", "run", "qwen3:latest", "--format", "json", prompt],
            capture_output=True, timeout=30, text=True
        )

        if result.returncode != 0:
            return {"confidence": 0, "signal": None}

        parsed = json.loads(result.stdout.strip())
        return {
            "confidence": float(parsed.get("confidence", 0)),
            "has_signal": parsed.get("has_signal", False),
            "direction": parsed.get("direction", "neutral"),
            "summary": parsed.get("summary", ""),
        }
    except Exception:
        return {"confidence": 0, "signal": None}


def _load_signal_history() -> list:
    """Load history of previously sent X signal alerts."""
    history_path = OPENCLAW_DIR / "state" / "x_signal_history.json"
    try:
        with open(history_path) as f:
            data = json.load(f)
            # Only keep last 48h of history
            cutoff = time.time() - 48 * 3600
            return [h for h in data if h.get("timestamp", 0) > cutoff]
    except (OSError, json.JSONDecodeError):
        return []


def _save_signal_history(history: list):
    """Persist sent signal history."""
    history_path = OPENCLAW_DIR / "state" / "x_signal_history.json"
    try:
        # Keep max 200 entries
        with open(history_path, "w") as f:
            json.dump(history[-200:], f, indent=2)
    except OSError:
        pass


def _filter_novel_signals(signals: list, history: list) -> list:
    """Stage 2: Qwen materiality gate — only pass through signals worth an interruption.

    Compares candidate signals against recently sent history.
    Returns only signals that represent genuinely new, actionable developments.
    """
    if not signals:
        return []

    # Build history context for Qwen
    recent_summaries = []
    for h in history[-20:]:  # Last 20 sent signals
        ts = h.get("timestamp", 0)
        age_h = (time.time() - ts) / 3600
        recent_summaries.append(f"- [{age_h:.0f}h ago] {h.get('topic', '?')}: {h.get('summary', '')}")

    history_block = "\n".join(recent_summaries) if recent_summaries else "(No previous alerts sent)"

    # Build candidate signals block
    candidate_block = "\n".join(
        f"- [{s['topic']}] {s['summary']} (confidence: {s['confidence']:.0%}, direction: {s['direction']})"
        for s in signals
    )

    try:
        prompt = (
            "You are a personal alert filter for a prediction market trader. "
            "Your job is to PREVENT notification fatigue. Only let through signals that are genuinely NEW and MATERIAL.\n\n"
            "RECENTLY SENT ALERTS (what the user already knows):\n"
            f"{history_block}\n\n"
            "CANDIDATE NEW SIGNALS:\n"
            f"{candidate_block}\n\n"
            "RULES:\n"
            "- REJECT if the signal covers the same story/development as a recent alert (even with different wording)\n"
            "- REJECT if it's ongoing background noise (e.g. 'Trump discusses tariffs' when tariffs have been in the news for days)\n"
            "- REJECT if there's no concrete new event, just commentary or speculation\n"
            "- ACCEPT only if: (a) a genuinely new development occurred (vote, announcement, emergency, data release, market move), "
            "OR (b) a significant escalation/reversal of something previously reported\n"
            "- When in doubt, REJECT. The user prefers silence over noise.\n\n"
            "Respond in JSON: {\"keep\": [list of topic strings to keep], \"reasoning\": \"one line explaining why\"}"
        )

        result = subprocess.run(
            ["ollama", "run", "qwen3:latest", "--format", "json", prompt],
            capture_output=True, timeout=45, text=True
        )

        if result.returncode != 0:
            _log("Stage 2 filter: Qwen failed, dropping all signals (fail-closed)")
            return []  # Fail closed — silence over noise when filter is broken

        parsed = json.loads(result.stdout.strip())
        keep_topics = set(parsed.get("keep", []))
        reasoning = parsed.get("reasoning", "")

        filtered = [s for s in signals if s["topic"] in keep_topics]
        _log(f"Stage 2 filter: {len(signals)} candidates → {len(filtered)} kept. Reason: {reasoning}")
        return filtered

    except Exception as e:
        _log(f"Stage 2 filter error: {e}, dropping all signals (fail-closed)")
        return []  # Fail closed — silence over noise


# ── Stage 3: Kalshi position gate ─────────────────────────────────────────

def _get_active_kalshi_topics() -> list:
    """Fetch current Kalshi positions and extract topic keywords for matching."""
    try:
        from handlers.kalshi_handlers import _get_client, _check_enabled
        err = _check_enabled()
        if err:
            _log(f"Kalshi position gate: {err}")
            return []

        client = _get_client()
        if not client:
            return []

        resp = client.call_api(
            "GET",
            "https://api.elections.kalshi.com/trade-api/v2/portfolio/positions?limit=100&settlement_status=unsettled"
        )
        data = json.loads(resp.read())
        positions = data.get("market_positions", [])

        if not positions:
            _log("Kalshi position gate: no active positions — suppressing all X signals")
            return []

        topics = []
        for pos in positions:
            ticker = pos.get("ticker", "")
            title = pos.get("market_title", "") or pos.get("title", "")
            event_ticker = pos.get("event_ticker", "")

            keywords = set()
            for field in [ticker, title, event_ticker]:
                for word in field.lower().replace("-", " ").replace("_", " ").split():
                    if len(word) > 2:
                        keywords.add(word)

            topics.append({
                "ticker": ticker,
                "title": title,
                "keywords": keywords,
                "side": pos.get("side", ""),
                "quantity": pos.get("total_traded", 0),
            })

        _log(f"Kalshi position gate: {len(topics)} active positions loaded")
        return topics

    except Exception as e:
        _log(f"Kalshi position gate error: {e}")
        return []  # Fail closed — don't send if we can't verify positions


def _signal_matches_position(signal: dict, positions: list) -> dict:
    """Check if an X signal topic matches any active Kalshi position.

    Returns the matched position dict if found, None otherwise.
    A signal matches if 2+ keywords overlap with position ticker/title.
    """
    signal_topic = signal.get("topic", "").lower()
    signal_summary = signal.get("summary", "").lower()
    signal_words = set(signal_topic.replace("-", " ").replace("_", " ").split())
    signal_words |= set(w for w in signal_summary.split() if len(w) > 3)

    best_match = None
    best_overlap = 0

    for pos in positions:
        overlap = len(signal_words & pos["keywords"])
        if overlap >= 2 and overlap > best_overlap:
            best_overlap = overlap
            best_match = pos

    return best_match


# ── Main trigger ──────────────────────────────────────────────────────────

def check_x_signals(state: dict, dry_run: bool = False, force: bool = False) -> bool:
    """Scan X/Twitter for market-relevant signals via DDG + local Qwen."""
    cfg = PROACTIVE_CFG.get("x_signals", {})
    if not cfg.get("enabled", True):
        return False

    interval = cfg.get("check_interval_minutes", 30)
    last_check = state.get("last_x_signal_check", 0)
    if not force and time.time() - last_check < interval * 60:
        return False

    state["last_x_signal_check"] = time.time()
    topics = cfg.get("topics", [])
    min_confidence = cfg.get("min_confidence", 0.7)

    if not topics:
        return False

    _log(f"X signal scanner starting... ({len(topics)} topics)")

    signals = []
    for topic in topics:
        posts = _search_x_posts(topic)
        if not posts:
            continue

        _log(f"  {topic}: {len(posts)} posts found")
        analysis = _analyze_signals_local(topic, posts)

        if analysis.get("has_signal") and analysis.get("confidence", 0) >= min_confidence:
            signals.append({
                "topic": topic,
                "confidence": analysis["confidence"],
                "direction": analysis.get("direction", "?"),
                "summary": analysis.get("summary", ""),
                "post_count": len(posts),
            })

    # Cache results (all signals, pre-filter — available for morning brief / on-demand)
    cache_path = OPENCLAW_DIR / "state" / "x_signal_cache.json"
    try:
        with open(cache_path, "w") as f:
            json.dump({
                "signals": signals,
                "topics_scanned": len(topics),
                "timestamp": time.time(),
            }, f, indent=2)
    except OSError:
        pass

    _log(f"X scanner: {len(signals)} high-confidence signals from {len(topics)} topics")

    if not signals:
        return False

    # Stage 2: Novelty + materiality filter
    signal_history = _load_signal_history()
    use_materiality = cfg.get("materiality_gate", True)

    if use_materiality:
        signals = _filter_novel_signals(signals, signal_history)
        if not signals:
            _log("X scanner: all signals filtered by materiality gate (nothing new)")
            return False

    # ── Stage 3: Kalshi position gate ──────────────────────────
    # Only iMessage signals that match an active Kalshi position.
    # Everything else is logged silently and cached for morning brief.
    kalshi_positions = _get_active_kalshi_topics()

    if not kalshi_positions:
        _log("X scanner: no active Kalshi positions — all signals suppressed (logged only)")
        state["last_x_signals_silent"] = [
            {"topic": s["topic"], "summary": s["summary"][:80], "confidence": s["confidence"]}
            for s in signals
        ]
        return False

    critical_signals = []
    silent_signals = []

    for s in signals:
        match = _signal_matches_position(s, kalshi_positions)
        if match:
            s["matched_position"] = match["ticker"]
            s["position_side"] = match["side"]
            critical_signals.append(s)
        else:
            silent_signals.append(s)

    if silent_signals:
        _log(f"X scanner: {len(silent_signals)} signals suppressed (no matching Kalshi position)")
        state["last_x_signals_silent"] = [
            {"topic": s["topic"], "summary": s["summary"][:80], "confidence": s["confidence"]}
            for s in silent_signals
        ]

    if not critical_signals:
        _log("X scanner: signals found but none match active Kalshi positions — silent")
        return False

    # Only position-relevant signals get pushed to iMessage
    critical_signals.sort(key=lambda x: x["confidence"], reverse=True)
    parts = ["\u26a0\ufe0f X signal — affects your Kalshi positions:"]
    for s in critical_signals[:3]:
        icon = "\ud83d\udcc8" if s["direction"] == "bullish" else "\ud83d\udcc9" if s["direction"] == "bearish" else "\u27a1\ufe0f"
        pos_info = f"[{s.get('matched_position', '?')}]"
        parts.append(f"  {icon} {s['topic']}: {s['summary'][:80]} ({int(s['confidence']*100)}% conf) {pos_info}")

    message = "\n".join(parts)

    # Route through personality
    data = {
        "signal_count": len(critical_signals),
        "topics": [s["topic"] for s in critical_signals[:3]],
        "max_confidence": max(s["confidence"] for s in critical_signals) if critical_signals else 0,
        "matched_positions": [s.get("matched_position", "") for s in critical_signals[:3]],
    }

    sent = False
    if _HAS_PERSONALITY and not force:
        sent = _personality_send("x_signals", message, data, state, dry_run=dry_run)
    else:
        sent = _send_message(message, dry_run=dry_run)
        if sent:
            _record_send(state, message)

    if sent:
        for s in critical_signals[:3]:
            signal_history.append({
                "topic": s["topic"],
                "summary": s["summary"],
                "direction": s["direction"],
                "confidence": s["confidence"],
                "matched_position": s.get("matched_position"),
                "timestamp": time.time(),
            })
        _save_signal_history(signal_history)
        _log(f"X signal CRITICAL alert: {len(critical_signals)} signals matching Kalshi positions")
        return True
    return False
