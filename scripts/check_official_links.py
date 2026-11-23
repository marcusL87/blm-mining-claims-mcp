"""Check that every link in the official pages tool still answers.

Not part of the tests (they never go online). Run it before a demo or
when re-checking the table:

    uv run python scripts/check_official_links.py

It requests each distinct URL in official_pages.py once (GET, following
redirects, a generic user agent, one at a time) and prints the status. It
exits with 1 if any link does not answer 200. A failure means: open the page
by hand, and if it moved or changed, update the table and LAST_CHECKED.

Certificates are always checked. When the truststore package is installed,
the script checks them against the computer's own certificate store (as curl
and browsers do), so it works behind antivirus HTTPS scanning such as Avast;
otherwise it uses Python's default certificates.
"""

from __future__ import annotations

import ssl
import sys

import httpx

from mining_claims_mcp.official_pages import OFFICE_CONTACT, TOPICS

TIMEOUT_SECONDS = 30
HEADERS = {"User-Agent": "mining-claims-mcp link check"}


def ssl_context() -> ssl.SSLContext | bool:
    """The computer's certificate store if truststore is installed, else the
    default (True). Never turns certificate checking off."""
    try:
        import truststore
    except ImportError:
        return True
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def all_urls() -> list[str]:
    """Every distinct URL in the table, in table order."""
    urls = [url for _q, pages, _s in TOPICS.values() for _t, url in pages]
    urls.append(OFFICE_CONTACT["url"])
    return list(dict.fromkeys(urls))


def main() -> int:
    failures = 0
    with httpx.Client(
        headers=HEADERS,
        timeout=TIMEOUT_SECONDS,
        follow_redirects=True,
        verify=ssl_context(),
    ) as client:
        for url in all_urls():
            try:
                status = str(client.get(url).status_code)
            except httpx.HTTPError as exc:
                status = f"failed ({type(exc).__name__})"
            if status != "200":
                failures += 1
            print(f"{status:>6}  {url}")
    print(f"\n{len(all_urls()) - failures} of {len(all_urls())} links answered 200.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
