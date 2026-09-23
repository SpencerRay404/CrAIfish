import pytest

from pathcrawl.normalize import host_allowed, locale_allowed, normalize_url, param_is_stripped

STRIP = ["utm_*", "gclid", "fbclid", "li_fat_id", "trk"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # host and scheme are lowercased; path case is kept
        ("HTTPS://WWW.Example.COM/Us/En/Page", "https://www.example.com/Us/En/Page"),
        # fragments are dropped
        ("https://www.example.com/a#section-2", "https://www.example.com/a"),
        # default ports are dropped, others kept
        ("https://www.example.com:443/a", "https://www.example.com/a"),
        ("http://www.example.com:80/a", "http://www.example.com/a"),
        ("https://www.example.com:8443/a", "https://www.example.com:8443/a"),
        # empty path becomes /
        ("https://www.example.com", "https://www.example.com/"),
        # trailing dot on host is dropped
        ("https://www.example.com./a", "https://www.example.com/a"),
        # trailing slash is significant and kept
        ("https://www.example.com/a/", "https://www.example.com/a/"),
        # surrounding whitespace from copy-paste
        ("  https://www.example.com/a \n", "https://www.example.com/a"),
    ],
)
def test_basic_normalization(raw, expected):
    assert normalize_url(raw) == expected


def test_strips_configured_params_and_keeps_others():
    url = "https://www.example.com/a?utm_source=li&utm_medium=paid&id=7&gclid=x&trk=y&li_fat_id=z&fbclid=q"
    assert normalize_url(url, strip_params=STRIP) == "https://www.example.com/a?id=7"


def test_param_stripping_is_case_insensitive():
    assert normalize_url("https://x.com/a?UTM_Source=li&Q=1", strip_params=STRIP) == "https://x.com/a?Q=1"


def test_query_param_order_does_not_create_duplicates():
    a = normalize_url("https://x.com/a?b=2&a=1", strip_params=STRIP)
    b = normalize_url("https://x.com/a?a=1&utm_source=z&b=2", strip_params=STRIP)
    assert a == b == "https://x.com/a?a=1&b=2"


def test_repeated_params_keep_relative_order():
    assert normalize_url("https://x.com/a?t=2&s=0&t=1") == "https://x.com/a?s=0&t=2&t=1"


def test_blank_params_are_kept():
    assert normalize_url("https://x.com/a?flag=") == "https://x.com/a?flag="


def test_all_params_stripped_leaves_no_question_mark():
    assert normalize_url("https://x.com/a?utm_source=li", strip_params=STRIP) == "https://x.com/a"


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        ("b.html", "https://x.com/dir/b.html"),
        ("../up.html", "https://x.com/up.html"),
        ("/root.html", "https://x.com/root.html"),
        ("//other.com/p", "https://other.com/p"),
        ("?q=1", "https://x.com/dir/page.html?q=1"),
        ("#top", "https://x.com/dir/page.html"),
    ],
)
def test_relative_links_resolve_against_base(href, expected):
    assert normalize_url(href, base="https://x.com/dir/page.html") == expected


@pytest.mark.parametrize(
    "href",
    ["mailto:a@b.com", "tel:+15551234", "javascript:void(0)", "data:text/html,hi", "ftp://x.com/f", "", "   ", "http://x.com:notaport/"],
)
def test_non_crawlable_urls_return_none(href):
    assert normalize_url(href, base="https://x.com/") is None


def test_url_without_host_returns_none():
    assert normalize_url("https://") is None


def test_ipv6_host_keeps_brackets():
    assert normalize_url("http://[::1]:8000/a") == "http://[::1]:8000/a"


def test_param_is_stripped_globs():
    assert param_is_stripped("utm_campaign", STRIP)
    assert not param_is_stripped("utmost", STRIP)
    assert not param_is_stripped("id", STRIP)


def test_host_allowed_requires_exact_host():
    allowed = ["www.example.com", "about.example.com"]
    assert host_allowed("https://www.example.com/a", allowed)
    assert host_allowed("https://ABOUT.example.com/a", allowed)
    assert not host_allowed("https://example.com/a", allowed)
    assert not host_allowed("https://shop.example.com/a", allowed)
    assert not host_allowed("https://www.example.com.evil.net/a", allowed)
    assert not host_allowed("not a url", allowed)


def test_locale_filters():
    inc, exc = ["/us/en/"], ["/us/en/careers/"]
    assert locale_allowed("https://x.com/us/en/shipping", inc, exc)
    assert locale_allowed("https://x.com/US/EN/shipping", inc, exc)  # case-insensitive
    assert not locale_allowed("https://x.com/gb/en/shipping", inc, exc)
    assert not locale_allowed("https://x.com/us/en/careers/jobs", inc, exc)
    assert locale_allowed("https://x.com/anything", [], [])
    # filters look at the path only, not the query
    assert not locale_allowed("https://x.com/gb/en/?next=/us/en/", inc, exc)
