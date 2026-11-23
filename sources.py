"""Job source adapters. Each adapter returns a list of normalized job dicts.

To add a source: write one function `def mysource(cfg, warn) -> list[job]`
using make_job(...), then add it to SOURCES at the bottom and to
"sources_enabled" in config.json.
"""
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone


def http_json(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "job-digest/1.0", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def http_post_json(url, payload, headers=None, timeout=20):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"User-Agent": "job-digest/1.0", "Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def to_date(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000, tz=timezone.utc).date()
    s = str(v).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


def strip_html(s):
    s = html.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def make_job(source, sid, title, company, location, remote, posted, url,
             desc="", salary="", closes=None, geo_ok=False):
    return {"source": source, "sid": str(sid), "title": (title or "").strip(),
            "company": (company or "").strip(), "location": (location or "").strip(),
            "remote": bool(remote), "posted": posted, "url": url, "desc": desc or "",
            "salary": salary or "", "closes": closes, "geo_ok": geo_ok}


# ---------------------------------------------------------------- USAJobs
def usajobs(cfg, warn):
    key, email = os.environ.get("USAJOBS_KEY"), os.environ.get("USAJOBS_EMAIL")
    if not key or not email:
        raise RuntimeError("USAJOBS_KEY / USAJOBS_EMAIL not set")
    c, out = cfg["usajobs"], []
    hp = (c.get("hiring_path") or "").strip().lower()  # e.g. "public"; empty = no filter
    headers = {"User-Agent": email, "Authorization-Key": key}
    searches = [{"LocationName": cfg["home"]["city"], "Radius": c["radius_miles"]},
                {"RemoteIndicator": "True"}]
    for kw in c["keywords"]:
        for extra in searches:
            q = {"Keyword": kw, "DatePosted": cfg["max_age_days"], "ResultsPerPage": 50, **extra}
            if hp:
                q["HiringPath"] = hp
            try:
                data = http_json("https://data.usajobs.gov/api/search?" + urllib.parse.urlencode(q), headers)
            except Exception as e:
                warn.append(f"usajobs '{kw}': {e}")
                continue
            for it in data.get("SearchResult", {}).get("SearchResultItems", []):
                d = it.get("MatchedObjectDescriptor", {})
                apply_uri = (d.get("ApplyURI") or [d.get("PositionURI")])[0]
                pay = (d.get("PositionRemuneration") or [{}])[0]
                salary = f"${pay.get('MinimumRange','')}-${pay.get('MaximumRange','')} {pay.get('RateIntervalCode','')}".strip() if pay else ""
                details = (d.get("UserArea") or {}).get("Details") or {}
                paths = [str(x).lower() for x in (details.get("HiringPath") or [])]
                if hp and paths and hp not in paths:
                    continue  # safety net in case the API filter is ignored
                desc = f"{d.get('QualificationSummary','')} {details.get('JobSummary','')}"
                out.append(make_job("usajobs", it.get("MatchedObjectId"), d.get("PositionTitle"),
                                    d.get("OrganizationName"), d.get("PositionLocationDisplay"),
                                    "RemoteIndicator" in extra, to_date(d.get("PublicationStartDate")),
                                    apply_uri, desc, salary, to_date(d.get("ApplicationCloseDate")),
                                    geo_ok="LocationName" in extra))
    return out


# ---------------------------------------------------------------- Adzuna
def adzuna(cfg, warn):
    app_id, app_key = os.environ.get("ADZUNA_ID"), os.environ.get("ADZUNA_KEY")
    if not app_id or not app_key:
        raise RuntimeError("ADZUNA_ID / ADZUNA_KEY not set")
    c, out = cfg["adzuna"], []
    for what in c["queries"]:
        for mode in ("local", "remote"):
            q = {"app_id": app_id, "app_key": app_key, "results_per_page": 50,
                 "max_days_old": cfg["max_age_days"], "sort_by": "date"}
            if mode == "local":
                q.update(what=what, where=cfg["home"]["city"], distance=c["distance_km"])  # distance is km
            else:
                q.update(what=f"{what} remote")
            url = f"https://api.adzuna.com/v1/api/jobs/{c['country']}/search/1?" + urllib.parse.urlencode(q)
            try:
                data = http_json(url)
            except Exception as e:
                warn.append(f"adzuna '{what}' {mode}: {e}")
                continue
            for r in data.get("results", []):
                loc = (r.get("location") or {}).get("display_name", "")
                remote = mode == "remote" or "remote" in (r.get("title", "") + loc).lower()
                sal = ""
                if r.get("salary_min"):
                    sal = f"${int(r['salary_min']):,}-${int(r.get('salary_max') or r['salary_min']):,}"
                out.append(make_job("adzuna", r.get("id"), r.get("title"),
                                    (r.get("company") or {}).get("display_name"), loc, remote,
                                    to_date(r.get("created")), r.get("redirect_url"),
                                    strip_html(r.get("description")), sal,
                                    geo_ok=(mode == "local")))
    return out


# ---------------------------------------------------------------- Remotive (remote-only)
# Remotive asks for only a few calls a day and blocks more than 2 per minute, so calls are spaced out.
# Terms: link back to the Remotive URL and name Remotive as the source; do not republish their jobs.
def remotive(cfg, warn):
    c, out = cfg["remotive"], []
    queries = [{"category": slug} for slug in c.get("categories", [])] + \
              [{"search": s} for s in c.get("searches", [])]
    for i, q in enumerate(queries):
        if i:
            time.sleep(c.get("pause_seconds", 35))
        try:
            data = http_json("https://remotive.com/api/remote-jobs?" + urllib.parse.urlencode(q))
        except Exception as e:
            warn.append(f"remotive {q}: {e}")
            continue
        for j in data.get("jobs", []):
            out.append(make_job("remotive", j.get("id"), j.get("title"), j.get("company_name"),
                                j.get("candidate_required_location", ""), True,
                                to_date(j.get("publication_date")), j.get("url"),
                                strip_html(j.get("description")), j.get("salary", "")))
    return out


# ---------------------------------------------------------------- Greenhouse / Lever (per-company boards)
def _board_error(kind, token, e):
    if isinstance(e, urllib.error.HTTPError) and e.code == 404:
        return f"{kind} board '{token}' not found (check the token in the company's careers-page URL)"
    return f"{kind} '{token}': {e}"


def greenhouse(cfg, warn):
    out = []
    if not cfg.get("greenhouse_boards"):
        raise RuntimeError("no companies listed in greenhouse_boards in config.json")
    boards = cfg.get("greenhouse_boards", [])
    if not boards:
        warn.append("greenhouse: no boards listed in greenhouse_boards (config.json)")
    for token in boards:
        try:
            data = http_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true")
        except Exception as e:
            warn.append(_board_error("greenhouse", token, e))
            continue
        for j in data.get("jobs", []):
            loc = (j.get("location") or {}).get("name", "")
            posted = to_date(j.get("first_published") or j.get("updated_at"))  # updated_at can be later than the real posting date
            out.append(make_job("greenhouse", j.get("id"), j.get("title"),
                                j.get("company_name") or token.replace("-", " ").title(), loc,
                                "remote" in loc.lower(), posted, j.get("absolute_url"),
                                strip_html(j.get("content"))))
    return out


def lever(cfg, warn):
    out = []
    if not cfg.get("lever_boards"):
        raise RuntimeError("no companies listed in lever_boards in config.json")
    boards = cfg.get("lever_boards", [])
    if not boards:
        warn.append("lever: no boards listed in lever_boards (config.json)")
    for co in boards:
        data, err = None, None
        for host in ("api.lever.co", "api.eu.lever.co"):  # EU-hosted companies use the second host
            try:
                data = http_json(f"https://{host}/v0/postings/{co}?mode=json")
                err = None
                break
            except Exception as e:
                err = e
                if not (isinstance(e, urllib.error.HTTPError) and e.code == 404):
                    break
        if data is None:
            warn.append(_board_error("lever", co, err))
            continue
        for j in data:
            cat = j.get("categories") or {}
            locs = cat.get("allLocations") or [cat.get("location", "")]
            loc = "; ".join(x for x in locs if x)
            remote = j.get("workplaceType") == "remote" or "remote" in loc.lower()
            # requirements live in "lists", so include them or scoring misses most skills
            parts = [j.get("descriptionPlain", "")]
            for lst in j.get("lists") or []:
                parts.append(f"{lst.get('text', '')} {strip_html(lst.get('content'))}")
            parts.append(j.get("additionalPlain", ""))
            out.append(make_job("lever", j.get("id"), j.get("text"), co.replace("-", " ").title(),
                                loc, remote, to_date(j.get("createdAt")),
                                j.get("hostedUrl") or j.get("applyUrl"), " ".join(parts)))
    return out


# ---------------------------------------------------------------- Jooble (aggregator)
def jooble(cfg, warn):
    key = os.environ.get("JOOBLE_KEY")
    if not key:
        raise RuntimeError("JOOBLE_KEY not set")
    c, out = cfg["jooble"], []
    for kw in c["keywords"]:
        for mode in ("local", "remote"):
            body = {"keywords": kw if mode == "local" else f"{kw} remote",
                    "location": cfg["home"]["city"] if mode == "local" else "",
                    "radius": str(c["radius_miles"]), "page": "1"}
            try:
                data = http_post_json(f"https://jooble.org/api/{key}", body)
            except Exception as e:
                warn.append(f"jooble '{kw}' {mode}: {e}")
                continue
            for j in data.get("jobs", []):
                loc = j.get("location", "")
                remote = mode == "remote" or "remote" in (j.get("title", "") + " " + loc).lower()
                out.append(make_job("jooble", j.get("id"), j.get("title"), j.get("company"), loc, remote,
                                    to_date(j.get("updated")), j.get("link"), strip_html(j.get("snippet")),
                                    j.get("salary", ""), geo_ok=(mode == "local")))
    return out


# ---------------------------------------------------------------- JSearch via RapidAPI (Google for Jobs data)
def jsearch(cfg, warn):
    key = os.environ.get("RAPIDAPI_KEY")
    if not key:
        raise RuntimeError("RAPIDAPI_KEY not set")
    c, out = cfg["jsearch"], []
    headers = {"X-RapidAPI-Key": key, "X-RapidAPI-Host": "jsearch.p.rapidapi.com"}
    for kw in c["queries"]:
        for mode in ("local", "remote"):
            q = {"page": "1", "num_pages": "1", "date_posted": "month", "country": "us"}
            if mode == "local":
                q.update(query=f"{kw} in {cfg['home']['city']}", radius=c["radius_km"])
            else:
                q.update(query=f"{kw} remote", remote_jobs_only="true")
            try:
                data = http_json("https://jsearch.p.rapidapi.com/search?" + urllib.parse.urlencode(q), headers)
            except Exception as e:
                warn.append(f"jsearch '{kw}' {mode}: {e}")
                continue
            for j in data.get("data", []):
                loc = ", ".join(x for x in (j.get("job_city"), j.get("job_state")) if x) or (j.get("job_country") or "")
                lo, hi = j.get("job_min_salary"), j.get("job_max_salary")
                sal = f"${int(lo):,}-${int(hi):,}" if lo and hi else ""
                out.append(make_job("jsearch", j.get("job_id"), j.get("job_title"), j.get("employer_name"), loc,
                                    bool(j.get("job_is_remote")) or mode == "remote",
                                    to_date(j.get("job_posted_at_datetime_utc")),
                                    j.get("job_apply_link") or j.get("job_google_link"),
                                    j.get("job_description", ""), sal, geo_ok=(mode == "local")))
    return out


# Order matters: earlier sources win when the same job appears in several (direct employer feeds first).
SOURCES = {"greenhouse": greenhouse, "lever": lever, "usajobs": usajobs, "jsearch": jsearch,
           "jooble": jooble, "adzuna": adzuna, "remotive": remotive}
