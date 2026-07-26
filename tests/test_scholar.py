"""scholar — paginate a Google Scholar profile past the "Show more" wall and merge
all works into one document.

Offline: an injected fetcher serves synthetic multi-page HTML, so pagination +
merge are pinned without touching the network.

    python3 -m pytest tests/test_scholar.py
    PYTHONPATH=src python3 tests/test_scholar.py
"""
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from htmldrill.sources import scholar as SCH      # noqa: E402

PROFILE = "https://scholar.google.com/citations?hl=en&user=ABC123&view_op=list_works&sortby=pubdate"


def _row(i: int) -> str:
    return (f'<tr class="gsc_a_tr"><td class="gsc_a_t">'
            f'<a href="/citations?citation_for_view=ABC:{i}" class="gsc_a_at">Work {i}</a>'
            f'<div class="gs_gray">A Author, B Author</div>'
            f'<div class="gs_gray">Some Venue<span class="gs_oph">, {2000 + i}</span></div></td>'
            f'<td class="gsc_a_c"><a class="gsc_a_ac gs_ibl">{i}</a></td>'
            f'<td class="gsc_a_y"><span class="gsc_a_h gsc_a_hc gs_ibl">{2000 + i}</span></td></tr>')


def _page(n_rows: int) -> str:
    rows = "".join(_row(i) for i in range(n_rows))
    return f'<html><body><table><tbody id="gsc_a_b">{rows}</tbody></table></body></html>'


def _paged_fetcher(total: int, pagesize: int = 100):
    """Serve `total` works across cstart pages of `pagesize`."""
    def _f(url: str) -> str:
        q = parse_qs(urlparse(url).query)
        cstart = int(q.get("cstart", ["0"])[0])
        ps = int(q.get("pagesize", [str(pagesize)])[0])
        return _page(max(0, min(ps, total - cstart)))
    return _f


def test_recognises_scholar_profiles_only():
    assert SCH.is_scholar_citations(PROFILE)
    assert SCH.is_scholar_citations("https://scholar.google.de/citations?user=X")
    assert not SCH.is_scholar_citations("https://scholar.google.com/scholar?q=foo")   # search, not profile
    assert not SCH.is_scholar_citations("https://example.com/citations?user=X")
    assert not SCH.is_scholar_citations("not-a-url")


def test_paginates_and_builds_clean_list():
    html, total, pages = SCH.fetch_all_works(PROFILE, _paged_fetcher(315))
    assert total == 315
    assert pages == 4                                   # 100+100+100+15
    # a CLEAN static list — one <li> per work, NO Scholar JS / "Show more" button
    assert len(re.findall(r"<li ", html)) == 315
    assert "gsc_a_tr" not in html and "gsc_bpr_more" not in html and "<script" not in html
    # real fields extracted: title, id (citation key), year, absolute link
    assert '<span class="title">Work 0</span>' in html
    assert 'id="scholar-ABC-0"' in html
    assert 'href="https://scholar.google.com/citations?citation_for_view=ABC:0"' in html


def test_single_full_page_stops_after_one_fetch():
    merged, total, pages = SCH.fetch_all_works(PROFILE, _paged_fetcher(42))
    assert total == 42 and pages == 1                   # short first page = done


def test_exact_multiple_needs_trailing_empty_page():
    # 200 works: pages of 100,100, then an empty page confirms the end
    _, total, pages = SCH.fetch_all_works(PROFILE, _paged_fetcher(200))
    assert total == 200 and pages == 3                  # 100 + 100 + 0


def test_page_url_sets_pagination_params():
    u = SCH._page_url(PROFILE, 200, 100)
    q = parse_qs(urlparse(u).query)
    assert q["cstart"] == ["200"] and q["pagesize"] == ["100"]
    assert q["user"] == ["ABC123"]                      # original params preserved
    # existing cstart is replaced, not duplicated
    u2 = SCH._page_url(PROFILE + "&cstart=0&pagesize=20", 100, 100)
    assert parse_qs(urlparse(u2).query)["cstart"] == ["100"]


def test_router_points_scholar_at_the_scholar_command():
    from htmldrill.sources import router as RT
    v = RT.route(PROFILE)
    assert v.drill == "htmldrill" and v.rule == "scholar"
    assert "scholar" in v.action and "Show more" in v.reason


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
