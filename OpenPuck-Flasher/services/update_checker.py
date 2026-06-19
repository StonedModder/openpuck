from __future__ import annotations

from urllib.parse import urlparse

import requests

from ..models import UpdateInfo


class UpdateChecker:
    def check_github_commit(self, current: str, endpoint: str) -> UpdateInfo | None:
        if not endpoint:
            return None
        response = requests.get(
            endpoint,
            headers={"Accept": "application/vnd.github+json"},
            timeout=8,
        )
        response.raise_for_status()
        payload = response.json()
        sha = payload.get("sha", "")
        latest = sha[:8] if sha else ""
        if not latest:
            return None
        host = urlparse(endpoint).netloc or "github"
        return UpdateInfo(
            current=current,
            latest=latest,
            source=host,
            url=payload.get("html_url", "https://github.com/"),
            available=current != latest,
        )

