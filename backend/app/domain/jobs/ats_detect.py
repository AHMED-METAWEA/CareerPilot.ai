"""ATS platform detection, in the precedence order of Appendix C.

The confidences are fixed by the specification, not tuned: a posting fetched
from an ATS's own API *is* on that ATS (1.00); a host pattern is near-certain
(0.95); an HTML fingerprint is good evidence (0.80); a redirect terminus is
weaker still (0.75). Anything else is `unknown`, which is a legitimate answer.
"""

from __future__ import annotations

import re

from app.domain.jobs.urls import url_host
from app.domain.models import ATSDetection, ATSPlatform, DetectionMethod

# (host suffix or regex, platform). Order matters only for readability; the
# match is exact-suffix or regex, so overlaps are not possible in practice.
_HOST_PATTERNS: tuple[tuple[str, ATSPlatform], ...] = (
    ("boards.greenhouse.io", ATSPlatform.GREENHOUSE),
    ("job-boards.greenhouse.io", ATSPlatform.GREENHOUSE),
    ("boards-api.greenhouse.io", ATSPlatform.GREENHOUSE),
    ("greenhouse.io", ATSPlatform.GREENHOUSE),
    ("jobs.lever.co", ATSPlatform.LEVER),
    ("lever.co", ATSPlatform.LEVER),
    ("jobs.ashbyhq.com", ATSPlatform.ASHBY),
    ("ashbyhq.com", ATSPlatform.ASHBY),
    ("apply.workable.com", ATSPlatform.WORKABLE),
    ("workable.com", ATSPlatform.WORKABLE),
    ("jobs.smartrecruiters.com", ATSPlatform.SMARTRECRUITERS),
    ("smartrecruiters.com", ATSPlatform.SMARTRECRUITERS),
    ("recruitee.com", ATSPlatform.RECRUITEE),
    ("myworkdayjobs.com", ATSPlatform.WORKDAY),
    ("myworkdaysite.com", ATSPlatform.WORKDAY),
    ("icims.com", ATSPlatform.ICIMS),
    ("taleo.net", ATSPlatform.TALEO),
    ("breezy.hr", ATSPlatform.BREEZY),
    ("jobs.jobvite.com", ATSPlatform.JOBVITE),
    ("jobvite.com", ATSPlatform.JOBVITE),
    ("teamtailor.com", ATSPlatform.TEAMTAILOR),
    ("personio.de", ATSPlatform.PERSONIO),
    ("jobs.personio.com", ATSPlatform.PERSONIO),
)

_HOST_REGEXES: tuple[tuple[re.Pattern[str], ATSPlatform], ...] = (
    (re.compile(r"^[\w.-]+\.bamboohr\.com$"), ATSPlatform.BAMBOOHR),
    (re.compile(r"^[\w.-]*successfactors\.(com|eu)$"), ATSPlatform.SUCCESSFACTORS),
    (re.compile(r"^[\w.-]+\.oraclecloud\.com$"), ATSPlatform.ORACLE_HCM),
)

# BambooHR and Oracle need a path check as well: the host alone is used for far
# more than recruiting.
_PATH_REQUIRED: dict[ATSPlatform, tuple[str, ...]] = {
    ATSPlatform.BAMBOOHR: ("/careers", "/jobs"),
    ATSPlatform.ORACLE_HCM: ("/hcmui", "/hcmrecruiting"),
}

_HTML_FINGERPRINTS: tuple[tuple[re.Pattern[str], ATSPlatform], ...] = (
    (
        re.compile(r"id=[\"']grnhse_app[\"']|boards\.greenhouse\.io/embed", re.I),
        ATSPlatform.GREENHOUSE,
    ),
    (re.compile(r"jobs\.lever\.co|lever-jobs-embed", re.I), ATSPlatform.LEVER),
    (re.compile(r"jobs\.ashbyhq\.com|_ashby_embed", re.I), ATSPlatform.ASHBY),
    (re.compile(r"apply\.workable\.com|whr_embed|workable\.com/embed", re.I), ATSPlatform.WORKABLE),
    (
        re.compile(r"smartrecruiters\.com/widget|SmartRecruiters\b", re.I),
        ATSPlatform.SMARTRECRUITERS,
    ),
    (re.compile(r"recruitee\.com/embed|recruitee-careers", re.I), ATSPlatform.RECRUITEE),
    (re.compile(r"myworkdayjobs\.com|wd\d+\.myworkday", re.I), ATSPlatform.WORKDAY),
    (re.compile(r"icims\.com/jobs|iCIMS", re.I), ATSPlatform.ICIMS),
    (re.compile(r"teamtailor\.com", re.I), ATSPlatform.TEAMTAILOR),
    (re.compile(r"personio\.(de|com)/(job|xml)", re.I), ATSPlatform.PERSONIO),
)

UNKNOWN = ATSDetection(platform=ATSPlatform.UNKNOWN, confidence=0.0, method=DetectionMethod.NONE)


def detect_by_construction(platform: ATSPlatform) -> ATSDetection:
    """Rule 1: the posting came from that ATS's own API. Nothing beats this."""
    return ATSDetection(platform=platform, confidence=1.00, method=DetectionMethod.CONSTRUCTION)


def detect_from_url(url: str, *, method: DetectionMethod = DetectionMethod.URL) -> ATSDetection:
    """Rules 2 and 4: host pattern, applied to a URL or to a redirect terminus."""
    host = url_host(url)
    if not host:
        return UNKNOWN
    confidence = 0.95 if method is DetectionMethod.URL else 0.75
    path = url.lower()

    for suffix, platform in _HOST_PATTERNS:
        if host == suffix or host.endswith("." + suffix):
            return ATSDetection(platform=platform, confidence=confidence, method=method)

    for pattern, platform in _HOST_REGEXES:
        if pattern.match(host):
            required = _PATH_REQUIRED.get(platform)
            if required and not any(token in path for token in required):
                continue
            return ATSDetection(platform=platform, confidence=confidence, method=method)

    return UNKNOWN


def detect_from_html(html: str) -> ATSDetection:
    """Rule 3: embedded script hosts, generator meta tags and known DOM ids."""
    if not html:
        return UNKNOWN
    for pattern, platform in _HTML_FINGERPRINTS:
        if pattern.search(html):
            return ATSDetection(
                platform=platform, confidence=0.80, method=DetectionMethod.FINGERPRINT
            )
    return UNKNOWN


def detect(
    *,
    constructed: ATSPlatform | None = None,
    url: str | None = None,
    html: str | None = None,
    redirect_terminus: str | None = None,
) -> ATSDetection:
    """Apply Appendix C in order and return the first confident answer."""
    if constructed is not None and constructed is not ATSPlatform.UNKNOWN:
        return detect_by_construction(constructed)
    if url:
        found = detect_from_url(url)
        if found.platform is not ATSPlatform.UNKNOWN:
            return found
    if html:
        found = detect_from_html(html)
        if found.platform is not ATSPlatform.UNKNOWN:
            return found
    if redirect_terminus:
        found = detect_from_url(redirect_terminus, method=DetectionMethod.REDIRECT)
        if found.platform is not ATSPlatform.UNKNOWN:
            return found
    return UNKNOWN
