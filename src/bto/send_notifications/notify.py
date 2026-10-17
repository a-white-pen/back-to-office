"""Format and send collection and cleaning summaries and failure alerts.

Sending is fail-soft: missing SMTP configuration or a send failure is logged
and returns ``False`` without undoing the work being reported. ``True`` means
the message was handed to the SMTP server, not that a recipient read it.
Callers decide which conditions warrant an alert; this module does not query
application state. It also provides the systemd fallback for failures that
ended without a delivered Python alert.
"""

import argparse
import json
import logging
import os
import smtplib
import ssl
import sys
from datetime import timedelta
from email.message import EmailMessage

log = logging.getLogger(__name__)

ALERT_SUBJECT = "[back-to-office] Fetch Job Listings Error"
CLEAN_ALERT_SUBJECT = "[back-to-office] Clean Job Listings Error"
SUMMARY_SUBJECT = "[back-to-office] Fetch Job Listings Summary"
CLEAN_SUMMARY_SUBJECT = "[back-to-office] Clean Job Listings Summary"
SUMMARY_UNAVAILABLE_SUBJECT = "[back-to-office] Fetch Job Listings Summary Unavailable"


def send_email(settings, subject, body, html=None):
    """Send one email — plain text, with an optional HTML alternative.
    Returns True when the message was handed to the SMTP server, False
    otherwise. Never raises; failures are logged.

    Port 465 is implicit TLS; anything else is STARTTLS. The From address is
    SMTP_USERNAME.
    """
    host = settings.get("smtp_host")
    username = settings.get("smtp_username")
    password = settings.get("smtp_password")
    to = settings.get("notify_email_to")
    if not (host and username and password and to):
        log.warning("email not configured — NOT sent: %s", subject)
        return False

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = username
    message["To"] = to
    message.set_content(body)
    if html:
        message.add_alternative(html, subtype="html")

    port = settings.get("smtp_port") or 587
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(),
                                  timeout=30) as server:
                server.login(username, password)
                server.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=30) as server:
                server.starttls(context=ssl.create_default_context())
                server.login(username, password)
                server.send_message(message)
        log.info("email sent: %s", subject)
        return True
    except Exception as error:
        log.error("email FAILED (%s): %s — not delivered", subject, error)
        return False


def send_problem_alert(settings, *, run_id, board, market, cause, detail,
                       counters=None, subject=ALERT_SUBJECT):
    """One problem alert for one run: board, market, cause, counters, what
    landed and what remains. Returns send_email()'s outcome. `subject`
    defaults to the collection alert; cleaning passes its own."""
    lines = [
        f"Run:     {run_id}",
        f"Board:   {board}",
        f"Market:  {market}",
        f"Cause:   {cause}",
        "",
        detail.rstrip(),
    ]
    counters = counters or {}
    if counters:
        lines += ["", "Counters:"]
        for key in ("terms_swept", "terms_succeeded", "unique_seen", "new_jobs",
                    "changed_jobs", "jds_intended", "jds_fetched",
                    "fetch_failures", "backlog_remaining"):
            if counters.get(key) is not None:
                lines.append(f"  {key:<18} {counters[key]}")
    return send_email(settings, subject, "\n".join(lines) + "\n")


def send_clean_summary(settings, *, run_id, stage, inserted, canonicalized,
                       version, elapsed=None, standardization_elapsed=None,
                       canonicalization_elapsed=None, canonicalization_mode=None,
                       fallback_reason=None):
    """The heartbeat for ONE cleaning run that completed successfully.

    Its arrival is the operator's evidence that cleaning ran to the end; its
    absence is the signal to look. It reports only what the completed run
    already knows, so it never queries anything — including whether
    canonicalization extended the previous mapping or rebuilt the whole
    history, and why an extension was refused.

    Returns send_email()'s outcome; problem alerts stay separate, so a run
    with quarantined rows sends both.
    """
    whole = stage == "all"
    lines = [
        f"Run:     {run_id}",
        f"Stage:   {stage}" + (" (full production build from complete Bronze)"
                               if whole else " (incremental maintenance)"),
        "Result:  completed successfully",
        "",
        "Standardization:",
        f"  {'observations standardized' if whole else 'new observations':<26}"
        f" {inserted}",
        "",
        "Canonicalization:",
        f"  {'mapping':<26} "
        + ("republished" if canonicalized else
           "unchanged — already current, nothing new to canonicalize"),
    ]
    if canonicalized and canonicalization_mode:
        mode = ("incremental" if canonicalization_mode == "incremental"
                else "full rebuild")
        if fallback_reason:
            mode += f" (incremental refused: {fallback_reason})"
        lines.append(f"  {'mode':<26} {mode}")
    lines.append(f"  {'standardized version':<26} {version}")
    # each stage and the whole run; a stage that did not run has no line, and
    # the total also covers the final freshness proof and reconciliation
    durations = [(label, value) for label, value in (
        ("standardization", standardization_elapsed),
        ("canonicalization", canonicalization_elapsed),
        ("total", elapsed)) if value is not None]
    if durations:
        lines += ["", "Duration:"] + [
            f"  {label:<26} {timedelta(seconds=int(value.total_seconds()))}"
            for label, value in durations]
    return send_email(settings, CLEAN_SUMMARY_SUBJECT, "\n".join(lines) + "\n")


def send_summary_unavailable(settings, night_label, error):
    """Sent in place of the fetch summary when Databricks does not return the
    run results it is built from — so an outage produces a message rather than
    silence. Any run that failed has already had its own alert attempt."""
    body = (f"The summary for the {night_label} could not be built.\n\n"
            f"Databricks did not return the run results:\n  {error}\n\n"
            "Any run that failed has already had its own alert attempt.\n")
    return send_email(settings, SUMMARY_UNAVAILABLE_SUBJECT, body)


def build_summary_body(rows, missing_pairs, night_label):
    """The summary of one fetch execution, as (plain_text, html).

    `rows` are dicts (one per run) with the scrape_runs fields; `missing_pairs`
    are pairs the execution intended that left no run row — each is a problem
    worth a line of its own. Changed shows a dash for Indeed and LinkedIn:
    their JDs arrive with the search result, so there is nothing to
    re-download and nothing to compare. `night_label` says which execution
    this is (scheduled or manual, and when).
    """
    STATUS_COLORS = {"ok": "#1e7e34", "partial": "#b35c00",
                     "failed": "#b02a37", "running": "#0b5ed7",
                     "no run": "#b02a37"}

    def cell(value):
        return "-" if value is None else str(value)

    def short_reason(text):
        """First ~200 readable characters of an error — enough to diagnose,
        without dumping a raw stack/exception into the email body."""
        collapsed = " ".join(str(text).split())
        return collapsed[:200] + ("…" if len(collapsed) > 200 else "")

    table = []          # (name, status, seen, new, changed, fetched,
                        #  failed, deferred, runtime)
    problems = []       # (name, status, reason-or-None)
    for row in rows:
        name = f"{row['board']}_{row['market']}"
        status = row["status"] or "?"
        table.append((name, status, cell(row.get("unique_seen")),
                      cell(row.get("new_jobs")), cell(row.get("changed_jobs")),
                      cell(row.get("jds_fetched")),
                      cell(row.get("fetch_failures")),
                      cell(row.get("backlog_remaining")),
                      row.get("runtime") or "-"))
        if status in ("failed", "partial", "running") or row.get("error_reason"):
            problems.append((name, status,
                             short_reason(row["error_reason"])
                             if row.get("error_reason") else None))
    for board, market in missing_pairs:
        name = f"{board}_{market}"
        table.append((name, "no run", "-", "-", "-", "-", "-", "-", "-"))
        problems.append((name, "no run",
                         "this execution intended the run but no scrape_runs "
                         "row exists for it"))

    totals = (f"seen {sum(r.get('unique_seen') or 0 for r in rows)} · "
              f"new (mcf/seek) {sum(r.get('new_jobs') or 0 for r in rows)} · "
              f"changed (mcf/seek) "
              f"{sum(r.get('changed_jobs') or 0 for r in rows)} · "
              f"payloads fetched {sum(r.get('jds_fetched') or 0 for r in rows)}")

    # ── plain-text fallback ──
    header = (f"{'RUN':<16} {'STATUS':<9} {'SEEN':>6} {'NEW':>6} {'CHANGED':>8} "
              f"{'FETCHED':>8} {'FAILED':>7} {'DEFERRED':>9} {'RUNTIME':>8}")
    lines = [f"Fetch Job Listings — {night_label}", "", header,
             "-" * len(header)]
    for name, status, *cells in table:
        shown = (f"[{status.upper()}]"
                 if status in ("failed", "running", "no run") else status)
        lines.append(f"{name:<16} {shown:<9} {cells[0]:>6} {cells[1]:>6} "
                     f"{cells[2]:>8} {cells[3]:>8} {cells[4]:>7} {cells[5]:>9} "
                     f"{cells[6]:>8}")
    lines += ["", f"Totals: {totals}"]
    if problems:
        lines += ["", "Needs attention:"]
        for name, status, reason in problems:
            lines.append(f"  {name}: {status}"
                         + (f" — {reason}" if reason else ""))
    else:
        lines += ["", "Every run in this execution completed cleanly."]
    lines += ["", "Full detail: journalctl -u fetch-job-listings · "
                  "bronze.scrape_runs"]
    plain = "\n".join(lines) + "\n"

    # ── HTML ──
    from html import escape

    def td(value, *, align="right", color=None, bold=False):
        style = (f"padding:4px 10px;border-bottom:1px solid #e3e3e3;"
                 f"text-align:{align};")
        if color:
            style += f"color:{color};"
        if bold:
            style += "font-weight:600;"
        return f'<td style="{style}">{escape(str(value))}</td>'

    heads = ("Run", "Status", "Seen", "New", "Changed", "Fetched", "Failed",
             "Deferred", "Runtime")
    head_cells = "".join(
        f'<th style="padding:4px 10px;border-bottom:2px solid #444;'
        f'text-align:{"left" if h in ("Run", "Status") else "right"};">'
        f"{h}</th>" for h in heads)
    body_rows = []
    for name, status, *cells in table:
        color = STATUS_COLORS.get(status, "#333")
        attention = status not in ("ok",)
        body_rows.append(
            "<tr>" + td(name, align="left", bold=attention)
            + td(status, align="left", color=color, bold=attention)
            + "".join(td(value) for value in cells) + "</tr>")
    problem_html = ""
    if problems:
        items = "".join(
            f'<li><b>{escape(name)}</b>: {escape(status)}'
            + (f" — {escape(reason)}" if reason else "") + "</li>"
            for name, status, reason in problems)
        problem_html = (f'<p style="margin:14px 0 4px;font-weight:600;">'
                        f"Needs attention</p><ul style='margin:2px 0 0;'>"
                        f"{items}</ul>")
    else:
        problem_html = ('<p style="margin:14px 0 0;color:#1e7e34;">'
                        "Every run in this execution completed cleanly.</p>")
    html = f"""\
<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;
            font-size:14px;color:#222;">
  <p style="margin:0 0 10px;font-weight:600;">Fetch Job Listings —
     {escape(night_label)}</p>
  <table style="border-collapse:collapse;font-variant-numeric:tabular-nums;">
    <tr>{head_cells}</tr>
    {"".join(body_rows)}
  </table>
  <p style="margin:10px 0 0;color:#555;">Totals: {escape(totals)}</p>
  {problem_html}
  <p style="margin:16px 0 0;font-size:12px;color:#888;">
    Full detail: <code>journalctl -u fetch-job-listings</code> ·
    <code>bronze.scrape_runs</code></p>
</div>
"""
    return plain, html


def send_summary(settings, rows, missing_pairs, night_label):
    plain, html = build_summary_body(rows, missing_pairs, night_label)
    return send_email(settings, SUMMARY_SUBJECT, plain, html=html)


def render_template(name, context, *, html_output=False):
    """Expand the approved template's small IF/REPEAT vocabulary, escaping values."""
    import re
    from html import escape
    from pathlib import Path

    source = (Path(__file__).parent / "templates" / name).read_text()
    if html_output:
        source = re.sub(
            r"<!--(?!\s*(?:IF |END IF|REPEAT|END REPEAT|\[if|\[endif))[\s\S]*?-->", "", source
        )
        source = re.sub(
            r"<!--\s*(IF [^<>]+?|END IF|REPEAT[^<>]*?|END REPEAT)\s*-->",
            lambda m: "[" + m[1] + "]",
            source,
        )
    marker = re.compile(r"\[(IF [^\]]+|END IF|REPEAT[^\]]*|END REPEAT)\]")

    def parse(position=0, closing=None):
        nodes = []
        while True:
            match = marker.search(source, position)
            if not match:
                if closing:
                    raise ValueError("Unclosed email template block")
                nodes.append(source[position:])
                return nodes, len(source)
            nodes.append(source[position : match.start()])
            label = match[1]
            position = match.end()
            if label.startswith("END "):
                if label != closing:
                    raise ValueError("Mismatched email template block")
                return nodes, position
            children, position = parse(
                position, "END IF" if label.startswith("IF ") else "END REPEAT"
            )
            nodes.append((label, children))

    def repeat_key(label):
        if "failed or partial" in label:
            return "problem_runs"
        if "failed step" in label:
            return "failed_steps"
        if "routine note" in label:
            return "routine_notes"
        if "lack" in label:
            return "lacks"
        return "collection_runs"

    def render(nodes, values):
        result = []
        for node in nodes:
            if isinstance(node, str):

                def value(match):
                    answer = values.get(match[1], "—")
                    answer = "—" if answer is None else str(answer)
                    return escape(answer, quote=True) if html_output else answer

                result.append(re.sub(r"\{\{([A-Z0-9_]+)\}\}", value, node))
            else:
                label, children = node
                if label.startswith("IF "):
                    if values.get("conditions", {}).get(label[3:], bool(values.get(label[3:]))):
                        result.append(render(children, values))
                else:
                    result.extend(
                        render(children, {**values, **row})
                        for row in values.get(repeat_key(label), [])
                    )
        return "".join(result)

    return render(parse()[0], context)


def render_morning_email(context):
    subject = (
        f"BTO · {context['DATE']} · Top picks {context['TOP_PICKS_TOTAL']} · In lane {context['IN_LANE_TOTAL']} · Not sorted {context['NOT_SORTED_TOTAL']}"
        if context["STATUS_LINE"] == "All done"
        else f"BTO · {context['DATE']} · NOT FINISHED · stopped during {context['STEP']}"
    )
    plain = render_template("morning_email.txt", context).split("\n\n", 1)[1]
    return subject, plain, render_template("morning_email.html", context, html_output=True)


def collection_problem(row):
    """Adapter-specific coverage loss; payload loss alone remains a routine note."""
    if row.get("status") in {"failed", "running", "missing"}:
        return True
    if row.get("status") != "partial":
        return False
    swept = row.get("terms_swept")
    succeeded = row.get("terms_succeeded")
    if swept is not None and succeeded is not None and succeeded < swept:
        return True
    return bool(
        row.get("board") in {"mcf", "jobstreet", "jobsdb", "seek", "indeed", "linkedin"}
        and row.get("full_sweep") is False
    )


def collection_context(rows, finished, at):
    from bto.filter_and_bucket_job_listings import SINGAPORE

    problems = [r for r in rows if collection_problem(r)]
    failed = sum(r["status"] in {"failed", "running", "missing"} for r in problems)
    partial = len(problems) - failed

    def runs(n):
        return f"{n} run" + ("s" if n != 1 else "") if n else ""

    def cell(value):
        return f"{value:,}" if isinstance(value, int) else "—" if value is None else str(value)

    summary = ", ".join(
        v
        for v in [
            runs(failed) + " failed" if failed else "",
            runs(partial) + " partial" if partial else "",
        ]
    )
    context = {
        "DATE": at.astimezone(SINGAPORE).strftime("%-d %b"),
        "PROBLEM_SUMMARY": summary,
        "FAILED_RUNS": runs(failed),
        "PARTIAL_RUNS": runs(partial),
        "PROBLEM_RUN_LIST": "; ".join(
            r["board"].title() + " " + r["market"].upper() for r in problems
        ),
        "OK_RUN_LIST": "; ".join(
            r["board"].title() + " " + r["market"].upper() for r in rows if r["status"] == "ok"
        ),
        "conditions": {
            "COLLECTION FINISHED": finished,
            "COLLECTION STOPPED": not finished,
            "BOTH": bool(failed and partial),
            "FAILED_RUNS AND PARTIAL_RUNS": bool(failed and partial),
        },
        "problem_runs": [],
    }
    for row in problems:
        context["problem_runs"].append(
            {
                "BOARD": row["board"].title(),
                "MARKET": row["market"].upper(),
                "STATUS": row["status"],
                "STATUS_COLOUR": "#9a6700" if row["status"] == "partial" else "#cf222e",
                "REASON": row.get("error_reason") or "Collection did not finish normally",
                "TERMS_DONE": cell(row.get("terms_succeeded")),
                "TERMS_PLANNED": cell(row.get("terms_planned")),
                "JOB_LISTINGS_SEEN": cell(row.get("unique_seen")),
                "JDS_FETCHED": cell(row.get("jds_fetched")),
            }
        )
    return context


def render_collection_email(context):
    return (
        f"BTO · {context['DATE']} · collection: {context['PROBLEM_SUMMARY']}",
        render_template("collection_failed.txt", context).split("\n\n", 1)[1],
        render_template("collection_failed.html", context, html_output=True),
    )


def read_collection(dbx, config):
    import json
    from pathlib import Path

    from bto.storage.databricks.connection import table_name

    handoff = json.loads(Path(config["run_handoff_path"]).read_text())
    names = [
        "run_id",
        "board",
        "market",
        "started_at",
        "finished_at",
        "status",
        "terms_swept",
        "terms_succeeded",
        "unique_seen",
        "new_jobs",
        "changed_jobs",
        "jds_fetched",
        "fetch_failures",
        "error_reason",
        "full_sweep",
    ]
    projection = ",".join("'" + n + "'," + n for n in names)
    predicate = (
        "array_contains(from_json(:run_ids,'array<string>'),run_id)"
        if handoff.get("state") == "finished"
        else "run_trigger=:trigger AND started_at>=CAST(:started AS TIMESTAMP) AND array_contains(from_json(:pairs,'array<string>'),concat(board,'_',market))"
    )
    params = (
        {"run_ids": json.dumps(handoff.get("run_ids", []))}
        if handoff.get("state") == "finished"
        else {
            "trigger": handoff.get("trigger", "scheduled"),
            "started": handoff["started_at"],
            "pairs": json.dumps(handoff.get("pairs", [])),
        }
    )
    rows = [
        json.loads(r[0])
        for r in dbx.query(
            f"SELECT to_json(named_struct({projection})) FROM {table_name(config['databricks_catalog'], 'scrape_runs')} WHERE {predicate}",
            parameters=params,
        )
    ]
    observed = {r["board"] + "_" + r["market"] for r in rows}
    rows.extend(
        {
            "board": pair.split("_", 1)[0],
            "market": pair.split("_", 1)[1],
            "status": "missing",
            "error_reason": "Intended run has no collection record",
        }
        for pair in handoff.get("pairs", [])
        if pair not in observed
    )
    # Terms planned are the actual private configured sweep, never JDs/results inferred as terms.
    for row in rows:
        try:
            from bto.fetch_job_listings import run as collection

            if row["board"] == "indeed":
                row["terms_planned"] = len(collection.indeed_fetch.adapter(row["market"]).queries)
            elif row["board"] == "linkedin":
                row["terms_planned"] = len(collection.linkedin_fetch.adapter(row["market"]).terms)
            else:
                row["terms_planned"] = len(
                    collection._card_adapter(row["board"], row["market"])[0].terms
                )
        except (ImportError, AttributeError, ValueError):
            row["terms_planned"] = None
    return handoff, rows


def bucket_validation_report(conn, *, first_id=None):
    from bto.filter_and_bucket_job_listings import BUCKETS, HISTORY_REASON, PROCESSING_START
    from bto.storage.postgres.job_sorting import read_rows

    predicate = "j.wording_first_seen_at >= %s"
    params = [PROCESSING_START]
    if first_id is not None:
        predicate += " AND j.id>%s"
        params.append(first_id)
    counts = read_rows(
        conn,
        "SELECT upper(market) market,coalesce(bucket,'not_sorted') bucket,(repeat_of_id IS NOT NULL) suppressed,count(*) n FROM job_sorting.job_listings j WHERE "
        + predicate
        + " GROUP BY 1,2,3",
        params,
    )
    totals = {
        market: sum(r["n"] for r in counts if r["market"] == market)
        for market in ("SG", "HK", "TH")
    }
    totals["Total"] = sum(totals.values())

    def entry(bucket, suppressed=False):
        numbers = {
            m: sum(
                r["n"]
                for r in counts
                if r["market"] == m
                and (
                    r["suppressed"] if suppressed else not r["suppressed"] and r["bucket"] == bucket
                )
            )
            for m in ("SG", "HK", "TH")
        }
        numbers["Total"] = sum(numbers.values())
        return {
            "bucket": bucket,
            "counts": numbers,
            "percentages": {
                m: round(100 * n / totals[m], 2) if totals[m] else 0 for m, n in numbers.items()
            },
        }

    buckets = [entry(b) for b in BUCKETS]
    scope = buckets[:3]
    combined = {m: sum(r["counts"][m] for r in scope) for m in totals}
    historical = conn.execute(
        "SELECT count(*) FROM job_sorting.job_listings WHERE reason=%s", (HISTORY_REASON,)
    ).fetchone()[0]
    reasons = read_rows(
        conn,
        "SELECT reason,count(*) n FROM job_sorting.job_listings j WHERE "
        + predicate
        + " AND coalesce(bucket,'not_sorted')='not_sorted' AND repeat_of_id IS NULL GROUP BY reason",
        params,
    )
    return {
        "buckets": buckets,
        "in_scope": scope,
        "combined": {
            "counts": combined,
            "percentages": {
                m: round(100 * n / totals[m], 2) if totals[m] else 0 for m, n in combined.items()
            },
        },
        "suppressed": entry("suppressed", True),
        "totals": totals,
        "not_sorted_reasons": reasons,
        "historical_not_sorted": historical,
    }


def build_morning_context(
    conn, report, budget, preferences, *, config=None, dbx=None, collection_rows=None
):
    from collections import Counter
    from datetime import datetime, timezone
    from decimal import Decimal

    from bto.filter_and_bucket_job_listings import (
        DEFERRED_REASONS,
        PROCESSING_START,
        SINGAPORE,
        night,
    )
    from bto.storage.postgres.job_sorting import JOIN_RESULTS, cost_totals, read_rows, stream_rows

    started = datetime.fromisoformat(report["started_at"])
    ended = datetime.fromisoformat(
        report.get("finished_at") or datetime.now(timezone.utc).isoformat()
    )
    validation = bucket_validation_report(conn, first_id=report["first_id"])
    context = {
        "DATE": night(ended).strftime("%-d %b"),
        "conditions": {},
        "failed_steps": [],
        "routine_notes": [],
        "collection_runs": [],
        "lacks": [],
    }

    def number(value):
        return f"{value:,}"

    def dollars(value):
        return f"USD {Decimal(value):.4f}"

    def clock(value):
        return value.astimezone(SINGAPORE).strftime("%-I:%M %p")

    def timing(step, begin=None, end=None, status="—"):
        context.update(
            {
                step + "_START": clock(begin) if begin else "—",
                step + "_END": clock(end) if end else "—",
                step + "_DURATION": str(timedelta(seconds=int((end - begin).total_seconds())))
                if begin and end
                else "—",
                step + "_STATUS": status,
                step + "_STATUS_COLOUR": {"ok": "#1f883d", "failed": "#cf222e", "partial": "#9a6700"}.get(
                    status, "#57606a"
                ),
                step + "_COST": "USD 0.0000",
                step + "_CACHE": "—",
            }
        )

    prefix = {
        "tech_not_for_me": "OUT_OF_SCOPE_TECH_NOT_FOR_ME",
        "non_tech": "OUT_OF_SCOPE_NON_TECH",
        "title_filter": "OUT_OF_SCOPE_TITLE_FILTER",
    }
    for row in validation["buckets"] + [validation["suppressed"]]:
        label = prefix.get(row["bucket"], row["bucket"].upper())
        for market, value in row["counts"].items():
            context[label + "_" + market.upper()] = number(value)
    for market, value in validation["totals"].items():
        context["ALL_FIRST_SEEN_" + market.upper()] = number(value)
    cohort = read_rows(
        conn,
        "WITH good_companies AS MATERIALIZED(SELECT DISTINCT CASE WHEN is_recruitment_firm AND board='mcf' AND hiring_company_name IS NOT NULL THEN hiring_company_name ELSE advertiser_name END company FROM job_sorting.job_listings WHERE wording_first_seen_at>=%s AND bucket IN ('top_picks','in_lane')) SELECT upper(j.market) market,count(*) n FROM job_sorting.job_listings j WHERE j.id>%s AND j.wording_first_seen_at>=%s AND j.bucket='less_interested' AND j.repeat_of_id IS NULL AND EXISTS(SELECT 1 FROM good_companies good WHERE good.company=(CASE WHEN j.is_recruitment_firm AND j.board='mcf' AND j.hiring_company_name IS NOT NULL THEN j.hiring_company_name ELSE j.advertiser_name END)) GROUP BY 1",
        (PROCESSING_START, report["first_id"], PROCESSING_START),
    )
    for market in ("SG", "HK", "TH", "TOTAL"):
        inside = sum(r["n"] for r in cohort if market == "TOTAL" or r["market"] == market)
        context["LESS_INTERESTED_IN_QUEUE_" + market] = number(inside)
        context["LESS_INTERESTED_NOT_IN_QUEUE_" + market] = number(
            int(context["LESS_INTERESTED_" + market].replace(",", "")) - inside
        )
        context["YOUR_QUEUE_" + market] = number(
            inside
            + sum(
                int(context[b + "_" + market].replace(",", ""))
                for b in ("TOP_PICKS", "IN_LANE", "NOT_SORTED")
            )
        )
    unread = read_rows(
        conn,
        "SELECT CASE WHEN triage_input_hash IS NULL THEN 'triage' ELSE 'extraction' END step,reason,count(*) n FROM job_sorting.job_listings WHERE wording_first_seen_at>=%s AND (bucket IS NULL OR reason=ANY(%s)) GROUP BY 1,2",
        (PROCESSING_START, list(DEFERRED_REASONS)),
    )
    stopped = report.get("status") != "complete" or bool(unread)
    context.update(
        STATUS_LINE="Not finished" if stopped else "All done",
        STEP=report.get("failed_step") or (unread[0]["step"] if unread else "rules and pages"),
        UNREAD_LISTINGS=number(sum(r["n"] for r in unread)),
        PULLED=number(validation["totals"]["Total"]),
        REPEATS_SUPPRESSED=context["SUPPRESSED_TOTAL"],
    )
    for row in unread:
        context["failed_steps"].append(
            {
                "STEP": row["step"].title(),
                "STATUS": "not finished",
                "ERROR": row["reason"] or "Interrupted before reading",
                "MISSING_FROM_REVIEW": number(row["n"]),
                "STATUS_COLOUR": "#cf222e",
            }
        )
    if report.get("error"):
        context["failed_steps"].append(
            {
                "STEP": context["STEP"],
                "STATUS": "failed",
                "ERROR": report["error"],
                "MISSING_FROM_REVIEW": context["UNREAD_LISTINGS"],
                "STATUS_COLOUR": "#cf222e",
            }
        )
    for step in ("COLLECTION", "CLEANING", "PULL", "TRIAGE", "EXTRACTION", "RULES_PAGES"):
        timing(step)
    pull = report.get("pull", {})
    if pull:
        timing(
            "PULL",
            datetime.fromisoformat(pull["started_at"]),
            datetime.fromisoformat(pull["finished_at"]),
            "ok",
        )
    if pull.get("grouping_available") is False:
        context['failed_steps'].append({'STEP':'Pull','STATUS':'partial','ERROR':'Current grouping unavailable; previous assignments retained','MISSING_FROM_REVIEW':'—','STATUS_COLOUR':'#9a6700'})
    timings = report.get("timings", {})
    counters = report.get("counters", {})
    for step in ("triage", "extraction"):
        if counters.get(step + "_start") and counters.get(step + "_end"):
            timings[step] = {
                "start": counters[step + "_start"],
                "end": counters[step + "_end"],
                "status": "partial" if any(r["step"] == step for r in unread) else "ok",
            }
    for step, values in timings.items():
        timing(
            step.upper(),
            datetime.fromisoformat(values["start"]),
            datetime.fromisoformat(values["end"]),
            values.get("status", "ok"),
        )
    usage=[{**row,"cost_usd":Decimal(row["cost_usd"])} for row in report.get("usage_totals",{}).values()]
    if not usage:usage = budget.usages if budget is not None else []
    if not usage:
        usage = cost_totals(conn, started)
    total_cost = Decimal(0)
    total_input = 0
    total_cache = 0
    counters = report.get("counters", {})
    for stage in ("triage", "extraction"):
        context[stage.upper()+'_STATUS']='partial' if any(r['step']==stage for r in unread) or counters.get(stage+'_no_answer',0) else 'ok'
        context[stage.upper()+'_STATUS_COLOUR']='#9a6700' if context[stage.upper()+'_STATUS']=='partial' else '#1f883d'
        if counters.get(stage+'_no_answer',0):context['failed_steps'].append({'STEP':stage.title(),'STATUS':'failed','ERROR':stage+' failed twice','MISSING_FROM_REVIEW':number(counters[stage+'_no_answer']),'STATUS_COLOUR':'#cf222e'})
        for tiers, label in ((("flex", "batch"), "HALF"), (("standard",), "FULL")):
            rows = [
                r for r in usage if r["stage"] == stage and r["service_tier"].casefold() in tiers
            ]
            cost = sum((r["cost_usd"] for r in rows), Decimal(0))
            total_cost += cost
            context[stage.upper() + "_" + label + "_COST"] = dollars(cost)
            context[stage.upper() + "_" + label + "_LISTINGS"] = number(
                counters.get(stage + "_" + label.lower() + "_listings", 0)
            )
        context[stage.upper() + "_COST"] = dollars(
            sum((r["cost_usd"] for r in usage if r["stage"] == stage), Decimal(0))
        )
        cached = sum(r["cached_tokens"] for r in usage if r["stage"] == stage)
        input_tokens = sum(r["input_tokens"] for r in usage if r["stage"] == stage)
        total_input += input_tokens
        total_cache += cached
        context[stage.upper() + "_CACHE"] = (
            f"{100 * cached / (cached + input_tokens):.0f}%" if cached + input_tokens else "—"
        )
        for suffix in ("SENT", "RETRIED", "NO_ANSWER"):
            context[stage.upper() + "_" + suffix] = number(
                counters.get(stage + "_" + suffix.lower(), 0)
            )
    for label in ("HALF", "FULL"):
        context[label + "_COST"] = dollars(
            sum(
                (
                    r["cost_usd"]
                    for r in usage
                    if (r["service_tier"].casefold() in {"flex", "batch"}) == (label == "HALF")
                ),
                Decimal(0),
            )
        )
        context[label + "_LISTINGS"] = number(
            sum(
                counters.get(s + "_" + label.lower() + "_listings", 0)
                for s in ("triage", "extraction")
            )
        )
    context.update(
        READS_COST=dollars(total_cost),
        NIGHT_COST="—",
        NIGHT_CACHE=f"{100 * total_cache / (total_cache + total_input):.0f}%"
        if total_cache + total_input
        else "—",
        CEILING=dollars(budget.ceiling) if budget else dollars(report["ceiling_usd"]) if report.get("ceiling_usd") else "—",
        CEILING_TIME=clock(budget.ceiling_time) if budget and budget.ceiling_time else "—",
        TITLE_FILTER=number(counters.get("title_filter", 0)),
        LANGUAGE_CUT=number(counters.get("language_cut", 0)),
        TRIAGE_PASSED=number(counters.get("triage_passed", 0)),
        SPENT="—",
        PLAN="—",
    )
    if collection_rows is None and dbx is not None and config:
        try:
            _handoff, collection_rows = read_collection(dbx, config)
        except Exception:
            log.warning("Collection report unavailable")
            collection_rows = []
    for row in collection_rows or []:
        context["collection_runs"].append(
            {
                "BOARD": row["board"].title(),
                "MARKET": row["market"].upper(),
                "STATUS": row["status"],
                "STATUS_COLOUR": {"ok": "#1f883d", "partial": "#9a6700"}.get(
                    row["status"], "#cf222e"
                ),
                "JOB_LISTINGS_SEEN": number(row["unique_seen"])
                if row.get("unique_seen") is not None
                else "—",
                "NEW": number(row["new_jobs"])
                if row.get("new_jobs") is not None and row["board"] not in {"indeed", "linkedin"}
                else "—",
                "JDS_FETCHED": number(row["jds_fetched"])
                if row.get("jds_fetched") is not None
                else "—",
            }
        )
        if collection_problem(row):
            context['failed_steps'].append({'STEP':'Collection','STATUS':row['status'],'ERROR':row.get('error_reason') or 'Collection evidence incomplete','MISSING_FROM_REVIEW':'—','STATUS_COLOUR':'#cf222e'})
        if row["status"] == "partial" and not collection_problem(row):
            context["routine_notes"].append(
                {
                    "STEP": "Collection",
                    "BOARD": row["board"].title(),
                    "MARKET": row["market"].upper(),
                    "NOTE": row.get("error_reason") or (
                        f"{row['fetch_failures']} payload fetch(es) failed"
                        if row.get("fetch_failures") else "Incomplete payload evidence"
                    ),
                }
            )
    collection_cost_usd = None
    if collection_rows:
        ids=[r['run_id'] for r in collection_rows if r.get('run_id')]
        try:
            with conn.transaction():
                value=conn.execute("SELECT sum(cost_usd) FROM (SELECT bto_run_id,cost_usd FROM costs.apify_indeed UNION ALL SELECT bto_run_id,cost_usd FROM costs.apify_linkedin) runs WHERE bto_run_id=ANY(%s)",(ids,)).fetchone()[0]
            collection_cost_usd = value
            context['COLLECTION_COST']=dollars(value) if value is not None else '—'
        except Exception:context['COLLECTION_COST']='—'
    else:context['COLLECTION_COST']='—'
    context['NIGHT_COST'] = dollars(total_cost + collection_cost_usd) if collection_cost_usd is not None else '—'
    collection_cost=context['COLLECTION_COST']
    for source, target in (
        ("unique_seen", "TOTAL_JOB_LISTINGS_SEEN"),
        ("new_jobs", "TOTAL_NEW"),
        ("jds_fetched", "TOTAL_JDS_FETCHED"),
    ):
        values = [r.get(source) for r in collection_rows or []]
        context[target] = (
            number(sum(v for v in values if v is not None))
            if any(v is not None for v in values)
            else "—"
        )

    def instant(value):
        return (
            datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=timezone.utc)
            if isinstance(value, str)
            else value
        )

    starts = [instant(r["started_at"]) for r in collection_rows or [] if r.get("started_at")]
    ends = [instant(r["finished_at"]) for r in collection_rows or [] if r.get("finished_at")]
    timing(
        "COLLECTION",
        min(starts) if starts else None,
        max(ends) if ends else None,
        "partial"
        if any(r["status"] != "ok" for r in collection_rows or [])
        else "ok"
        if starts
        else "—",
    )
    context['COLLECTION_COST']=collection_cost
    whole_start = min(starts) if starts else started
    context.update(
        NIGHT_START=clock(whole_start),
        NIGHT_END=clock(ended),
        NIGHT_DURATION=str(timedelta(seconds=int((ended - whole_start).total_seconds()))),
    )
    if budget:
        for note in budget.notes:
            context["failed_steps"].append(
                {
                    "STEP": "Gemini",
                    "STATUS": "note",
                    "ERROR": note,
                    "MISSING_FROM_REVIEW": "—",
                    "STATUS_COLOUR": "#9a6700",
                }
            )
    if config and config.get("apify_token") and dbx is not None:
        try:
            from bto.fetch_job_listings.apify_client import ApifyClient

            account = ApifyClient(config["apify_token"]).account()
            context["SPENT"] = f"{account['spentUsd']:.2f}"
            context["PLAN"] = f"{account['includedUsd']:.2f}"
        except Exception:
            log.warning("Apify monthly account report unavailable")
    import subprocess

    try:
        result = subprocess.run(
            [
                "systemctl",
                "show",
                "clean-job-listings.service",
                "--property=ExecMainStartTimestamp,ExecMainExitTimestamp,ExecMainStatus",
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=3,
            env={**os.environ, "TZ": "UTC"},
        )
        values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)

        def unit_time(value):
            return datetime.strptime(value, "%a %Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)

        begin = unit_time(values["ExecMainStartTimestamp"])
        end = unit_time(values["ExecMainExitTimestamp"])
        if begin >= whole_start:
            if values["ExecMainStatus"]!="0":context["failed_steps"].append({"STEP":"Cleaning","STATUS":"failed","ERROR":"Cleaning exited with status "+values["ExecMainStatus"],"MISSING_FROM_REVIEW":"—","STATUS_COLOUR":"#cf222e"})
            timing("CLEANING", begin, end, "ok" if values["ExecMainStatus"] == "0" else "failed")
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        pass
    cuts = read_rows(
        conn,
        "SELECT bucket,reason,count(*) n FROM job_sorting.job_listings WHERE id>%s AND wording_first_seen_at>=%s AND (bucket='title_filter' OR reason LIKE 'JD not in English%%') GROUP BY 1,2",
        (report["first_id"], PROCESSING_START),
    )
    context["TITLE_FILTER"] = number(sum(r["n"] for r in cuts if r["bucket"] == "title_filter"))
    context["LANGUAGE_CUT"] = number(
        sum(r["n"] for r in cuts if r["reason"] and r["reason"].startswith("JD not in English:"))
    )
    monday = night(ended).weekday() == 0
    if monday:
        from bto.filter_and_bucket_job_listings.evaluate_job_requirements import (
            evaluate_requirements,
        )

        asks = Counter()
        ineligible = Counter()
        from bto.filter_and_bucket_job_listings.bucket_job_listings import listing_market

        for row in stream_rows(
            conn,
            JOIN_RESULTS
            + " WHERE j.wording_first_seen_at >= %s AND j.extraction_input_hash IS NOT NULL",
            (
                datetime.combine(night(ended) - timedelta(days=6), datetime.min.time(), SINGAPORE)
                + timedelta(hours=3),
            ),
        ):
            result = evaluate_requirements(
                row["extraction"]["lines"],
                row["extraction"]["kinds"][0],
                listing_market(row),
                preferences,
            )
            names = {
                line["detail"]
                for line in result["against"]
                if line["type"] in {"tool", "credential", "industry"}
                and line["status"] == "required"
                and line["detail"] in result["lacks"]
            }
            asks.update(names)
            if row["bucket"] == "ineligible":
                ineligible.update(names)
        context["lacks"] = [
            {"LACK": name, "LACK_ASKED": number(n), "LACK_INELIGIBLE": number(ineligible[name])}
            for name, n in asks.most_common()
        ]
    context["conditions"].update(
        {
            "MONDAY": monday,
            "ANY FULL PRICE": bool(int(context["FULL_LISTINGS"].replace(",", ""))),
            "CEILING REACHED": bool(budget and budget.ceiling_time),
            "ANY PROBLEMS OR ROUTINE NOTES": bool(
                context["failed_steps"] or context["routine_notes"]
            ),
        }
    )
    return context


# Exit statuses clean_job_listings.run returns only after it has DELIVERED
# its own alert (BuildError 2, StaleMapping 3, any other handled exception
# 5). Those same failures exit 6 instead when the send did not reach SMTP,
# and 6 is deliberately absent here so the fallback retries them. Status 1 is
# deliberately NOT here: it is what Python exits with when it never reached
# main() — an import error, a missing dependency, or a syntax error. Every other
# unsuccessful outcome needs the fallback: exit 6 means Python's alert did not
# reach SMTP, while a signal, timeout, OOM kill, exit 1, or exit 4 may prevent
# an alert attempt. Best-effort, like every alert here: the fallback is this
# module under the same venv interpreter, so an exec failure, missing venv, or
# missing source tree is reported by systemd and the journal only.
PYTHON_ALERTED_EXIT_STATUSES = ("2", "3", "5")


def render_terminal_nightly(report):
    """One terminal report, preserving the approved successful summary layout."""
    import html as html_module
    from decimal import Decimal
    sorting = report.get("sorting", {})
    failed = report.get("status") != "complete"
    context = sorting.get("email_context")
    if context:
        from datetime import datetime, timedelta

        from bto.filter_and_bucket_job_listings import SINGAPORE

        context = dict(context)
        cleaning_stage = report.get("stages", {}).get("cleaning", {})
        if cleaning_stage.get("finished_at"):
            begin = datetime.fromisoformat(cleaning_stage["started_at"])
            end = datetime.fromisoformat(cleaning_stage["finished_at"])
            context.update(CLEANING_START=begin.astimezone(SINGAPORE).strftime("%-I:%M %p"),
                CLEANING_END=end.astimezone(SINGAPORE).strftime("%-I:%M %p"),
                CLEANING_DURATION=str(timedelta(seconds=int((end-begin).total_seconds()))),
                CLEANING_STATUS="ok" if cleaning_stage["status"] == "complete" else "failed")
        subject, plain, markup = render_morning_email(context)
    else:
        subject, plain, markup = "BTO nightly run", "", "<html><body></body></html>"
    if failed:
        subject = "BTO · FAILED / NOT FINISHED · " + str(report.get("failed_stage") or sorting.get("failed_stage") or "nightly run")
    lines = ["Overall run: " + ("FAILED / NOT FINISHED" if failed else "COMPLETE")]
    if failed:
        lines += ["Failed stage: " + str(report.get("failed_stage") or sorting.get("failed_stage") or "unverified"),
                  "Failure: " + str(report.get("error") or sorting.get("error") or "See service journal")]
    for stage, outcome in report.get("stages", {}).items():
        lines.append(f"{stage}: {outcome.get('status')} ({outcome.get('started_at')} → {outcome.get('finished_at', 'unfinished')})")
    cleaning = report.get("cleaning", {})
    if cleaning:
        lines.append("Cleaning: " + json.dumps(cleaning, default=str))
    lines += ["Source publication: " + json.dumps(sorting.get("source", "not verified")),
              "Safely resumable: " + ("yes; use saved logical run/checkpoint" if sorting.get("safely_resumable") or sorting.get("status") == "complete" else "verify interrupted stage before rerunning"),
              "Known Batch inference: USD " + str(sorting.get("known_batch_usd") or sum((Decimal(r["cost_usd"]) for r in sorting.get("receipts", {}).values()), Decimal(0))),
              "Unknown Batch liabilities: " + json.dumps(sorting.get("unknown", {})),
              "Cache allowance: USD " + str(sorting.get("cache_upper_usd", "0")),
              "Historical Flex liabilities: retained in their separate ledgers; no amounts erased",
              "Review: " + str(sorting.get("review_publication", "Previous serving layer unchanged; publication not reached"))]
    outstanding = {j.get("name", j.get("display", "unreceipted")): j.get("upper_usd", "unverified")
        for jobs in sorting.get("jobs", {}).values() for j in jobs if not j.get("ingested_at")}
    lines.append("Outstanding Batch billing bounds: " + json.dumps(outstanding))
    pending_inputs = {stage: sum(i.get("outcome") not in {"valid", "terminal"} for i in items.values())
        for stage, items in sorting.get("items", {}).items()}
    lines.append("Unfinished distinct stage inputs: " + json.dumps(pending_inputs))
    if sorting.get("review_refresh"):
        lines.append("Review refresh receipt: " + json.dumps(sorting["review_refresh"]))
    if report.get("collection_problems"):
        lines.append("Collection coverage problems: " + json.dumps(report["collection_problems"], default=str))
    if sorting.get("estimate"):
        lines.append("Preflight estimate: " + json.dumps(sorting["estimate"], default=str))
    validation = sorting.get("validation")
    if validation:
        lines.append("Review population/buckets: " + json.dumps(validation, default=str))
    for stage, jobs in sorting.get("jobs", {}).items():
        for job in jobs:
            lines.append(f"{stage}: {job.get('name', 'submission not receipted')}; last observed {job.get('status')}; "
                         f"items {job['item_count']}; submission attempts {job.get('submission_attempt', 0)}; "
                         f"item outcomes {job.get('counts', {})}; polling interruptions {len(job.get('polling_interruptions', []))}")
    lines.append("Unfinished: " + (str(report.get("error") or sorting.get("error") or "see failed stage and saved checkpoint") if failed else "none; legitimate terminal failed-twice outcomes are included above"))
    detail = "\n".join(lines)
    plain += "\n\n" + detail
    markup = markup.replace("</body>", "<pre style=\"white-space:pre-wrap\">" + html_module.escape(detail) + "</pre></body>")
    return subject, plain, markup


def service_failure_alert(environ, unit, send=None):
    """The systemd fallback: called from ExecStopPost= with $SERVICE_RESULT,
    $EXIT_CODE and $EXIT_STATUS in `environ`. Attempts one alert when `unit`
    ended without a delivered Python alert; nothing on success or on a cleaning
    exit status that confirms delivery. It reads SMTP settings directly from
    the service environment so unrelated application configuration cannot
    prevent the fallback from starting. Never raises; returns ``True`` only
    when the message was handed to SMTP."""
    try:
        if environ.get("BTO_COMBINED_NIGHTLY_EMAIL") == "1":
            from pathlib import Path

            from bto.filter_and_bucket_job_listings.call_gemini_batch import save, timestamp
            path = Path("data/job_sorting/nightly.json")
            state = json.loads(path.read_text()) if path.exists() else {"stages": {}}
            same_invocation = (not environ.get("INVOCATION_ID") or
                               state.get("invocation_id") == environ.get("INVOCATION_ID"))
            if environ.get("SERVICE_RESULT") == "success" or (same_invocation and state.get("terminal_email_attempted_at")):
                return False
            if not same_invocation:
                state = {"stages": {}, "started_at": timestamp(),
                         "invocation_id": environ.get("INVOCATION_ID")}
            if state.get("run_id"):
                batch_path = path.parent / "runs" / state["run_id"] / "batch.json"
                if batch_path.exists():
                    state["sorting"] = json.loads(batch_path.read_text())
            state.update(status="failed", failed_stage=state.get("stage", unit),
                         error="Service terminated: " + environ.get("SERVICE_RESULT", "unknown"))
            subject, plain, html = render_terminal_nightly(state)
            state["terminal_email_attempted_at"] = timestamp()
            save(path, state)
            settings = {"smtp_host": environ.get("SMTP_HOST", ""), "smtp_port": int(environ.get("SMTP_PORT", 587)),
                        "smtp_username": environ.get("SMTP_USERNAME", ""), "smtp_password": environ.get("SMTP_PASSWORD", ""),
                        "notify_email_to": environ.get("NOTIFY_EMAIL_TO", "")}
            state["terminal_email_delivered"] = send_email(settings, subject, plain, html=html)
            save(path, state)
            return state["terminal_email_delivered"]
        result = environ.get("SERVICE_RESULT", "")
        code, status = environ.get("EXIT_CODE", ""), environ.get("EXIT_STATUS", "")
        cleaning = unit.startswith("clean")
        if unit == "filter-and-bucket-job-listings" and code == "exited" and status == "5":
            return False  # the approved morning failure report reached SMTP
        if result == "success":
            return False
        # 2/3/5 belong to the cleaning exit contract. Collection never sends a
        # Python alert for them — its 2 is an argparse usage failure — so this
        # suppression is scoped to cleaning.
        if cleaning and code == "exited" and status in PYTHON_ALERTED_EXIT_STATUSES:
            return False                    # Python attempted its own alert
        settings = {
            "smtp_host": environ.get("SMTP_HOST") or "",
            "smtp_port": int(environ.get("SMTP_PORT") or 587),
            "smtp_username": environ.get("SMTP_USERNAME") or "",
            "smtp_password": environ.get("SMTP_PASSWORD") or "",
            "notify_email_to": environ.get("NOTIFY_EMAIL_TO") or "",
        }
        # Fetch failures block cleaning, so report the skipped stage explicitly.
        cleaning_note = (
            "\nCleaning was SKIPPED because collection did not finish "
            "normally.\n"
            if unit == "fetch-job-listings" else "")
        detail = (f"systemd result:  {result or 'unknown'}\n"
                  f"exit code:       {code or 'unknown'}\n"
                  f"exit status:     {status or 'unknown'}\n\n"
                  "No Python alert was delivered. Delivery may have failed, "
                  "or the process may have been killed, timed out, run out "
                  "of memory, or failed during imports or configuration.\n"
                  f"{cleaning_note}"
                  f"Full detail: journalctl -u {unit}")
        subject = CLEAN_ALERT_SUBJECT if cleaning else ALERT_SUBJECT
        return bool((send or send_problem_alert)(
            settings, run_id="systemd", board="all", market="all",
            cause=f"{unit} ended without a delivered Python alert "
                  f"({result or 'unknown'})",
            detail=detail, subject=subject))
    except Exception as error:
        log.error("service failure alert not sent: %s", error)
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m bto.send_notifications.notify")
    parser.add_argument("--service-failure", metavar="UNIT", required=True,
                        help="systemd ExecStopPost= fallback alert for UNIT")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, force=True,
                        format="%(asctime)s %(levelname)-7s %(name)s %(message)s")
    service_failure_alert(os.environ, args.service_failure)
    return 0                    # never turns the unit's own result into a failure


if __name__ == "__main__":
    sys.exit(main())
