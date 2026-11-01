"""Render and send nightly emails and manual collection summaries.

Sending returns False on failure and leaves completed work intact. The nightly
workflow and its systemd fallback share a checkpoint to prevent duplicate mail.
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

from bto.log_errors import log_failure

log = logging.getLogger(__name__)

SUMMARY_SUBJECT = "[back-to-office] Fetch Job Listings Summary"
CLEAN_SUMMARY_SUBJECT = "[back-to-office] Clean Job Listings Summary"


def send_email(settings, subject, body, html=None):
    """Send plain text and optional HTML; return whether SMTP accepted the message.

    Use implicit TLS on port 465 and STARTTLS on other ports."""
    host = settings.get("smtp_host")
    username = settings.get("smtp_username")
    password = settings.get("smtp_password")
    to = settings.get("notify_email_to")
    if not (host and username and password and to):
        log.warning("email not configured — NOT sent: %s", subject)
        return False

    try:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = username
        message["To"] = to
        message.set_content(body)
        if html:
            message.add_alternative(html, subtype="html")

        port = settings.get("smtp_port") or 587
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
        log_failure(log, "email FAILED (%s) — not delivered", subject, error=error)
        return False


def send_clean_summary(settings, *, run_id, stage, inserted, canonicalized,
                       version, elapsed=None, standardization_elapsed=None,
                       canonicalization_elapsed=None, canonicalization_mode=None,
                       fallback_reason=None):
    """Send a manual cleaning summary using only the completed run's known facts.

    The combined nightly workflow reports these facts in its terminal email."""
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
    lines += ["", "Full detail: journalctl -u nightly-run · "
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
    Full detail: <code>journalctl -u nightly-run</code> ·
    <code>bronze.scrape_runs</code></p>
</div>
"""
    return plain, html


def send_summary(settings, rows, missing_pairs, night_label):
    plain, html = build_summary_body(rows, missing_pairs, night_label)
    return send_email(settings, SUMMARY_SUBJECT, plain, html=html)


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
        "jds_intended",
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
    from bto.storage.postgres.read import read_rows

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


def render_terminal_nightly(report):
    """One designed terminal message; private diagnostics stay in checkpoints."""
    from .nightly_email import render

    return render(report)


def service_failure_alert(environ, unit):
    """Send the nightly crash fallback, or retry an undelivered terminal email.

    The shared checkpoint prevents duplicates. Standalone Collection and
    Cleaning commands retain their errors in logs rather than send old alerts.
    """
    try:
        if environ.get("BTO_COMBINED_NIGHTLY_EMAIL") == "1":
            from pathlib import Path

            from bto.filter_and_bucket_job_listings.call_gemini_batch import save, timestamp
            path = Path("data/job_sorting/nightly.json")
            state = json.loads(path.read_text()) if path.exists() else {"stages": {}}
            same_invocation = (not environ.get("INVOCATION_ID") or
                               state.get("invocation_id") == environ.get("INVOCATION_ID"))
            if same_invocation and state.get("terminal_email_delivered"):
                return False
            if environ.get("SERVICE_RESULT") == "success" and not (
                same_invocation and state.get("terminal_email_attempted_at")
            ):
                return False
            if not same_invocation:
                state = {"stages": {}, "started_at": timestamp(),
                         "invocation_id": environ.get("INVOCATION_ID")}
            if state.get("run_id"):
                batch_path = path.parent / "runs" / state["run_id"] / "batch.json"
                if batch_path.exists():
                    state["sorting"] = json.loads(batch_path.read_text())
            if state.get("status") not in {"complete", "failed"}:
                state.update(status="failed", process_stopped=True,
                             stopped_at=timestamp(),
                             failed_stage=state.get("sorting", {}).get("stage") or state.get("stage", unit),
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
        return False
    except Exception as error:
        log_failure(log, "service failure alert not sent unit=%s", unit, error=error)
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
