"""
Web search tools for Rout agent.

DuckDuckGo Lite search — no API key required.
"""

import re
import urllib.parse
import urllib.request


def web_search(query: str, num_results: int = 4) -> str:
    """Search DuckDuckGo lite and return top result snippets."""
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

        results = []
        for title, snippet in zip(titles, snippets):
            title = re.sub(r'<[^>]+>', '', title).strip()
            snippet = re.sub(r'<[^>]+>', '', snippet).strip()
            snippet = re.sub(r'\s+', ' ', snippet).replace('&#x27;', "'").replace('&amp;', '&')
            if title and len(title) > 3:
                results.append(f"• {title}: {snippet[:200]}" if snippet else f"• {title}")
            if len(results) >= num_results:
                break

        if not results:
            return "No search results found."
        return "\n".join(results)
    except Exception as e:
        return f"[Search failed: {e}]"
