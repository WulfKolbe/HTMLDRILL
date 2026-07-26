"""URL routing — a clear answer to "which *drill tool should handle this URL?".

A pasted URL is not always htmldrill's job. An arXiv link is a PDF/LaTeX paper
(pdfdrill); a youtube.com/watch is a video (YTDRILL); a perplexity.ai/search is
an authenticated conversation whose static HTML is just an empty app shell
(CHATDRILL). :func:`route` gives a definite verdict for each, defaulting to
htmldrill for ordinary web pages.

DESIGN — deliberately modular, for lift into a shared "anydrill" state machine:
  * Pure + stdlib (url parsing only) — no htmldrill internals, no network. It
    classifies; it does not fetch or invoke anything.
  * Each rule is an independent ``url -> Verdict | None`` function in the ordered
    :data:`RULES` list. Add a host by appending a rule (or an entry to the host
    tables); move the whole registry by copying this one file.
  * The verdict NAMES the handler and says WHY + WHAT NEXT — it never pretends to
    solve auth or download the video; a clear answer is the contract.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlparse

from .known_hosts import host_of, is_url, known_host, parse_arxiv_id
from .orcid import is_orcid
from .scholar import is_scholar_citations
from .semanticscholar import is_s2_author


@dataclass(frozen=True)
class Verdict:
    drill: str          # canonical tool id: htmldrill | pdfdrill | chatdrill | ytdrill
    handler: str        # display name for the message
    reason: str         # why this URL belongs to that tool
    action: str         # the concrete next step
    rule: str           # which rule matched (for tracing)
    known: bool = True   # False only for the generic htmldrill default


def _bare_host(url: str) -> str:
    h = host_of(url)
    return h[4:] if h.startswith("www.") else h


# host → path prefixes that are conversations (empty tuple = the whole host).
# The extension point for CHATDRILL-handled sources.
_CHAT_HOSTS: dict[str, tuple[str, ...]] = {
    "perplexity.ai": ("/search", "/page"),
    "chatgpt.com": ("/share", "/c", "/g"),
    "chat.openai.com": ("/share", "/c"),
    "claude.ai": ("/chat", "/share", "/project", "/recents"),
    "gemini.google.com": ("/app", "/share"),
    "poe.com": (),
    "you.com": ("/search",),
    "phind.com": ("/search",),
    "copilot.microsoft.com": (),
    "grok.com": (),
}

# video hosts YTDRILL owns.
_VIDEO_HOSTS = {"youtube.com", "m.youtube.com", "youtu.be", "youtube-nocookie.com"}


def _rule_arxiv(url: str) -> Optional[Verdict]:
    if known_host(url) == "arxiv" or parse_arxiv_id(url):
        return Verdict(
            "pdfdrill", "pdfdrill",
            "arXiv paper — the real content is a PDF / LaTeX e-print, not the HTML abstract page",
            "htmldrill arxiv <url>  (free metadata, then hand the PDF / e-print to pdfdrill)",
            rule="arxiv")
    return None


def _rule_pdf(url: str) -> Optional[Verdict]:
    if urlparse(url).path.lower().endswith(".pdf"):
        return Verdict("pdfdrill", "pdfdrill", "direct PDF link",
                       "pdfdrill md <url>", rule="pdf")
    return None


def _rule_video(url: str) -> Optional[Verdict]:
    h = _bare_host(url)
    if h not in _VIDEO_HOSTS:
        return None
    # on youtube.com only the watch/shorts/live/embed paths are videos (not the
    # home page or channel pages); youtu.be/<id> is always a video short-link.
    if h == "youtube.com" and not urlparse(url).path.startswith(
            ("/watch", "/shorts", "/live", "/embed")):
        return None
    return Verdict(
        "ytdrill", "YTDRILL",
        "video page — the content is a video + transcript/captions, not extractable article HTML",
        "hand to YTDRILL (video id + transcript/caption track)", rule="video")


def _rule_scholar(url: str) -> Optional[Verdict]:
    if is_scholar_citations(url):
        return Verdict(
            "htmldrill", "htmldrill",
            "Google Scholar profile — a paginated 'Show more' works list; the full "
            "set is reachable via cstart/pagesize (no browser clicks)",
            "htmldrill scholar <url>  (fetches every page, merges all works)",
            rule="scholar", known=True)
    return None


def _rule_orcid(url: str) -> Optional[Verdict]:
    if is_orcid(url):
        return Verdict(
            "htmldrill", "htmldrill",
            "ORCID record — works load from the public API behind a 'Show more' "
            "button; the API returns the whole list in one call",
            "htmldrill orcid <url>  (fetches every work via pub.orcid.org)",
            rule="orcid", known=True)
    return None


def _rule_semanticscholar(url: str) -> Optional[Verdict]:
    if is_s2_author(url):
        return Verdict(
            "htmldrill", "htmldrill",
            "Semantic Scholar author — a paged paper list; the Graph API returns "
            "every paper via offset/limit (no browser clicks)",
            "htmldrill semanticscholar <url>  (fetches every paper via the Graph API)",
            rule="semanticscholar", known=True)
    return None


def _rule_chat(url: str) -> Optional[Verdict]:
    prefixes = _CHAT_HOSTS.get(_bare_host(url))
    if prefixes is None:
        return None
    path = urlparse(url).path or "/"
    if prefixes and not any(path.startswith(p) for p in prefixes):
        return None
    return Verdict(
        "chatdrill", "CHATDRILL",
        "authenticated chat/search — a static fetch returns only the app shell "
        "(no conversation); the logged-in session is required to see the content",
        "hand to CHATDRILL (session/cookies), not htmldrill", rule="chat")


#: ordered rule registry — first match wins. Append to extend.
RULES: list[Callable[[str], Optional[Verdict]]] = [
    _rule_arxiv, _rule_pdf, _rule_video,
    _rule_scholar, _rule_orcid, _rule_semanticscholar,
    _rule_chat,
]


def route(url: str) -> Verdict:
    """Classify ``url`` and return a definite handler verdict (never None)."""
    if not is_url(url):
        # a local path / bare id: still let arXiv-id detection speak, else htmldrill
        v = _rule_arxiv(url)
        if v:
            return v
        return Verdict("htmldrill", "htmldrill",
                       "local file or non-http path — htmldrill reads it directly",
                       "htmldrill fetch <path>", rule="default", known=False)
    for rule in RULES:
        v = rule(url)
        if v is not None:
            return v
    return Verdict("htmldrill", "htmldrill",
                   "ordinary web page (static or JS-rendered) — htmldrill's home ground",
                   "htmldrill fetch <url>  →  size · single · model", rule="default", known=False)
