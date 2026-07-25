"""router — the modular "which *drill tool handles this URL?" verdict.

Pure/offline: no network, just URL classification. Pins the clear answers the
routing contract promises (arXiv→pdfdrill, video→YTDRILL, auth chat→CHATDRILL,
everything else→htmldrill) and the modularity guarantees (rule order, extension).

    python3 -m pytest tests/test_router.py
    PYTHONPATH=src python3 tests/test_router.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from htmldrill.sources import router as RT      # noqa: E402


def _drill(url):
    return RT.route(url).drill


def test_arxiv_routes_to_pdfdrill():
    assert _drill("https://arxiv.org/abs/2501.18823") == "pdfdrill"
    assert _drill("https://arxiv.org/pdf/2501.18823") == "pdfdrill"
    assert _drill("2501.18823") == "pdfdrill"            # bare id too


def test_pdf_link_routes_to_pdfdrill():
    assert _drill("https://example.com/paper.pdf") == "pdfdrill"
    assert RT.route("https://example.com/paper.pdf").rule == "pdf"


def test_youtube_routes_to_ytdrill():
    assert _drill("https://www.youtube.com/watch?v=BkA0bkb6ZO0") == "ytdrill"
    assert _drill("https://youtu.be/BkA0bkb6ZO0") == "ytdrill"
    assert _drill("https://www.youtube.com/shorts/abc123") == "ytdrill"
    # the youtube HOME page is not a video → not YTDRILL
    assert _drill("https://www.youtube.com/") == "htmldrill"


def test_auth_chat_routes_to_chatdrill():
    v = RT.route("https://www.perplexity.ai/search/0560f9be-08f0-4974-b1ff-56483f879266")
    assert v.drill == "chatdrill" and v.handler == "CHATDRILL"
    assert "app shell" in v.reason                       # the clear auth answer
    assert _drill("https://chatgpt.com/share/abc") == "chatdrill"
    assert _drill("https://claude.ai/chat/xyz") == "chatdrill"
    # a chat host on a NON-conversation path is not routed to CHATDRILL
    assert _drill("https://www.perplexity.ai/about") == "htmldrill"


def test_ordinary_pages_default_to_htmldrill():
    for u in ("https://transformer-circuits.pub/2026/emotions/index.html",
              "https://example.com/blog/post",
              "https://someblog.dev/"):
        v = RT.route(u)
        assert v.drill == "htmldrill" and v.known is False and v.rule == "default"


def test_verdict_is_always_actionable():
    """Every verdict names a handler, a reason, and a next action — never blank."""
    for u in ("https://arxiv.org/abs/1", "https://youtu.be/x",
              "https://chatgpt.com/share/y", "https://a.dev/", "not-a-url"):
        v = RT.route(u)
        assert v.handler and v.reason and v.action


def test_rules_are_ordered_and_first_match_wins():
    """A .pdf on an arXiv host still resolves (arxiv rule precedes pdf); the
    registry is an ordered, inspectable list — the modularity contract."""
    assert isinstance(RT.RULES, list) and len(RT.RULES) >= 4
    assert RT.route("https://arxiv.org/pdf/2501.18823").rule == "arxiv"


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in fns:
        try:
            fn(); print(f"  ✓ {fn.__name__}"); passed += 1
        except AssertionError as e:
            print(f"  ✗ {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {fn.__name__}: {type(e).__name__}: {e}")
    print(f"{passed}/{len(fns)} passed")
    return 0 if passed == len(fns) else 1


if __name__ == "__main__":
    raise SystemExit(_run_all())
