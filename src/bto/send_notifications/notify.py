"""Format and send collection and cleaning summaries and failure alerts.

Sending is fail-soft: missing SMTP configuration or a send failure is logged
and returns ``False`` without undoing the work being reported. ``True`` means
the message was handed to the SMTP server, not that a recipient read it.
Callers decide which conditions warrant an alert; this module does not query
application state. It also provides the systemd fallback for failures that
ended without a delivered Python alert.
"""

import argparse
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


def service_failure_alert(environ, unit, send=None):
    """The systemd fallback: called from ExecStopPost= with $SERVICE_RESULT,
    $EXIT_CODE and $EXIT_STATUS in `environ`. Attempts one alert when `unit`
    ended without a delivered Python alert; nothing on success or on a cleaning
    exit status that confirms delivery. It reads SMTP settings directly from
    the service environment so unrelated application configuration cannot
    prevent the fallback from starting. Never raises; returns ``True`` only
    when the message was handed to SMTP."""
    try:
        result = environ.get("SERVICE_RESULT", "")
        code, status = environ.get("EXIT_CODE", ""), environ.get("EXIT_STATUS", "")
        cleaning = unit.startswith("clean")
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
