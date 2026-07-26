"""orcid + semanticscholar — fetch every work from a public JSON API and
synthesise a works-list snapshot. Offline: injected JSON fetchers, no network.

    python3 -m pytest tests/test_orcid_s2.py
    PYTHONPATH=src python3 tests/test_orcid_s2.py
"""
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from htmldrill.sources import orcid as ORC              # noqa: E402
from htmldrill.sources import semanticscholar as S2     # noqa: E402
from htmldrill.sources import router as RT              # noqa: E402


# ---- ORCID ----------------------------------------------------------------
def _orcid_json(n):
    return {"group": [
        {"work-summary": [{
            "put-code": 1000 + i,
            "title": {"title": {"value": f"Work {i}"}},
            "publication-date": {"year": {"value": str(2000 + i)}},
            "external-ids": {"external-id": [
                {"external-id-type": "doi", "external-id-value": f"10.1/{i}"}]},
        }]} for i in range(n)]}


def test_orcid_recogniser_and_id():
    assert ORC.is_orcid("https://orcid.org/0000-0002-1825-0097")
    assert ORC.orcid_id("https://orcid.org/0000-0002-1825-0097") == "0000-0002-1825-0097"
    assert not ORC.is_orcid("https://example.com/0000-0002-1825-0097")


def test_orcid_builds_works_list():
    html, n = ORC.fetch_all_works("0000-0002-1825-0097", lambda u: _orcid_json(6))
    assert n == 6
    assert len(re.findall(r"<li ", html)) == 6
    assert 'id="orcid-1000"' in html and "Work 0" in html
    assert "https://doi.org/10.1/0" in html               # doi → link
    assert "<span class=\"title\">Work 5</span>" in html


def test_orcid_empty_record_is_graceful():
    html, n = ORC.fetch_all_works("0000-0002-9640-3001", lambda u: {"group": []})
    assert n == 0 and "<ol></ol>" in html                 # no rows, no crash


# ---- Semantic Scholar -----------------------------------------------------
def _s2_paged(total, limit=100):
    def _f(url):
        off = int(parse_qs(urlparse(url).query)["offset"][0])
        data = [{"paperId": f"p{off + i}", "title": f"Paper {off + i}",
                 "year": 2020, "url": f"https://s2/p{off + i}",
                 "externalIds": {"DOI": f"10.9/{off + i}"}}
                for i in range(min(limit, total - off))]
        nxt = off + limit if off + limit < total else None
        return {"offset": off, "next": nxt, "data": data}
    return _f


def test_s2_author_id_extraction():
    assert S2.author_id("https://www.semanticscholar.org/author/J-Doe/1741101") == "1741101"
    assert S2.is_s2_author("https://www.semanticscholar.org/author/J-Doe/1741101")
    assert not S2.is_s2_author("https://www.semanticscholar.org/paper/abc")


def test_s2_paginates_all_papers():
    html, n, pages = S2.fetch_all_works(
        "https://www.semanticscholar.org/author/X/42", _s2_paged(250))
    assert n == 250 and pages == 3                        # 100+100+50
    assert len(re.findall(r"<li ", html)) == 250
    assert 'id="s2-p249"' in html
    assert "https://doi.org/10.9/0" in html               # doi link present


def test_router_points_at_the_right_commands():
    v1 = RT.route("https://orcid.org/0000-0002-1825-0097")
    assert v1.rule == "orcid" and "orcid" in v1.action
    v2 = RT.route("https://www.semanticscholar.org/author/X/42")
    assert v2.rule == "semanticscholar" and "semanticscholar" in v2.action


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
