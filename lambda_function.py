import html
import json
import os
import time
from datetime import date, datetime, timezone
from decimal import Decimal

import scoring
from sources import SOURCES


def load_config():
    with open(os.path.join(os.path.dirname(__file__), "config.json")) as f:
        return json.load(f)


def build_digest(rows, errors, warnings, stats):
    esc = html.escape
    today = date.today().strftime("%b %d, %Y")
    parts = [f"<h2>Job digest - {today}</h2>",
             f"<p>{stats['new']} new matches. Screened {stats['seen']} postings from {stats['sources']} sources "
             f"({stats['passed']} passed the pre-screen).</p>"]
    text = [f"Job digest - {today}", f"{stats['new']} new matches"]
    for tier in ("Tier 1", "Tier 2", "Tier 3"):
        group = [r for r in rows if r["tier"] == tier]
        if not group:
            continue
        parts.append(f"<h3>{tier} ({len(group)})</h3>")
        text.append(f"\n== {tier} ==")
        for r in group:
            loc = r["location"] + (" (remote)" if r["remote"] else "")
            parts.append(
                f"<p><b>{esc(r['title'])}</b> - {esc(r['company'])} - {int(r['score']*100)}% pre-screen<br>"
                f"{esc(loc)} | posted {r['posted']} | {esc(r['verify'])}"
                + (f" | {esc(r['salary'])}" if r.get("salary") else "") +
                f"<br>Matches: {esc(', '.join(r['matched'][:8]))}<br>"
                f"Possible gaps: {esc(', '.join(r['gaps']) or 'none found')}<br>"
                f"<a href=\"{esc(r['url'])}\">Apply link</a> ({esc(r['source'])})</p>")
            text.append(f"{r['title']} - {r['company']} - {int(r['score']*100)}%\n  {loc} | posted {r['posted']} | {r['verify']}\n"
                        f"  Gaps: {', '.join(r['gaps']) or 'none found'}\n  {r['url']}")
    if not rows:
        parts.append("<p>No new matches today.</p>")
    notes = errors + warnings
    if notes:
        parts.append("<hr><p><b>Source issues</b></p><ul>" + "".join(f"<li>{esc(n)}</li>" for n in notes) + "</ul>")
        text.append("\nSource issues:\n" + "\n".join(notes))
    parts.append("<hr><p style='color:#666'>Pre-screen only: audit the full description before applying. "
                 "'Open - live feed' means listed in the employer feed during this run.</p>")
    return "".join(parts), "\n".join(text)


def lambda_handler(event=None, context=None):
    import boto3
    from botocore.exceptions import ClientError

    cfg = load_config()
    table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
    ses = boto3.client("ses")

    raw, errors, warnings = [], [], []
    for name in cfg["sources_enabled"]:
        try:
            raw.extend(SOURCES[name](cfg, warnings))
        except Exception as e:  # one bad source never kills the run
            errors.append(f"{name}: {e}")

    # de-duplicate across sources (earlier sources win)
    unique = {}
    for j in raw:
        unique.setdefault(scoring.job_id(j), j)

    passed, rows, budget = 0, [], cfg["verify_budget"]
    for jid, j in unique.items():
        res, _reason = scoring.evaluate(j, cfg)
        if not res:
            continue
        passed += 1
        item = {"job_id": jid, "source": j["source"], "title": j["title"], "company": j["company"],
                "location": j["location"], "remote": j["remote"], "posted": j["posted"].isoformat(),
                "url": j["url"], "score": Decimal(str(res["score"])), "tier": res["tier"],
                "first_seen": datetime.now(timezone.utc).isoformat(), "status": "New",
                "ttl": int(time.time()) + 60 * 86400}
        try:
            table.put_item(Item=item, ConditionExpression="attribute_not_exists(job_id)")
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                continue  # already sent in an earlier digest
            raise
        verify = scoring.verify_open(j) if budget > 0 else "Unverified"
        if verify not in ("Open - live feed",) and j["source"] not in ("greenhouse", "lever", "usajobs"):
            budget -= 1
        table.update_item(Key={"job_id": jid}, UpdateExpression="SET verify = :v",
                          ExpressionAttributeValues={":v": verify})
        if verify == "Closed":
            continue
        rows.append({**res, **{k: j[k] for k in ("title", "company", "location", "remote", "url", "source", "salary")},
                     "posted": j["posted"].isoformat(), "verify": verify})

    rows.sort(key=lambda r: r["score"], reverse=True)
    rows = rows[: cfg["max_digest_jobs"]]
    stats = {"new": len(rows), "seen": len(unique), "passed": passed, "sources": len(cfg["sources_enabled"])}
    body_html, body_text = build_digest(rows, errors, warnings, stats)
    ses.send_email(Source=os.environ["SES_FROM"], Destination={"ToAddresses": [os.environ["SES_TO"]]},
                   Message={"Subject": {"Data": f"Job digest: {len(rows)} new matches ({date.today():%b %d})"},
                            "Body": {"Html": {"Data": body_html}, "Text": {"Data": body_text}}})
    return {"new": len(rows), **stats, "errors": errors, "warnings": warnings}
