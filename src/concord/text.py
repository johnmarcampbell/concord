"""Plain-text extraction for Congressional Record articles.

Articles served under ``congress.gov/.../modified/CREC-*.htm`` are a single
``<pre>`` block wrapping the article body with the occasional inline ``<a>``
tag pointing at gpo.gov. Extraction is: GET the URL, parse the HTML with
``html.parser`` from the stdlib, return the text inside ``<pre>`` with tags
dropped but their inner text preserved.

No bs4, no lxml — the format is stable enough that one stdlib parser does the
job and removes a dependency the rest of the project doesn't need.
"""

from __future__ import annotations

from html.parser import HTMLParser

import httpx


class TextFetchError(Exception):
    """Raised when an article URL can't be fetched or contains no ``<pre>`` block.

    ``status_code`` is the HTTP status for response-level failures, ``None``
    for transport failures and structural problems with the HTML itself.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class _PreExtractor(HTMLParser):
    """Accumulate text inside ``<pre>`` blocks; drop tags, keep their text.

    Tracks nesting depth so that defensively-malformed HTML (extra closing
    tags, etc.) doesn't break extraction. Anchor tags inside ``<pre>`` are
    dropped — only their inner text survives, which is exactly what you want
    for human-readable plain text.
    """

    def __init__(self) -> None:
        super().__init__()
        self._depth = 0
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "pre":
            self._depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "pre" and self._depth > 0:
            self._depth -= 1

    def handle_data(self, data: str) -> None:
        if self._depth > 0:
            self._chunks.append(data)

    @property
    def text(self) -> str:
        return "".join(self._chunks).strip()


def fetch_text(url: str, client: httpx.Client) -> str:
    """Fetch an article URL and return its plain text.

    The caller owns the :class:`httpx.Client` (so connection pooling, custom
    transports, and timeout policy are external concerns). Redirects are
    followed automatically.

    Raises :class:`TextFetchError` on:

    - non-success HTTP status (``status_code`` populated)
    - transport-level failures (``status_code`` is ``None``)
    - HTML that contains no ``<pre>`` block (``status_code`` is ``None``)
    """
    try:
        response = client.get(url, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise TextFetchError(
            f"{exc.response.status_code} {exc.response.reason_phrase} fetching {url}",
            status_code=exc.response.status_code,
        ) from exc
    except httpx.HTTPError as exc:
        raise TextFetchError(f"transport error fetching {url}: {exc}") from exc

    extractor = _PreExtractor()
    extractor.feed(response.text)
    text = extractor.text
    if not text:
        raise TextFetchError(f"no <pre> content found at {url}")
    return text
