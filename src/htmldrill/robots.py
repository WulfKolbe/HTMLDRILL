"""robots — read the site owner's stated CRAWLING policy (RFC 9309).

WHAT THIS IS FOR, AND WHAT IT IS NOT FOR

``robots.txt`` is a statement of the owner's intent about **automated
crawlers**: agents that discover and traverse a site's link graph on their own
initiative. RFC 9309 is written for exactly that, and htmldrill honours it where
htmldrill does exactly that — in ``crawl``, which follows links and visits pages
nobody asked for individually.

It is **not** a general access-control mechanism, and it does not govern a tool
retrieving one specific URL because a person asked for that URL. A browser does
not consult robots.txt. Neither does a screen reader, a reader-mode extension,
or a "save to read later" service — all of which fetch on a user's behalf, one
resource at a time, at human initiative. htmldrill's single-target commands
(``fetch``, ``classify``, ``underlying``) are in that class: assistive, not
autonomous.

So the boundary this module draws is deliberate:

    crawl            robots.txt BINDS      — link-graph traversal is crawling
    single retrieval robots.txt is REPORTED — user-directed access is not

Reporting still matters: it is the owner's expressed preference, and a user is
entitled to see it. `htmldrill robots <url>` says what the owner asked of
crawlers, names the exact rule that matched, and states plainly that it does not
gate a user-directed fetch. Surfacing the intent is respect; silently refusing a
person the page they asked for is not.

The parser is pure — ``parse(text)`` takes the file, ``verdict()`` answers about
a path — so all of it is testable offline.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse, urlunparse

DEFAULT_GROUP = "*"


@dataclass(frozen=True)
class Rule:
    """One Allow/Disallow line."""
    allow: bool
    pattern: str

    @property
    def specificity(self) -> int:
        """RFC 9309 §2.2.2: the longest matching path pattern wins."""
        return len(self.pattern)


@dataclass
class Group:
    """One record: the user-agent tokens it applies to, and its rules."""
    agents: list[str] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    crawl_delay: Optional[float] = None


@dataclass
class Verdict:
    """Whether a CRAWLER may traverse a path, and exactly why."""
    allowed: bool
    rule: Optional[Rule]
    group: str
    reason: str

    @property
    def rule_text(self) -> str:
        if self.rule is None:
            return "(no matching rule)"
        return f"{'Allow' if self.rule.allow else 'Disallow'}: {self.rule.pattern}"


@dataclass
class Robots:
    """A parsed robots.txt."""
    groups: list[Group] = field(default_factory=list)
    sitemaps: list[str] = field(default_factory=list)
    source_url: str = ""
    fetched: bool = False
    status: Optional[int] = None
    raw: str = ""

    # -- group selection ----------------------------------------------------

    def group_for(self, ua: str) -> tuple[Optional[Group], str]:
        """The most specific group for a user-agent product token, per RFC 9309:
        the longest ``User-agent`` value the token starts with, else ``*``."""
        token = _product_token(ua)
        best: tuple[int, Optional[Group], str] = (-1, None, DEFAULT_GROUP)
        wildcard: tuple[Optional[Group], str] = (None, DEFAULT_GROUP)
        for g in self.groups:
            for agent in g.agents:
                a = agent.lower()
                if a == DEFAULT_GROUP:
                    if wildcard[0] is None:
                        wildcard = (g, DEFAULT_GROUP)
                    continue
                if token.startswith(a) and len(a) > best[0]:
                    best = (len(a), g, agent)
        if best[1] is not None:
            return best[1], best[2]
        return wildcard

    # -- the question ------------------------------------------------------

    def verdict(self, url_or_path: str, ua: str) -> Verdict:
        """May an automated CRAWLER fetch this path? Never consulted for a
        user-directed single retrieval — see the module docstring."""
        path = _path_of(url_or_path)
        group, name = self.group_for(ua)
        if group is None:
            return Verdict(True, None, name,
                           "no group applies to this user-agent — crawling unrestricted")
        matches = [r for r in group.rules if _matches(r.pattern, path)]
        if not matches:
            return Verdict(True, None, name, "no rule in the matching group covers this path")
        # longest pattern wins; Allow beats Disallow on an exact tie
        best = max(matches, key=lambda r: (r.specificity, r.allow))
        return Verdict(best.allow, best, name,
                       f"longest matching rule in the `{name}` group")

    def crawl_delay_for(self, ua: str) -> Optional[float]:
        group, _ = self.group_for(ua)
        return group.crawl_delay if group else None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _product_token(ua: str) -> str:
    """The bare product token of a User-Agent string, lowercased.
    ``htmldrill/0.1 (+https://…)`` -> ``htmldrill``."""
    head = (ua or "").strip().split()[0] if (ua or "").strip() else ""
    return head.split("/")[0].lower()


def _path_of(url_or_path: str) -> str:
    if "://" in url_or_path:
        p = urlparse(url_or_path)
        return (p.path or "/") + (f"?{p.query}" if p.query else "")
    return url_or_path or "/"


def _matches(pattern: str, path: str) -> bool:
    """RFC 9309 §2.2.3 path matching: ``*`` is any sequence, ``$`` anchors the
    end. Everything else is a literal prefix match."""
    if pattern == "":
        return False                       # an empty Disallow is a no-op rule
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
    return re.match(regex + ("$" if anchored else ""), path) is not None


def parse(text: str) -> Robots:
    """Parse robots.txt into groups. Consecutive ``User-agent`` lines share a
    group; a ``User-agent`` after any rule starts a new one."""
    robots = Robots(raw=text or "")
    current: Optional[Group] = None
    accepting_agents = False
    for raw_line in (text or "").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field_name, _, value = line.partition(":")
        key, value = field_name.strip().lower(), value.strip()

        if key == "user-agent":
            if current is None or not accepting_agents:
                current = Group()
                robots.groups.append(current)
                accepting_agents = True
            current.agents.append(value)
            continue
        if key == "sitemap":
            robots.sitemaps.append(value)
            continue
        if current is None:
            continue                        # a rule before any User-agent: ignore
        accepting_agents = False
        if key in ("disallow", "allow"):
            current.rules.append(Rule(allow=(key == "allow"), pattern=value))
        elif key == "crawl-delay":
            try:
                current.crawl_delay = float(value)
            except ValueError:
                pass
    return robots


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

def robots_url(url: str) -> str:
    """The robots.txt URL for a target's origin."""
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, "/robots.txt", "", "", ""))


def fetch(url: str, fetcher, ua: str = "") -> Robots:
    """Retrieve and parse the origin's robots.txt.

    An absent or unreachable robots.txt means the owner stated no crawling
    preference — which is NOT the same as stating a prohibition. It parses to an
    empty policy that allows everything, and ``fetched`` records whether we
    actually heard from the origin so a report can distinguish "no restrictions"
    from "we could not ask".
    """
    target = robots_url(url)
    try:
        res = fetcher(target)
    except Exception:                       # noqa: BLE001 — unreachable robots is not a refusal
        out = Robots(source_url=target, fetched=False)
        return out
    body = res.body.decode("utf-8", "replace") if isinstance(res.body, bytes) else str(res.body)
    if res.status != 200:
        out = Robots(source_url=target, fetched=True, status=res.status)
        return out
    out = parse(body)
    out.source_url, out.fetched, out.status = target, True, res.status
    return out
