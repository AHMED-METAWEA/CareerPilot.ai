"""URL canonicalisation and apply-URL resolution (§11.2 step 7, Appendix D).

Two separate jobs live here and must not be confused:

* `canonicalize_url` produces a *comparison key* — it is what dedup stage 2
  compares. It is lossy by design.
* `resolve_apply_url` picks the URL a human is sent to. It is returned exactly
  as the ATS gave it — never rewritten, shortened, proxied or wrapped (§12.7).
  Parameter stripping belongs to the comparison key alone: a parameter that
  looks like tracking to us may be the posting's identity to the employer, and
  a broken apply link is the most visible defect this product can ship.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.domain.models import ApplyUrlMethod, ResolvedApplyUrl

# Stripped from comparison keys and from stored apply URLs (Appendix D).
TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "gh_src",
        "ref",
        "referer",
        "referrer",
        "fbclid",
        "gclid",
        "msclkid",
        "mc_cid",
        "mc_eid",
        "source",
        "src",
    }
)
"""Appendix D's list. Note what is deliberately *absent*: `gh_jid`.

Greenhouse boards that render on the employer's own domain carry the job id in
`gh_jid`, e.g. `https://databricks.com/careers/open-positions/job?gh_jid=12345`.
Treating it as tracking collapses every posting on such a board to one URL — 870
unrelated jobs merged into a single group before this was caught. Anything that
might carry identity stays."""

# The subset that is *never* an identifier — safe to remove from a URL a human
# will actually open. Everything else in TRACKING_PARAMS is stripped only from
# the comparison key, because `ref`, `source` or `t` may carry identity on some
# board, and a broken apply link is the most visible defect we can ship.
ANALYTICS_PARAMS: frozenset[str] = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "fbclid",
        "gclid",
        "msclkid",
        "mc_cid",
        "mc_eid",
    }
)

_DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}


def _strip_params(url: str, drop: frozenset[str]) -> str:
    parts = urlsplit(url.strip())
    if not parts.query:
        return url.strip()
    kept = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in drop
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))


def strip_tracking_params(url: str) -> str:
    """Remove every tracking parameter in Appendix D. For comparison keys only."""
    return _strip_params(url, TRACKING_PARAMS)


def strip_analytics_params(url: str) -> str:
    """Remove only the parameters that cannot possibly identify a posting.

    Applied to the URL a human opens. Case, fragment and trailing slash are all
    preserved, and any parameter that might carry identity is left alone.
    """
    return _strip_params(url, ANALYTICS_PARAMS)


def canonicalize_url(url: str) -> str:
    """Normalise a URL down to a stable comparison key.

    Lowercases scheme and host, drops `www.`, drops default ports, drops the
    fragment, strips tracking parameters, sorts the remainder, and removes a
    trailing slash from a non-root path.
    """
    raw = url.strip()
    if not raw:
        return ""

    parts = urlsplit(raw)
    scheme = (parts.scheme or "https").lower()

    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    netloc = host
    if parts.port and parts.port != _DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{parts.port}"

    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    kept = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
    )
    return urlunsplit((scheme, netloc, path, urlencode(kept), ""))


def url_host(url: str) -> str:
    """Lowercased hostname with `www.` removed; empty string if unparseable."""
    host = (urlsplit(url.strip()).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def registrable_domain(url_or_host: str) -> str | None:
    """Best-effort registrable domain, used as the strongest company key.

    Deliberately simple: a public-suffix list is a dependency and a data-refresh
    obligation, and the two-label heuristic plus a short compound-suffix table
    covers the hosts a careers page actually resolves to.
    """
    host = url_or_host if "://" not in url_or_host else url_host(url_or_host)
    host = host.strip().lower().rstrip(".")
    if not host or "." not in host:
        return None
    labels = host.split(".")
    compound = {
        "co.uk",
        "org.uk",
        "ac.uk",
        "gov.uk",
        "com.au",
        "com.br",
        "com.eg",
        "com.sa",
        "com.tr",
        "co.jp",
        "co.in",
        "co.za",
        "com.mx",
        "com.sg",
    }
    if len(labels) >= 3 and ".".join(labels[-2:]) in compound:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


# Hosts shared by thousands of employers. A careers or apply URL on one of these
# says nothing about *which* employer a posting belongs to, so the registrable
# domain must never be used as a company key.
ATS_HOSTS: frozenset[str] = frozenset(
    {
        "greenhouse.io",
        "lever.co",
        "ashbyhq.com",
        "workable.com",
        "smartrecruiters.com",
        "recruitee.com",
        "myworkdayjobs.com",
        "myworkdaysite.com",
        "icims.com",
        "taleo.net",
        "breezy.hr",
        "jobvite.com",
        "teamtailor.com",
        "bamboohr.com",
        "personio.de",
        "personio.com",
        "successfactors.com",
        "oraclecloud.com",
        "applytojob.com",
        "jazz.co",
        "jobs.eu",
        "join.com",
        "hire.lever.co",
    }
)


def is_ats_host(url_or_host: str | None) -> bool:
    """True when the value resolves to a shared ATS host rather than an employer."""
    if not url_or_host:
        return False
    domain = registrable_domain(url_or_host)
    return domain in ATS_HOSTS if domain else False


def company_domain(*candidates: str | None) -> str | None:
    """First candidate that yields a genuine employer domain.

    Guards the single most damaging failure in entity resolution: deriving a
    company key from an ATS URL merges every employer on that platform into one
    row, and the merge is hard to notice because the row looks plausible.
    """
    for candidate in candidates:
        if not candidate:
            continue
        domain = registrable_domain(candidate)
        if domain and domain not in ATS_HOSTS:
            return domain
    return None


def resolve_apply_url(
    *,
    ats_native: str | None = None,
    canonical_link: str | None = None,
    json_ld_url: str | None = None,
    redirect_terminus: str | None = None,
    source_url: str,
) -> ResolvedApplyUrl:
    """Pick the apply URL by Appendix D precedence.

    `source_url` is the last resort and is always available, so this function
    always returns a URL — a posting with no apply link is not a posting.
    """
    for value, method in (
        (ats_native, ApplyUrlMethod.ATS_NATIVE),
        (canonical_link, ApplyUrlMethod.CANONICAL_LINK),
        (json_ld_url, ApplyUrlMethod.JSON_LD),
        (redirect_terminus, ApplyUrlMethod.REDIRECT_TERMINUS),
    ):
        if value and value.strip():
            return ResolvedApplyUrl(url=strip_analytics_params(value), method=method)
    return ResolvedApplyUrl(
        url=strip_analytics_params(source_url), method=ApplyUrlMethod.SOURCE_URL
    )
