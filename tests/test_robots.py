"""robots.txt — RFC 9309 parsing, and the boundary that matters.

The parser tests are ordinary. The important tests are at the bottom: they
assert that a `Disallow: /` NEVER prevents a user-directed single retrieval.

robots.txt states the owner's intent about automated CRAWLERS — agents that
discover and traverse a link graph on their own initiative. htmldrill's
single-target commands are assistive: they fetch one URL because a person asked
for that URL, exactly as a browser, a screen reader, or a reader-mode extension
does, none of which consult robots.txt. `crawl` is the one command that really
does traverse a link graph, and it is the one command the file binds.

That boundary is easy to erode by accident — one plausible-looking wire from the
robots verdict into the classifier and htmldrill starts silently refusing people
pages they asked for. These tests exist to make that erosion fail loudly.
"""
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from htmldrill import commands as C            # noqa: E402
from htmldrill import robots as R              # noqa: E402
from htmldrill.sidecar import Sidecar          # noqa: E402

UA = "htmldrill/0.1 (+https://example.test)"

SAMPLE = """
# a typical file
User-agent: *
Disallow: /private/
Disallow: /tmp
Allow: /private/public-note.html
Crawl-delay: 2

User-agent: htmldrill
User-agent: somebot
Disallow: /nothing-for-you/

Sitemap: https://host.test/sitemap.xml
"""


# -- parsing ----------------------------------------------------------------

def test_groups_and_sitemaps_parse():
    r = R.parse(SAMPLE)
    assert len(r.groups) == 2
    assert r.groups[0].agents == ["*"]
    assert r.groups[1].agents == ["htmldrill", "somebot"]
    assert r.sitemaps == ["https://host.test/sitemap.xml"]


def test_comments_and_blank_lines_are_ignored():
    r = R.parse("# only a comment\n\n   \nUser-agent: *\nDisallow: /x\n")
    assert len(r.groups) == 1 and len(r.groups[0].rules) == 1


def test_the_most_specific_user_agent_group_wins():
    r = R.parse(SAMPLE)
    g, name = r.group_for(UA)
    assert name == "htmldrill"
    assert r.group_for("SomeOtherBot/2.0")[1] == "*"


def test_consecutive_user_agent_lines_share_one_group():
    r = R.parse("User-agent: a\nUser-agent: b\nDisallow: /z\n")
    assert len(r.groups) == 1 and r.groups[0].agents == ["a", "b"]


def test_a_user_agent_after_a_rule_starts_a_new_group():
    r = R.parse("User-agent: a\nDisallow: /1\nUser-agent: b\nDisallow: /2\n")
    assert [g.agents for g in r.groups] == [["a"], ["b"]]


@pytest.mark.parametrize("pattern,path,hit", [
    ("/private/", "/private/x", True),
    ("/private/", "/public/x", False),
    ("/*.pdf$", "/a/b/c.pdf", True),
    ("/*.pdf$", "/a/b/c.pdf?x=1", False),      # $ anchors the end
    ("/a*b", "/azzzb", True),
    ("/", "/anything", True),
    ("", "/anything", False),                  # an empty Disallow is a no-op
])
def test_path_matching(pattern, path, hit):
    assert R._matches(pattern, path) is hit


def test_longest_matching_rule_wins_and_allow_breaks_a_tie():
    r = R.parse(SAMPLE)
    # OtherBot falls to the `*` group; UA's own product token is `htmldrill`,
    # which has its own group and would (correctly) not be covered by these rules
    assert r.verdict("/private/secret.html", "OtherBot/1.0").allowed is False
    assert r.verdict("/private/secret.html", UA).allowed is True
    v = r.verdict("/private/public-note.html", "OtherBot")
    assert v.allowed is True, "the longer Allow must beat the shorter Disallow"
    assert "Allow: /private/public-note.html" == v.rule_text


def test_crawl_delay_is_read_per_group():
    assert R.parse(SAMPLE).crawl_delay_for("OtherBot") == 2.0


def test_every_verdict_names_the_rule_and_group_that_produced_it():
    v = R.parse(SAMPLE).verdict("/private/x", "OtherBot")
    assert v.group == "*" and v.rule_text.startswith("Disallow:") and v.reason


def test_an_empty_or_garbled_file_restricts_nothing():
    for text in ("", "   ", "not a robots file at all", "Disallow: /x"):
        assert R.parse(text).verdict("/anything", UA).allowed is True


def test_silence_is_not_a_prohibition():
    """An unreachable robots.txt means no preference was STATED. Treating that
    as a prohibition would invent an intent the owner never expressed."""
    def boom(_url):
        raise OSError("connection refused")
    doc = R.fetch("https://host.test/a", boom, ua=UA)
    assert doc.fetched is False
    assert doc.verdict("/a", UA).allowed is True


# -- the boundary -----------------------------------------------------------

DISALLOW_ALL = "User-agent: *\nDisallow: /\n"


def test_a_blanket_disallow_never_blocks_a_user_directed_retrieval():
    """The whole point. A person asked for this page; htmldrill is not crawling."""
    from httpfixture import Origin
    with Origin() as o, tempfile.TemporaryDirectory() as work:
        ctx = C.Ctx(url=o.url("/ok"), work=work)
        C.cmd_fetch(ctx)                      # must not consult robots.txt at all
        out = C.cmd_classify(ctx)
        sc = Sidecar(C._resolve_id(ctx), work=work)
        assert sc.get_evidence("pagekind")["access"]["value"] != "blocked"
        assert sc.get_evidence("pagekind_terminal") != "NOT_RETRIEVABLE_BLOCKED"
        assert "NOT_RETRIEVABLE" not in out


def test_single_target_classification_can_never_produce_access_blocked():
    """`ac.robots` is CRAWL-scoped: the classifier is never handed a robots
    verdict on the single-retrieval path, so `blocked` is unreachable there.

    If someone later wires the robots verdict into `_snapshot_observation`,
    this fails — which is the intended alarm, not an inconvenience."""
    import inspect
    src = inspect.getsource(C._snapshot_observation)
    assert "robots" not in src.lower(), (
        "the single-retrieval observation path now consults robots.txt — "
        "that turns an owner's crawling preference into a refusal of a person's "
        "own request")


def test_fetch_does_not_request_robots_txt():
    from httpfixture import Origin
    with Origin() as o, tempfile.TemporaryDirectory() as work:
        C.cmd_fetch(C.Ctx(url=o.url("/ok"), work=work))
        asked = [h for h in o.seen_paths if "robots" in h]
        assert asked == [], f"single-target fetch asked for {asked}"


def test_the_robots_command_reports_without_refusing():
    from httpfixture import Origin
    with Origin() as o, tempfile.TemporaryDirectory() as work:
        out = C.cmd_robots(C.Ctx(url=o.url("/private/page"), work=work))
        assert "DISALLOWED by the owner" in out
        assert "does NOT gate" in out or "not crawling" in out
        sc = Sidecar(C.F.local_id_for(o.url("/private/page")), work=work)
        assert sc.has("ROBOTS_KNOWN")
        assert sc.get_evidence("robots_crawl_allowed") is False
        # ...and the page is still retrievable, because a person asked for it
        C.cmd_fetch(C.Ctx(url=o.url("/private/page"), work=work))
        assert Sidecar(C.F.local_id_for(o.url("/private/page")), work=work).has("FETCHED")


def test_the_robots_command_records_an_allowed_path_too():
    from httpfixture import Origin
    with Origin() as o, tempfile.TemporaryDirectory() as work:
        out = C.cmd_robots(C.Ctx(url=o.url("/ok"), work=work))
        assert "ALLOWED" in out
        sc = Sidecar(C.F.local_id_for(o.url("/ok")), work=work)
        assert sc.get_evidence("robots_crawl_allowed") is True
        assert sc.get_evidence("robots_sitemaps")


# -- where the owner's intent DOES bind -------------------------------------

def test_crawl_honours_the_owners_policy():
    """`crawl` follows a link graph on its own initiative — that IS crawling,
    and it is the one place robots.txt binds."""
    from httpfixture import Origin
    with Origin() as o:
        assert C._robots_ok(o.url("/private/page"), "htmldrill/0.1") is False
        assert C._robots_ok(o.url("/ok"), "htmldrill/0.1") is True


def test_crawl_proceeds_when_no_policy_was_stated():
    assert C._robots_ok("http://127.0.0.1:1/x", "htmldrill/0.1") is True


def test_a_file_url_is_not_a_web_origin():
    assert C._robots_ok("file:///tmp/a.html", "htmldrill/0.1") is True
