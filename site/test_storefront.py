"""Storefront smoke tests for site/index.html and site/config.js.

Stdlib only (pathlib, re, sys) - no pytest or third-party requirements needed.
Discoverable by pytest (test_* functions) and runnable directly:

    python3 site/test_storefront.py
"""

from pathlib import Path
import re
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = REPO_ROOT / "site" / "index.html"
CONFIG_JS = REPO_ROOT / "site" / "config.js"

SAMPLE_FILENAMES = (
    "forge-playbook-sample.pdf",
    "forge-playbook-sample.epub",
    "forge-playbook-sample.html",
)


def _read(path):
    assert path.exists(), f"expected file not found: {path}"
    return path.read_text(encoding="utf-8")


def test_price_is_displayed():
    html = _read(INDEX_HTML)
    assert "$79" in html, "site/index.html must display the $79 price"


def test_no_hardcoded_payment_url_and_config_is_wired():
    html = _read(INDEX_HTML)
    assert "stripe.com" not in html.lower(), (
        "site/index.html must not contain a hardcoded real payment URL "
        "(e.g. a stripe.com link) - checkout URL is operator-supplied config"
    )
    assert "FORGE_CONFIG.paymentLinkUrl" in html, (
        "site/index.html must wire the buy button href from "
        "window.FORGE_CONFIG.paymentLinkUrl, not a hardcoded value"
    )


def test_config_has_todo_operator_placeholder():
    config = _read(CONFIG_JS)
    assert "TODO-OPERATOR" in config, (
        "site/config.js must contain the literal placeholder 'TODO-OPERATOR' "
        "instead of a real payment link"
    )


def test_sample_links_reference_existing_assets():
    html = _read(INDEX_HTML)
    for filename in SAMPLE_FILENAMES:
        relative_href_pattern = re.compile(
            r'href=["\'][^"\']*' + re.escape(filename) + r'["\']'
        )
        assert relative_href_pattern.search(html), (
            f"site/index.html must link to {filename} via a relative href"
        )

        asset_path = REPO_ROOT / "assets" / filename
        assert asset_path.exists(), f"expected sample asset missing: {asset_path}"


ALL_TESTS = (
    test_price_is_displayed,
    test_no_hardcoded_payment_url_and_config_is_wired,
    test_config_has_todo_operator_placeholder,
    test_sample_links_reference_existing_assets,
)


if __name__ == "__main__":
    failures = []
    for test in ALL_TESTS:
        try:
            test()
        except AssertionError as exc:
            failures.append(f"{test.__name__}: {exc}")

    if failures:
        print("FAILED storefront tests:")
        for failure in failures:
            print(f"  - {failure}")
        sys.exit(1)

    print(f"All {len(ALL_TESTS)} storefront tests passed.")
    sys.exit(0)
