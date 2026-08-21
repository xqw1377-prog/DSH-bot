"""受控 HTTP。必须带 User-Agent；SEC 遵守 Fair Access。"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

DEFAULT_UA = "DSH-intelligence-ingest/0.1 (local research; +https://localhost)"


@dataclass
class Fetched:
    url: str
    status_code: int
    text: str
    headers: dict[str, str]


class Fetcher:
    def __init__(self, client: httpx.Client | None = None, user_agent: str = DEFAULT_UA):
        self.user_agent = user_agent
        self._client = client

    def get(self, url: str, timeout: float = 8.0) -> Fetched:
        headers = {"User-Agent": self.user_agent, "Accept": "application/atom+xml, application/rss+xml, text/html, */*"}
        if self._client is not None:
            resp = self._client.get(url, headers=headers, timeout=timeout)
        else:
            resp = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=True)
        return Fetched(
            url=str(resp.url),
            status_code=resp.status_code,
            text=resp.text,
            headers={k.lower(): v for k, v in resp.headers.items()},
        )
