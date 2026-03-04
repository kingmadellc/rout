"""
Web search tools for Rout agent.

Provider-agnostic search: configurable primary + DDG fallback.
Supported providers:
  - ddg (default, no API key required)
  - brave (requires brave.api_key in config.yaml, 1000 free queries/month)
  - searxng (requires searxng.url in config.yaml, self-hosted)

Results are deduplicated and scored for relevance.

Config (config.yaml):
    search:
      provider: "ddg"           # or "brave" or "searxng"
    brave:
      api_key: "BSA..."
    searxng:
      url: "http://localhost:8888"
"""

import json
import re
import urllib.parse
import urllib.request
import yaml
from pathlib import Path


# ── Config ──────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    for candidate in [
        Path(__file__).resolve().parent.parent.parent / "config.yaml",
        Path.home() / ".config/imsg-watcher/config.yaml",
    ]:
        if candidate.exists():
            try:
                with open(candidate) as f:
                    return yaml.safe_load(f) or {}
            except Exception:
                pass
    return {}

_CFG = _load_config()
_SEARCH_PROVIDER = _CFG.get("search", {}).get("provider", "ddg").lower()
_BRAVE_API_KEY = _CFG.get("brave", {}).get("api_key", "")
_SEARXNG_URL = _CFG.get("searxng", {}).get("url", "").rstrip("/")


# ── Result Scoring ──────────────────────────────────────────────────────────

_JUNK_DOMAINS = {
    "pinterest.com", "quora.com", "facebook.com", "tiktok.com",
    "instagram.com", "twitter.com", "x.com",
}

def _score_result(title: str, snippet: str, url: str, query: str) -> float:
    """Score a search result 0-1. Higher = more relevant."""
    score = 0.5
    query_words = set(query.lower().split())

    title_lower = title.lower()
    title_hits = sum(1 for w in query_words if w in title_lower)
    score += min(0.3, title_hits * 0.1)

    snippet_lower = snippet.lower()
    snippet_hits = sum(1 for w in query_words if w in snippet_lower)
    score += min(0.2, snippet_hits * 0.05)

    for domain in _JUNK_DOMAINS:
        if domain in url.lower():
            score -= 0.3
            break

    if any(d in url.lower() for d in ["wikipedia.org", "reuters.com", "apnews.com", ".gov"]):
        score += 0.1

    if len(snippet) < 30:
        score -= 0.15

    return max(0.0, min(1.0, score))


def _dedup_results(results: list) -> list:
    """Remove duplicate results by normalized title."""
    seen = set()
    unique = []
    for r in results:
        key = re.sub(r'\W+', '', r["title"].lower())[:60]
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


# ── DuckDuckGo Lite (default, no API key) ───────────────────────────────────

def _ddg_search(query: str, num_results: int = 5) -> list:
    """Search DuckDuckGo Lite. Zero config required."""
    try:
        data = urllib.parse.urlencode({'q': query}).encode()
        req = urllib.request.Request(
            'https://lite.duckduckgo.com/lite/',
            data=data,
            headers={
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)',
                'Content-Type': 'application/x-www-form-urlencoded',
            }
        )
        resp = urllib.request.urlopen(req, timeout=10)
        html = resp.read().decode('utf-8', errors='ignore')

        titles = re.findall(r"class='result-link'[^>]*>(.*?)</a>", html, re.DOTALL)
        snippets = re.findall(r"class='result-snippet'[^>]*>(.*?)</td>", html, re.DOTALL)
        urls = re.findall(r"class='result-link'\s+href='([^']*)'", html)

        results = []
        for i, (title, snippet) in enumerate(zip(titles, snippets)):
            title = re.sub(r'<[^>]+>', '', title).strip()
            snippet = re.sub(r'<[^>]+>', '', snippet).strip()
            snippet = re.sub(r'\s+', ' ', snippet).replace('&#x27;', "'").replace('&amp;', '&')
            result_url = urls[i] if i < len(urls) else ""

            if title and len(title) > 3:
                score = _score_result(title, snippet, result_url, query)
                results.append({
                    "title": title, "snippet": snippet[:200],
                    "url": result_url, "score": score, "source": "ddg",
                })
            if len(results) >= num_results + 2:
                break
        return results
    except Exception:
        return []


# ── Brave Search API (optional upgrade) ─────────────────────────────────────

def _brave_search(query: str, num_results: int = 5) -> list:
    """Search via Brave Search API. Requires API key."""
    if not _BRAVE_API_KEY:
        return []
    try:
        params = urllib.parse.urlencode({"q": query, "count": min(num_results + 2, 10)})
        url = f"https://api.search.brave.com/res/v1/web/search?{params}"
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "X-Subscription-Token": _BRAVE_API_KEY,
        })
        resp = urllib.request.urlopen(req, timeout=8)
        data = json.loads(resp.read().decode("utf-8", errors="ignore"))

        results = []
        for item in data.get("web", {}).get("results", []):
            title = item.get("title", "").strip()
            snippet = item.get("description", "").strip()
            result_url = item.get("url", "")
            if title:
                score = _score_result(title, snippet, result_url, query)
                results.append({
                    "title": title, "snippet": snippet[:200],
                    "url": result_url, "score": score, "source": "brave",
                })
        return results
    except Exception:
        return []


# ── SearXNG (self-hosted meta-search) ───────────────────────────────────────

def _searxng_search(query: str, num_results: int = 5) -> list:
    """Search via self-hosted SearXNG instance."""
    if not _SEARXNG_URL:
        return []
    try:
        params = urllib.parse.urlencode({
            "q": query, "format": "json", "categories": "general",
        })
        url = f"{_SEARXNG_URL}/search?{params}"
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "User-Agent": "Rout/0.8.0",
        })
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read().decode("utf-8", errors="ignore"))

        results = []
        for item in data.get("results", []):
            title = item.get("title", "").strip()
            snippet = item.get("content", "").strip()
            result_url = item.get("url", "")
            if title:
                score = _score_result(title, snippet, result_url, query)
                results.append({
                    "title": title, "snippet": snippet[:200],
                    "url": result_url, "score": score, "source": "searxng",
                })
            if len(results) >= num_results + 2:
                break
        return results
    except Exception:
        return []


# ── Provider Router ─────────────────────────────────────────────────────────

_PROVIDERS = {
    "ddg": _ddg_search,
    "brave": _brave_search,
    "searxng": _searxng_search,
}

def _search_with_fallback(query: str, num_results: int = 5) -> list:
    """Try configured provider, fall back to DDG."""
    primary = _PROVIDERS.get(_SEARCH_PROVIDER, _ddg_search)
    results = primary(query, num_results)
    if results:
        return results

    # Fallback chain: try everything else
    for name, fn in _PROVIDERS.items():
        if fn != primary:
            results = fn(query, num_results)
            if results:
                return results
    return []


# ── Public Interface ────────────────────────────────────────────────────────

def web_search(query: str, num_results: int = 5) -> str:
    """Search the web. Provider-agnostic with automatic fallback."""
    results = _search_with_fallback(query, num_results)

    if not results:
        return "No search results found."

    # Dedup, score-sort, take top N
    results = _dedup_results(results)
    results.sort(key=lambda r: r["score"], reverse=True)
    results = results[:num_results]

    formatted = []
    for r in results:
        line = f"• {r['title']}"
        if r["snippet"]:
            line += f": {r['snippet']}"
        formatted.append(line)

    return "\n".join(formatted)
