from signaldesk.richtext import has_link, linkify


def test_has_link_detects_urls() -> None:
    assert has_link("open https://example.com now")
    assert has_link("http://a.co/x")
    assert not has_link("no links here at all")


def test_linkify_wraps_urls_in_anchor() -> None:
    out = linkify("see https://example.com/path now", "#00915A")
    assert '<a href="https://example.com/path"' in out
    assert "color:#00915A" in out
    assert out.startswith("see ")
    assert out.endswith(" now")


def test_linkify_escapes_surrounding_html() -> None:
    out = linkify("a < b & c > d", "#000000")
    assert "&lt;" in out
    assert "&amp;" in out
    assert "&gt;" in out
    # No anchors and no raw markup leaked through.
    assert "<a" not in out


def test_linkify_excludes_trailing_punctuation_from_href() -> None:
    out = linkify("go to https://example.com/page.", "#000000")
    assert 'href="https://example.com/page"' in out
    # The trailing period stays as text, outside the link.
    assert out.rstrip().endswith(".")
    assert "page.</a>" not in out


def test_linkify_escapes_quotes_in_href() -> None:
    out = linkify('bad https://example.com/"onmouseover=x', "#000000")
    assert '"onmouseover' not in out.split("</a>")[0]
