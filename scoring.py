"""Pre-screen filter and keyword scoring. This is a rules-based pre-screen,
not a full audit: shortlist it, then audit the job description yourself (or with Claude)."""
import hashlib
import re
import urllib.error
import urllib.request
from datetime import date, datetime, timezone


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def job_id(job):
    city = norm((job["location"] or "").split(",")[0])
    return hashlib.sha1(f"{norm(job['company'])}|{norm(job['title'])}|{city}".encode()).hexdigest()[:16]


def _has(text, term):
    return re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", text) is not None


def term_hits(text, terms):
    return {t: w for t, w in terms.items() if _has(text, t)}


def remote_ok(location, cfg):
    loc = (location or "").lower()
    if any(_has(loc, t) for t in cfg["us_tokens"]):
        return True
    if any(_has(loc, t) for t in cfg["blocked_regions"]):
        return False
    return True  # empty / plain "Remote" / unknown: keep, the digest shows the location text


def tier_for(score, cfg):
    t = cfg["tiers"]
    if score >= t["tier1"]:
        return "Tier 1"
    if score >= t["tier2"]:
        return "Tier 2"
    return "Tier 3"


def evaluate(job, cfg, today=None):
    """Return (result_dict, None) if the job passes, else (None, reason)."""
    today = today or date.today()
    if not job["posted"]:
        return None, "no posting date"
    age = (today - job["posted"]).days
    if age > cfg["max_age_days"] or age < -1:
        return None, "outside 14-day window"
    title = job["title"].lower()
    if any(_has(title, x) for x in cfg["title_exclude"]):
        return None, "title too senior / excluded"
    if not any(x in title for x in cfg["title_include"]):
        return None, "title not a target role"
    loc = (job["location"] or "").lower()
    if job["remote"]:
        if not remote_ok(job["location"], cfg):
            return None, "remote but not US-eligible"
    elif not job["geo_ok"] and not any(t in loc for t in cfg["location_allow"]):
        return None, "outside commute area"
    text = f"{job['title']} {job['desc']}".lower()
    if any(d in text for d in cfg["disqualifiers"]):
        return None, "clearance / disqualifier"
    have, gaps = term_hits(text, cfg["have"]), term_hits(text, cfg["gaps"])
    if len(have) < cfg["min_have_hits"]:
        return None, "too little skill overlap"
    hw, gw = sum(have.values()), sum(gaps.values())
    score = hw / (hw + gw)
    yrs = [int(x) for x in re.findall(r"(\d{1,2})\+?\s*(?:-\s*\d{1,2}\s*)?years?", text)]
    need = max(yrs) if yrs else 0
    if need > cfg["max_years_experience"]:
        score -= 0.10 * (need - cfg["max_years_experience"])
    score = max(0.0, round(score, 2))
    if score < cfg["min_score"]:
        return None, f"score {score:.2f} below minimum"
    return {"score": score, "tier": tier_for(score, cfg), "matched": sorted(have),
            "gaps": sorted(gaps), "years_asked": need or None, "age_days": age}, None


CLOSED_PHRASES = ["no longer accepting", "no longer available", "position has been filled", "job is closed",
                  "this job has expired", "posting has closed", "job not found", "page not found",
                  "this position is no longer", "has been removed", "is no longer open"]


def verify_open(job):
    """Return one of: 'Open - live feed', 'Open - page checked', 'Unverified', 'Closed'."""
    today = date.today()
    if job.get("closes") and job["closes"] < today:
        return "Closed"
    if job["source"] in ("greenhouse", "lever", "usajobs"):
        return "Open - live feed"  # listed in the employer/agency feed during this run
    try:
        req = urllib.request.Request(job["url"], headers={"User-Agent": "Mozilla/5.0 (job-digest)"})
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read(200_000).decode("utf-8", "ignore").lower()
        if any(p in body for p in CLOSED_PHRASES):
            return "Closed"
        return "Open - page checked"
    except urllib.error.HTTPError as e:
        return "Closed" if e.code in (404, 410) else "Unverified"
    except Exception:
        return "Unverified"
