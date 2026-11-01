"""Render the nightly emails from saved facts, as mail-safe HTML and plain text."""

import os
import re
from collections import defaultdict
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
from html import escape
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

SGD_RATE = Decimal("1.28")
SUBJECT_PREFIX = "[BTO Nightly Runs]"
MARKETS = ("SG", "HK", "TH")
STEPS = ("Collection", "Cleaning & canonicalisation", "Load to database", "Triage", "Extraction", "Rules & Review")
STEP_KEYS = ("collection", "cleaning", "preparation", "triage", "extraction", "rules and review")
BOARD_NAMES = {"mcf": "MyCareersFuture", "jobstreet": "JobStreet", "jobsdb": "JobsDB", "seek": "SEEK", "indeed": "Indeed", "linkedin": "LinkedIn"}
MUTED = "color:#6F695E;"
BORDER = "border-bottom:1px solid #E5E0D5;"


def instant(value):
    if not value:
        return None
    return value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))


def amount(value):
    try:
        result = Decimal(str(value).replace("USD ", "").replace(",", ""))
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def count(value):
    return int(str(value or 0).replace(",", ""))


def money(value):
    if value is None:
        return "—"
    displayed_usd = value.quantize(Decimal("0.01"))
    return f"US${displayed_usd:.2f} ~S${displayed_usd * SGD_RATE:.2f}"


def money_markup(value, *, sgd_size=None):
    """Emphasise USD while keeping its approximate SGD display secondary."""
    if not value.startswith("US$") or " ~S$" not in value:
        return escape(value)
    usd, sgd = value.split(" ", 1)
    size = f"font-size:{sgd_size}px;" if sgd_size else ""
    return "<b>" + escape(usd) + '</b> <span style="font-weight:400;' + size + MUTED + '">' + escape(sgd) + "</span>"


def clock(value):
    stamp = instant(value)
    return stamp.astimezone(ZoneInfo("Asia/Singapore")).strftime("%-I:%M %p") if stamp else "—"


def duration(begin, end):
    if not begin or not end:
        return "—"
    minutes = max(0, int((instant(end) - instant(begin)).total_seconds()) // 60)
    hours, minutes = divmod(minutes, 60)
    return (f"{hours}h {minutes:02d}m" if hours else f"{minutes}m")


# Error explanations and safe diagnostic summaries. Full evidence stays private.

def short_reason(value):
    """Keep a short diagnostic without provider bodies, secrets or private IDs."""
    raw = str(value or "—")
    if "Traceback (" in raw:
        messages = re.findall(r"(?:^|\n)(?:[A-Za-z_]+Error|[A-Za-z_]+Exception): (.+)", raw)
        raw = messages[-1] if messages else "The step exited unexpectedly. Check its private service log."
    if "poll" in raw.casefold() and any(v in raw.casefold() for v in ("exhaust", "retry", "reconnect")):
        raw = "The provider connection could not be recovered. Check the saved run before resuming."
    text = " ".join(raw.split())
    for key, secret in os.environ.items():
        if secret and any(word in key for word in ("PASSWORD", "TOKEN", "API_KEY", "SECRET")):
            text = text.replace(secret, "[private]")
    text = re.sub(r"(?i)([?&](?:key|token|password)=)[^ &]+", r"\1[private]", text)
    text = re.sub(r"\b(?:batches|files|cachedContents)/[^\s,;]+", "[private provider reference]", text)
    text = re.sub(r"\b\d{4}-\d\d-\d\dT\S+", "[saved time]", text)
    # The full body remains in the private checkpoint/journal, never in the email.
    if "{" in text or ("[" in text and re.search(r'\[\s*[\{"\d]', text)):
        prefix = text.split("{", 1)[0].split("[", 1)[0].strip(" :")
        text = (prefix + ". " if prefix else "") + "Detailed diagnostics are saved privately."
    return text[:220] + ("…" if len(text) > 220 else "")


def error_explanation(value):
    """Explain recognised evidence only; an unknown error has no guessed cause."""
    text = str(value or "").casefold()
    if "deployed wheel identity" in text:
        return "The packaged cleaning code on Databricks did not match the server's code, so cleaning stopped before grouping jobs."
    if "provider items still failed" in text:
        return "Gemini refused too many JDs, even after a retry each, so the night stopped before finishing them."
    if "stalemapping" in text or "canonical mapping not current" in text:
        return "Cleaning's job grouping did not match the cleaned listings, so Sorting stopped before reading them."
    if "internal error" in text and "batch" in text and any(v in text for v in ("google", "gemini")):
        return "Google reported an internal error for the whole batch."
    if "could not be processed by the warehouse" in text:
        return "Databricks SQL warehouse refused the request."
    if "/sql/warehouses/" in text and "deny_new_and_existing_resources" in text:
        return "Databricks refused to start the SQL warehouse."
    provider = (
        "Databricks" if "databricks" in text or "/api/2." in text else
        "Gemini" if "gemini" in text or "google http status" in text else None
    )
    status = re.search(r"\bhttp(?:\s+status)?\s+(\d{3})\b", text)
    if provider:
        if "connection unavailable" in text or "network error" in text:
            return f"The connection to {provider} could not be completed."
        if provider == "Gemini" and any(v in text for v in ("spending cap", "spend cap", "spending limit")):
            return "Gemini refused further work because its spending limit was reached."
        if provider == "Gemini" and "failed_precondition" in text:
            return "Gemini rejected the request because a required precondition was not met."
        if status:
            code = int(status[1])
            if code == 401:
                return f"{provider} rejected authentication for the request."
            if code == 403:
                return f"{provider} denied access to the requested operation."
            if code == 429:
                return f"{provider} refused the request because a quota or rate limit was reached."
            if 500 <= code <= 599:
                return f"{provider} returned a server-side error."
    if "poll" in text and any(v in text for v in ("exhaust", "retry", "reconnect")):
        return "The provider connection could not be recovered. Check the saved run before resuming."
    if "review admin login failed" in text:
        return "BTO could not sign in to refresh Review."
    if "review cache did not confirm" in text or "review publication was not confirmed" in text:
        return "BTO could not confirm that the refreshed Review snapshot was live."
    if "review publication helper could not finish" in text:
        return "BTO could not start or finish the Review refresh worker. Check the service log before resuming."
    if "review publication helper failed" in text:
        return "The Review refresh worker did not complete successfully. Check the service log before resuming."
    if "batch billing gap" in text:
        return "Gemini usage receipts are missing; charges must be reconciled before more work is submitted."
    if "batch accounting ceiling reached" in text:
        return "The saved Batch spending ceiling was reached."
    if re.search(r"quote exceeds (?:30|60) words or is not found", text):
        return "The model's evidence quote was too long or could not be matched to the supplied wording."
    if "quote was not found" in text or "quote not found" in text:
        return "The model's evidence quote could not be matched to the supplied wording."
    if "no complete answer" in text:
        return "The model returned no complete answer."
    if any(v in text for v in ("exactly four listing fields", "exactly nine fields", "invalid line", "json answer")):
        return "The answer did not match the expected format."
    return None


def link(url, label, *, button=False, primary=False):
    if urlsplit(str(url or "")).scheme not in ("http", "https"):
        return "—"
    style = "color:#35618E;text-decoration:none;"
    if button:
        style += "display:inline-block;margin:0 8px 8px 0;padding:12px 18px;border:1px solid #B9C8D8;border-radius:8px;font-size:14px;font-weight:700;"
        if primary:
            style += "background:#35618E;color:#FFFFFF;border-color:#35618E;"
    return f'<a href="{escape(str(url), quote=True)}" style="{style}">{escape(label)}</a>'


class Body:
    def __init__(self):
        self.html, self.text = [], []

    def render(self, subject):
        template = Path(__file__).with_name("nightly_email.html").read_text()
        markup = template.replace("{{TITLE}}", escape(subject)).replace("{{CONTENT}}", "".join(self.html))
        return subject, "\n\n".join(self.text) + "\n", markup

    def add(self, markup, text):
        self.html.append(markup)
        self.text.append(text)

    def paragraph(self, text, *, style="", bold=False):
        content = escape(str(text))
        if bold:
            content = "<b>" + content + "</b>"
        self.add(f'<p style="margin:8px 0 0;{style}">{content}</p>', str(text))

    def section(self, title, badge=None):
        padding = "24px" if title == "What went wrong" else "8px 24px 28px" if title == "Jobs to review" else "0 24px 28px"
        self.html.append('<tr><td style="padding:' + padding + ';">')
        mark = (' <span style="display:inline-block;margin-left:8px;padding:3px 8px;border-radius:999px;background:#FBF3E2;color:#8A5A12;font-size:12px;font-weight:600;">' + escape(badge) + '</span>') if badge else ""
        divider = "" if title == "What went wrong" else "border-top:1px solid #E5E0D5;padding-top:24px;"
        self.add(f'<h2 style="margin:0 0 12px;font-size:18px;line-height:1.3;font-weight:700;letter-spacing:-.01em;{divider}">{escape(title)}{mark}</h2>', title + (" " + badge if badge else ""))

    def end(self):
        self.html.append("</td></tr>")

    def table(self, headers, rows, styles=None):
        styles = styles or {}
        cost_table = bool(rows and rows[0][0] == "Gemini pricing")
        compact = len(headers) == 2 and not cost_table
        size = "12px" if headers and headers[0] == "BOARD" else "14px"
        layout = "max-width:340px;width:90%;margin-top:10px;" if compact else "width:100%;"
        result = f'<table role="presentation" cellspacing="0" cellpadding="0" style="{layout}border-collapse:collapse;font-size:{size};font-variant-numeric:tabular-nums;">'
        if any(headers):
            result += "<tr>" + "".join(
                f'<td style="padding:0 2px 6px;border-bottom:1px solid #201D16;{MUTED}font-size:11px;font-weight:700;text-align:{"left" if i == 0 else "right"};">{escape(h)}</td>' for i, h in enumerate(headers)
            ) + "</tr>"
        for index, row in enumerate(rows):
            kind = styles.get(index)
            row_style = "background:#FBF3E2;font-weight:700;" if kind == "top" else "background:#F9E3DF;" if kind == "bad" else "color:#7A4F0E;" if kind == "amber" else "font-weight:700;" if kind == "total" else ""
            result += f'<tr style="{row_style}">'
            for i, value in enumerate(row):
                border = "border-top:2px solid #201D16;" if kind == "total" else "" if compact else BORDER
                if compact and row[0] in ("Passed to Extraction", "Successful"):
                    border = "border-top:1px solid #E5E0D5;font-weight:700;"
                label = f"{value:,}" if isinstance(value, int) else str(value if value is not None else "—")
                size = "font-size:22px;" if kind in ("top", "total") and i == len(row) - 1 else "font-size:15px;" if kind in ("top", "total") and i == 0 else ""
                number_style = "white-space:nowrap;" if isinstance(value, int) else ""
                padding = "3px 12px 3px 0" if compact and i == 0 else "3px 0" if compact else "8px 6px"
                column = "width:42%;" + MUTED if cost_table and i == 0 else ""
                muted = MUTED if compact and row[0] == "Inputs retried" else ""
                align = "left" if i == 0 or cost_table else "right"
                display = money_markup(label) if cost_table and i == 1 else escape(label)
                result += f'<td style="padding:{padding};{border}{size}{number_style}{column}{muted}vertical-align:top;overflow-wrap:anywhere;text-align:{align};">{display}</td>'
            result += "</tr>"
        self.add(result + "</table>", "\n".join(" | ".join(str(v if v is not None else "—") for v in row) for row in [headers, *rows]))


def stage_name(value):
    raw = str(value or "—").casefold()
    if "clean" in raw or "canonical" in raw:
        return STEPS[1]
    if any(s in raw for s in ("rules", "review", "validation")):
        return STEPS[5]
    if any(s in raw for s in ("prepar", "admission", "pull", "load to database")):
        return STEPS[2]
    return str(value or "—").title()


def failure_rows(sorting):
    saved = sorting.get("email_facts", {}).get("failure_listings", {})
    rows = []
    for stage, items in sorting.get("items", {}).items():
        for item in items.values():
            if item.get("outcome") == "terminal":
                for identifier in item.get("ids", [None]):
                    row = {**item.get("row", {}), **saved.get(str(identifier), {})}
                    explanation = error_explanation(item.get("correction"))
                    diagnostic = short_reason(item.get("correction"))
                    rows.append({**row, "id": identifier, "stage": stage.title(), "attempts": item.get("answer_attempts"), "reason": diagnostic, "explanation": explanation})
    return rows


def stage_inputs(sorting, stage):
    from bto.filter_and_bucket_job_listings.call_gemini_batch import submitted_jobs

    items = sorting.get("items", {}).get(stage, {})
    digests = {digest for job in submitted_jobs(sorting.get("jobs", {}).get(stage, [])) if job.get("name") for digest in job.get("keys", {}).values()}
    digests.update(digest for digest, item in items.items() if item.get("answer_attempts", 0))
    return len(digests)


def cache_share(sorting, context, stage):
    rows = [r for r in sorting.get("receipts", {}).values() if r.get("stage") == stage]
    if rows:
        cached = sum(r.get("cached_tokens", 0) for r in rows)
        total = cached + sum(r.get("input_tokens", 0) for r in rows)
        return f"{100 * cached / total:.0f}%" if total else "—"
    old = context.get(stage.upper() + "_CACHE", "")
    return old if re.fullmatch(r"\d{1,3}%", str(old)) else "—"


def step_status(report, title, failed_stage):
    sorting = report.get("sorting") or {}
    if report.get("status") == "complete":
        return "Done"
    if title == failed_stage:
        return "Failed"
    if failed_stage in STEPS and STEPS.index(title) > STEPS.index(failed_stage):
        return "Not run / not confirmed"
    key = STEP_KEYS[STEPS.index(title)]
    if key in ("collection", "cleaning") and report.get("stages", {}).get(key, {}).get("status") == "complete":
        return "Done"
    stage = sorting.get("stage")
    if title == STEPS[2] and sorting.get("preparation") is not None:
        return "Done"
    if title == "Triage" and (sorting.get("extraction_prepared") or stage in ("extraction", "rules and validation", "Review publication")):
        return "Done"
    if title == "Extraction" and stage in ("rules and validation", "Review publication"):
        return "Done"
    if title == STEPS[5] and sorting.get("review_refresh", {}).get("status") == "complete":
        return "Done"
    return "Not run / not confirmed"


def costs(sorting, context):
    result = {"Collection": amount(context.get("COLLECTION_COST"))}
    for stage in ("triage", "extraction"):
        rows = [r for r in sorting.get("receipts", {}).values() if r.get("stage") == stage]
        value = sum((Decimal(str(r["cost_usd"])) for r in rows), Decimal(0)) if rows else amount(context.get(stage.upper() + "_COST"))
        if value is None and stage in sorting.get("items", {}) and not sorting["items"][stage]:
            value = Decimal(0)
        result[stage.title()] = value
    return result


def resume_supported(sorting):
    """Offer resumption only when billing and bounded input failures allow it."""
    from bto.filter_and_bucket_job_listings import REFUSED_INPUT_LIMIT, spending_refusal

    if sorting.get("safely_resumable") is not True or sorting.get("unknown"):
        return False
    for items in sorting.get("items", {}).values():
        # Within the limit, refused inputs wait for a later night and a resume continues.
        refused = [i for i in items.values() if i.get("outcome") == "provider_failed"]
        if len(refused) > REFUSED_INPUT_LIMIT or any(spending_refusal(i.get("last_provider_error")) for i in refused):
            return False
    return not any(j.get("status") == "submission_rejected" and not j.get("verified_not_accepted_at")
                   for jobs in sorting.get("jobs", {}).values() for j in jobs)


def error_title(value):
    """Choose the most specific recognised failure before broad provider labels."""
    text = str(value or "").casefold()
    if "private preferences:" in text or "private configuration " in text or any(
        f"private {stage} prompt is unconfigured" in text for stage in ("triage", "extraction")
    ):
        return "Private configuration invalid"
    if "gemini batch" in text or "job_state_failed" in text:
        return "Gemini Batch job failed"
    # Before the provider rule: a sorting log tail names Databricks on every run,
    # and the wheel check names files under storage/databricks/.
    if "deployed wheel identity" in text:
        return "Cleaning code out of date"
    if "provider items still failed" in text:
        return "Gemini refused JDs"
    if "stalemapping" in text or "canonical mapping not current" in text:
        return "Job grouping out of date"
    if "databricks" in text or "/api/2." in text:
        return "Databricks unavailable"
    if "gemini" in text or "google http" in text:
        return "Gemini request failed"
    if "review" in text:
        return "Review refresh failed"
    return "Step failed" if value else "—"


def problem(body, report, failed_stage, *, crashed):
    sorting = report.get("sorting") or {}
    body.section("What went wrong")
    finished = [title for title in STEPS if step_status(report, title, failed_stage) == "Done"]
    error = report.get("error") or sorting.get("error")
    explanation = error_explanation(error)
    if crashed:
        title = failed_stage + " · process exited"
        fields = [
            ("What failed", "The service ended with an error. No step reported a failure first."),
            ("Last saved stage", failed_stage),
            ("Can backfill", "Probably. Check the service log first. A Gemini Batch job may still be running."),
            ("To do", "Look at the service log, then run a backfill."),
        ]
    else:
        title = failed_stage + " · " + error_title(error)
        upstream = failed_stage in STEPS[:2]
        safe = failed_stage in STEPS[2:] and resume_supported(sorting)
        details = []
        for name in STEPS:
            if name in finished:
                continue
            items = sorting.get("items", {}).get(name.lower())
            if items is not None:
                left = sum(i.get("outcome") not in {"valid", "terminal"} for i in items.values())
                details.append(f"{name} ({left:,} inputs left)")
            else:
                details.append(name)
        items = sorting.get("items", {}).get(failed_stage.lower())
        what = failed_stage
        if items is not None:
            valid = sum(i.get("outcome") == "valid" for i in items.values())
            if error_title(error) == "Gemini Batch job failed":
                jobs = sorting.get("jobs", {}).get(failed_stage.lower(), [])
                began = min((job["submission_at"] for job in jobs if job.get("submission_at")), default=None)
                ended = report.get("finished_at") or sorting.get("finished_at")
                elapsed = duration(began, ended) if began and ended else None
                verb = "extracted" if failed_stage == "Extraction" else "triaged"
                what = "The batch ended with an error" + (" after " + elapsed if elapsed else "") + f". {valid:,} of {len(items):,} inputs were {verb} before it stopped and are saved."
            else:
                what += f" stopped. {valid:,} of {len(items):,} inputs have saved successful answers."
        reuse = "Triage answers" if failed_stage == "Extraction" else "Validated answers"
        remaining = "Extraction requests" if failed_stage == "Extraction" else "requests"
        can = "Not automatically" if upstream or not safe else "Yes. Backfill picks up the saved run at " + failed_stage + ". " + reuse + " are reused and not billed again. The remaining " + remaining + " are billed."
        todo = (
            "Check the collection handoff, source run records and provider charges before rerunning collection."
            if failed_stage == STEPS[0] else
            "Check the Databricks job, source freshness and saved cleaning publication before rerunning cleaning."
            if upstream else
            "Run a backfill. Review updates when it finishes." if safe else
            "Inspect the service log and saved checkpoint before resuming; do not start a fresh paid run blindly."
        )
        fields = [
            ("What failed", what),
            ("Reason", explanation or "—"),
            ("Finished", ", ".join(finished) + ". Their results are saved." if finished else "—"),
            ("Not done", ", ".join(details) or "—"),
            ("Can backfill", can),
            ("To do", todo),
        ]
    border = "#E3A49A" if crashed else "#E9B98E"
    body.html.append(f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border:1px solid {border};border-radius:10px;"><tr><td style="padding:18px;">')
    body.paragraph(title, bold=True, style="font-size:16px;")
    body.html.append('<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin-top:12px;border-collapse:collapse;font-size:14px;">')
    for label, text in fields:
        emphasis = "font-weight:700;" if label == "To do" else ""
        body.add(f'<tr><td style="padding:7px 12px 7px 0;{MUTED}width:110px;vertical-align:top;border-top:1px solid #E5E0D5;{emphasis}">{escape(label)}</td><td style="padding:7px 0;vertical-align:top;border-top:1px solid #E5E0D5;overflow-wrap:anywhere;{emphasis}">{escape(text)}</td></tr>', label + ": " + text)
    body.html.append("</table></td></tr></table>")
    if not crashed:
        chips, plain = [], []
        for i, name in enumerate(STEPS):
            status = step_status(report, name, failed_stage)
            suffix = " ✓" if status == "Done" else " ✕" if status == "Failed" else " · not run"
            colour = "background:#E4F1EC;color:#17766A;" if status == "Done" else "background:#FBEBDD;color:#8A4300;" if status == "Failed" else "background:#F1EEE7;color:#6F695E;"
            chip = f"{i + 1} {name}{suffix}"
            chips.append(f'<span style="display:inline-block;margin:16px 6px 0 0;padding:4px 9px;border-radius:999px;font-size:12px;font-weight:600;{colour}">{escape(chip)}</span>')
            plain.append(chip)
        body.add("".join(chips), " · ".join(plain))
    body.end()


def source_values(row):
    board = str(row.get("board", row.get("BOARD", ""))).lower()
    market = str(row.get("market", row.get("MARKET", "—"))).upper()
    status = row.get("status", row.get("STATUS")) or "—"
    name = BOARD_NAMES.get(board, board.title() or "—") + " " + market
    failed = row.get("fetch_failures")
    intended = row.get("jds_intended")
    return board, name, status, failed, intended


def source_summary(row):
    _, name, status, failed, _ = source_values(row)
    if status == "partial":
        return name + " came back partial" + (f": {failed:,} JD fetch{'es' if failed != 1 else ''} failed." if failed else ".")
    return name + " failed."


def missing_by_stage(sorting):
    affected = defaultdict(set)
    unresolved = False
    for key in sorting.get("unknown", {}):
        found = False
        for stage, jobs in sorting.get("jobs", {}).items():
            for job in jobs:
                keys = job.get("keys", {})
                if key in keys:
                    affected[stage].add(keys[key])
                    found = True
                elif key == job.get("name") and keys:
                    affected[stage].update(keys.values())
                    found = True
        unresolved |= not found
    return affected, unresolved


def attention(body, report, rows, source_problems, *, cost_gap, listing_count=None):
    sorting = report.get("sorting") or {}
    listing_count = len(rows) if listing_count is None else listing_count
    badges = ([f"{listing_count:,} listing{'s' if listing_count != 1 else ''}"] if listing_count else [])
    if source_problems:
        badges.append(f"{len(source_problems)} source{'s' if len(source_problems) != 1 else ''}")
    if cost_gap:
        badges.append("cost")
    body.section("Needs attention", " + ".join(badges) or None)
    for row in source_problems:
        board, name, status, failed, intended = source_values(row)
        body.paragraph(f"Collection · {status} · 1", style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;" + MUTED)
        nothing = row.get("unique_seen") == 0 and row.get("jds_fetched") == 0
        title = name + (" came back partial" if status == "partial" else " returned nothing" if nothing else " failed")
        body.paragraph(title, bold=True, style="font-size:16px;")
        if status == "partial":
            if failed is not None and intended is not None:
                body.paragraph(f"{failed:,} of {intended:,} JD fetches failed.")
            else:
                body.paragraph(error_explanation(row.get("error_reason")) or "—")
            grey = "Collection · " + (f"{failed:,}" if failed is not None else "—") + f" fetch{'es' if failed != 1 else ''} failed"
        else:
            raw = str(row.get("error_reason") or "")
            if "apify" in raw.casefold() and any(v in raw.casefold() for v in ("timed out", "timeout")):
                reason = "The Apify run timed out."
            else:
                reason = error_explanation(raw) or "—"
            market = str(row.get("market", row.get("MARKET")) or "—").upper()
            body.paragraph(reason + " Other boards and markets were not affected. Tonight's " + market + " results are missing this board.")
            grey = "Collection"
        body.paragraph(grey, style="font-size:12px;" + MUTED)
    if rows and len(rows) <= 8:
        intro = f"The {len(rows):,} listings below failed on their own. They are in Not sorted and not counted in To review." if source_problems else "Individual listings only. The run itself is fine. They are in Not sorted and not counted in To review."
        body.paragraph(intro, style="margin-top:20px;" + MUTED)
    groups = defaultdict(list)
    for row in rows:
        groups[(row["stage"], row["reason"])].append(row)
    if listing_count and not rows:
        body.paragraph("—", style=MUTED)
    if len(rows) > 8:
        groups = dict(sorted(groups.items(), key=lambda pair: (-len(pair[1]), pair[0])))
        intro = "Grouped by reason; the first 3 of each are shown."
        if not source_problems:
            intro = "Individual listings only. The run itself is fine. " + intro
        body.paragraph(intro, style=MUTED)
        body.table(["", ""], [[stage + " · " + (members[0]["explanation"] or reason), len(members)] for (stage, reason), members in groups.items()])
    display_groups = groups if len(rows) > 8 else {
        (stage, ""): [r for r in rows if r["stage"] == stage] for stage in ("Triage", "Extraction") if any(r["stage"] == stage for r in rows)
    }
    for (stage, reason), members in display_groups.items():
        heading = f"{stage} · failed twice · {len(members):,}"
        if len(rows) > 8:
            group_reason = "quote not validated" if "quote" in reason.casefold() else members[0]["explanation"] or reason
            heading = f"{stage} · {group_reason} · first 3 of {len(members):,}"
        body.paragraph(heading, bold=True, style="margin-top:18px;font-size:11px;text-transform:uppercase;letter-spacing:.1em;" + MUTED)
        for row in members[:3] if len(rows) > 8 else members:
            title = row.get("title") or "—"
            company = row.get("advertiser_name") or "—"
            board = BOARD_NAMES.get(str(row.get("board", "")).lower(), "—")
            market = str(row.get("market") or "—").upper()
            if len(rows) > 8:
                detail = title + " · " + company + " · " + board + " " + market
                body.add('<p style="margin:4px 0;">' + escape(detail) + " · " + link(row.get("job_url"), "View job ↗") + "</p>", detail + " · View job ↗ " + str(row.get("job_url") or "—"))
                continue
            attempts = row.get("attempts")
            body.add('<p style="margin:8px 0 0;"><b>' + escape(title) + "</b> · " + escape(company) + "</p>", title + " · " + company)
            body.paragraph(row["explanation"] or row["reason"])
            detail = f"{board} · {market} · {attempts if attempts is not None else '—'} attempts"
            body.add('<p style="margin:4px 0 12px;font-size:12px;' + MUTED + '">' + escape(detail) + " · " + link(row.get("job_url"), "View job ↗") + "</p>", detail + " · View job ↗ " + str(row.get("job_url") or "—"))
    if len(rows) > 8:
        live = report.get("review_url")
        day = report.get("collection_night") or sorting.get("collection_night")
        url = live + "?" + urlencode({"buckets": "not_sorted", "seen": day or "any", "markets": "sg,hk,th", "view": "table"}) if live else None
        body.add('<p style="margin:14px 0 0;">' + link(url, "Open Not sorted in Review") + "</p>", "Open Not sorted in Review: " + (url or "—"))
    if cost_gap:
        body.paragraph("Cost incomplete", bold=True, style="margin-top:18px;")
        stages, unresolved = missing_by_stage(sorting)
        for stage, inputs in stages.items():
            n = len(inputs)
            body.paragraph(f"Gemini did not report usage for {n:,} {stage.title()} inputs. Their cost is not in tonight's total, so the real cost is slightly higher than shown.")
            body.paragraph(f"{stage.title()} · {n:,} inputs", style="font-size:12px;" + MUTED)
        if unresolved or not stages:
            body.paragraph("Gemini usage receipts are missing; the affected input count is —.")
    body.end()


def run_steps(body, report, context, charges, failed_stage):
    """Render each stage's saved outcome and recorded processing counts."""
    from bto.filter_and_bucket_job_listings.call_gemini_batch import submitted_jobs

    sorting = report.get("sorting") or {}
    facts = sorting.get("email_facts") or {}
    completed = report.get("status") == "complete"
    backfill_steps = set(report.get("backfill_steps", [])) | set(sorting.get("backfill_steps", []))
    body.section("Nightly run")
    body.paragraph("The 6 steps in order. All times SGT.", style=MUTED + "font-size:13px;")
    for i, title in enumerate(STEPS):
        status = step_status(report, title, failed_stage)
        if report.get("backfill") and status == "Done" and STEP_KEYS[i] in backfill_steps:
            status += " · backfill"
        row = report.get("stages", {}).get(STEP_KEYS[i], {}) if i < 2 else sorting.get("preparation_timing", sorting.get("pull", {})) if i == 2 else {}
        begin, end = row.get("started_at"), row.get("finished_at")
        if i in (3, 4):
            jobs = submitted_jobs(sorting.get("jobs", {}).get(title.lower(), []))
            begin = min((j["submission_at"] for j in jobs if j.get("submission_at")), default=None)
            end = max((j["ingested_at"] for j in jobs if j.get("ingested_at")), default=None)
        if i == 5:
            begin = sorting.get("rules_started_at")
            end = sorting.get("review_refresh", {}).get("confirmed_at") or sorting.get("finished_at")
        cost = money(charges[title]) if title in charges else "not tracked"
        colour = "#17766A" if status.startswith("Done") else "#A13F32" if status == "Failed" else "#6F695E"
        dot = '<span style="color:' + colour + ';">●</span> '
        body.add('<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin-top:22px;border-top:1px solid #E5E0D5;border-collapse:collapse;"><tr><td style="padding-top:18px;vertical-align:top;"><span style="display:inline-block;width:20px;font-family:monospace;font-size:12px;color:#6F695E;">' + str(i + 1) + '</span><b style="font-size:16px;">' + escape(title) + '</b><span style="margin-left:10px;font-size:12px;font-weight:700;color:' + colour + ';">' + dot + escape(status) + '</span></td><td style="padding-top:18px;text-align:right;vertical-align:top;font-size:15px;">' + (money_markup(cost, sgd_size=13) if title in charges else escape(cost)) + "</td></tr></table>", f"{i + 1}. {title} — {status} · {cost}")
        timing = clock(begin) + " – " + clock(end) + " · " + duration(begin, end) if begin and end else "—"
        if i in (3, 4):
            timing += " · Gemini Batch · Gemini input cached " + cache_share(sorting, context, title.lower())
        body.html.append('<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td style="padding-left:20px;">')
        body.paragraph(timing, style=MUTED + "font-size:13px;margin-top:2px;")
        if i == 0:
            source_rows = report.get("collection_rows") or context.get("collection_runs", [])
            def total(key, rows=source_rows):
                values = [r.get(key) for r in rows]
                return f"{sum(values):,}" if values and all(isinstance(v, int) for v in values) else "—"
            body.paragraph(total("unique_seen") + " listings seen · " + total("jds_fetched") + " JDs fetched", bold=True)
            table, styles = [], {}
            for source in source_rows:
                board = str(source.get("board", source.get("BOARD", ""))).lower()
                source_status = source.get("status", source.get("STATUS")) or "—"
                name = BOARD_NAMES.get(board, board.title())
                if source_status not in ("ok", "complete"):
                    styles[len(table)] = "bad"
                    name += " · " + source_status
                    if source_status == "partial" and source.get("fetch_failures") is not None:
                        name += f", {source['fetch_failures']:,} failed"
                cells = []
                for key, old in (("unique_seen", "JOB_LISTINGS_SEEN"), ("new_jobs", "NEW"), ("jds_fetched", "JDS_FETCHED")):
                    value = source.get(key, source.get(old))
                    cells.append("—" if value is None or key == "new_jobs" and board in ("indeed", "linkedin") else value)
                table.append([name, str(source.get("market", source.get("MARKET", "—"))).upper(), *cells])
            if table:
                body.table(["BOARD", "MKT", "SEEN", "NEW", "FETCHED"], table, styles)
                body.paragraph("— Indeed and LinkedIn do not report new listings.", style=MUTED + "font-size:13px;")
        if i == 1 and status.startswith("Done"):
            body.paragraph("Canonical titles done · Grouping done")
        if i == 2:
            outcomes = facts.get("outcomes")
            pulled = sum(r["n"] for r in outcomes) if outcomes is not None else None
            body.paragraph((f"{pulled:,}" if pulled is not None else "—") + " new or changed listings pulled", bold=True)
            body.paragraph("Cleaned listings copied into the sorting database. Earlier answers reused, title and language filters applied, the rest queued for Triage.", style=MUTED + "font-size:13px;")
        if i in (3, 4):
            stage = title.lower()
            items = sorting.get("items", {}).get(stage)
            if items is not None:
                if stage == "triage":
                    body.paragraph("Listings", bold=True, style="font-size:11px;letter-spacing:.1em;text-transform:uppercase;" + MUTED)
                    body.table(["", ""], [[label, facts.get("funnel", {}).get(key, "—")] for label, key in (("Pulled tonight", "pulled"), ("Title filter", "title_filter"), ("Language filter", "language_filter"))])
                    body.paragraph("Distinct inputs", bold=True, style="font-size:11px;letter-spacing:.1em;text-transform:uppercase;" + MUTED)
                funnel = [["Earlier answer reused", facts.get("funnel", {}).get("triage_reused", "—")]] if stage == "triage" else []
                funnel.append(["Inputs sent to Gemini", stage_inputs(sorting, stage)])
                if stage == "triage":
                    passed = len(sorting.get("items", {}).get("extraction", {})) if sorting.get("extraction_prepared") or completed else "—"
                    funnel.append(["Passed to Extraction", passed])
                else:
                    funnel.append(["Successful", sum(v.get("outcome") == "valid" for v in items.values())])
                funnel.extend([
                    ["Inputs retried", sum(v.get("answer_attempts", 0) > 1 for v in items.values())],
                    ["Refused by Gemini", sum(v.get("outcome") == "provider_failed" for v in items.values())],
                    ["Failed twice", sum(v.get("outcome") == "terminal" for v in items.values())],
                ])
                body.table(["", ""], funnel, {len(funnel)-1: "amber"})
        if i == 5 and completed:
            refresh = sorting.get("review_refresh", {})
            outcomes = facts.get("outcomes")
            suppressed = sum(r["n"] for r in outcomes if r["category"] == "suppressed") if outcomes is not None else None
            if refresh.get("status") == "complete":
                body.paragraph("Rules applied · " + (f"{suppressed:,}" if suppressed is not None else "—") + " repeats suppressed · Review rebuilt · Confirmed live at " + clock(refresh.get("confirmed_at")))
            else:
                body.paragraph("Rules applied · " + (f"{suppressed:,}" if suppressed is not None else "—") + " repeats suppressed · Review rebuilt · Confirmed live at —")
            body.html.append("</td></tr></table>")
            body.paragraph("Where tonight's listings ended up", bold=True, style="margin-top:22px;font-size:11px;letter-spacing:.1em;text-transform:uppercase;" + MUTED)
            if outcomes is None:
                body.paragraph("—", style=MUTED)
            else:
                labels = [("In review queue", "in_review_queue"), ("Less interested · outside queue", "less_interested_outside"), ("Ineligible · other", "ineligible_other"), ("Non-tech", "non_tech"), ("Tech, not for me", "tech_not_for_me"), ("Title filter", "title_filter"), ("Ineligible · not English", "not_english"), ("Suppressed", "suppressed"), ("Not sorted", "not_sorted")]
                rows = []
                for label, category in labels:
                    values = [sum(r["n"] for r in outcomes if r["category"] == category and r["market"] == m) for m in MARKETS]
                    rows.append([label, *values, sum(values)])
                unknown = [r for r in outcomes if r["category"] not in {key for _, key in labels}]
                if unknown:
                    values = [sum(r["n"] for r in unknown if r["market"] == m) for m in MARKETS]
                    rows.append(["—", *values, sum(values)])
                totals = [sum(r["n"] for r in outcomes if r["market"] == m) for m in MARKETS]
                rows.append(["Pulled tonight", *totals, sum(totals)])
                body.table(["", *MARKETS, "TOTAL"], rows, {8: "amber", len(rows)-1: "total"})
        if i != 5 or not completed:
            body.html.append("</td></tr></table>")
    body.end()


def subject_details(failed_count, source_problems):
    failed_sources = sum(source_values(r)[2] != "partial" for r in source_problems)
    partial_sources = len(source_problems) - failed_sources
    details = []
    if failed_count and failed_sources:
        details.append(f"{failed_count} listings + {failed_sources} source{'s' if failed_sources != 1 else ''} failed")
    elif failed_count:
        details.append(f"{failed_count} listings failed")
    elif failed_sources:
        details.append(f"{failed_sources} source{'s' if failed_sources != 1 else ''} failed")
    if partial_sources:
        details.append(f"{partial_sources} source{'s' if partial_sources != 1 else ''} partial")
    return details


def render(report):
    """Always display Singapore time; the saved UTC timestamps are never changed."""
    report = {**report, "review_url": review_url(report)}
    sorting = report.get("sorting") or {}
    context = sorting.get("email_context") or {}
    facts = sorting.get("email_facts") or {}
    start = report.get("started_at") or sorting.get("started_at")
    crashed = bool(report.get("process_stopped"))
    finish = report.get("stopped_at") if crashed else report.get("finished_at") or sorting.get("finished_at")
    day = report.get("collection_night") or sorting.get("collection_night")
    label = datetime.combine(datetime.fromisoformat(day).date(), time()).strftime("%a %-d %b") if day else instant(start).astimezone(ZoneInfo("Asia/Singapore")).strftime("%a %-d %b") if start else "—"
    completed = report.get("status") == "complete"
    failed_stage = stage_name(report.get("failed_stage") or sorting.get("failed_stage") or report.get("stage"))
    rows = failure_rows(sorting)
    legacy_failures = sum(count(context.get(s + "_NO_ANSWER")) for s in ("TRIAGE", "EXTRACTION"))
    failed_count = len(rows) or legacy_failures
    collection = report.get("collection_rows") or context.get("collection_runs", [])
    source_problems = list({
        (r.get("board", r.get("BOARD")), r.get("market", r.get("MARKET"))): r
        for r in [*report.get("collection_problems", []), *collection]
        if r.get("status", r.get("STATUS")) not in ("ok", "complete")
    }.values())
    cost_gap = bool(sorting.get("unknown"))
    needs_attention = bool(failed_count or source_problems or cost_gap or report.get("collection_cost_incomplete"))
    grouped = facts.get("review_counts")
    queue_rows = []
    for bucket, title in (("top_picks", "Top picks"), ("in_lane", "In lane"), ("less_interested", "Less interested · in queue")):
        values = [sum(r["n"] for r in grouped if r["market"] == m and r["bucket"] == bucket) for m in MARKETS] if grouped is not None else [None, None, None]
        queue_rows.append([title, *values, sum(values) if grouped is not None else None])
    to_review = [sum(r[i] for r in queue_rows) for i in range(1, 5)] if grouped is not None else [None]*4
    tail = f"{queue_rows[0][-1]:,} Top picks · {to_review[-1]:,} to review" if grouped is not None else "— Top picks · — to review"
    if completed:
        subject = f"{SUBJECT_PREFIX} {'!' if needs_attention else '✓'} {label} · " + ("backfill · " if report.get("backfill") else "") + tail
        details = subject_details(failed_count, source_problems)
        if cost_gap or report.get("collection_cost_incomplete"):
            details.append("cost incomplete")
        if needs_attention and not details:
            details.append("needs attention")
        if details:
            subject += " · " + " · ".join(details)
        hero = "Done · backfill" if report.get("backfill") else "Done · needs attention" if needs_attention else "All done"
        live_at = sorting.get("review_refresh", {}).get("confirmed_at")
        summary = "All 6 steps finished. Review is live from " + clock(live_at) + "."
        if needs_attention:
            summary = "All 6 steps finished and Review is live."
        if report.get("backfill"):
            prior = report.get("prior_failure", {})
            summary = "The first run stopped at " + stage_name(prior.get("stage")) + " at " + clock(prior.get("finished_at")) + ". A backfill finished it at " + clock(finish) + " and Review is live."
        if failed_count:
            summary += f" {failed_count} listing{'s' if failed_count != 1 else ''} failed on {'their' if failed_count != 1 else 'its'} own and {'were' if failed_count != 1 else 'was'} left unsorted."
        if not (live_at and sorting.get("review_refresh", {}).get("status") == "complete"):
            summary = summary.replace("Review is live from —.", "Review live time: —.").replace("Review is live.", "Review live time: —.")
        if source_problems:
            summary += " " + " ".join(source_summary(r) for r in source_problems)
    else:
        subject_error = error_title(report.get("error") or sorting.get("error")).replace("Gemini Batch job failed", "Gemini Batch failed")
        stopped_label = "Cleaning" if failed_stage == STEPS[1] else failed_stage
        subject = f"{SUBJECT_PREFIX} ✕ {label} · FAILED · at {stopped_label} · process exited" if crashed else f"{SUBJECT_PREFIX} ▲ {label} · INCOMPLETE · stopped at {stopped_label} · {subject_error}"
        hero = "Failed · at " + stopped_label if crashed else "Incomplete · stopped at " + stopped_label
        finished = [name for name in STEPS if step_status(report, name, failed_stage) == "Done"]
        indices = [STEPS.index(name)+1 for name in finished]
        prefix = f"Steps 1–{indices[-1]} finished. " if indices and indices == list(range(1,indices[-1]+1)) else ""
        summary = "The process exited during " + failed_stage + " without reporting a reason. Details below are from the last saved checkpoint." if crashed else prefix + "Tonight's results are not in Review."
    body = Body()
    colour = "#F9E3DF" if crashed else "#FFFFFF" if completed else "#FBEBDD"
    mark_colour = "#D23B2C" if crashed else "#DE6F00" if not completed else "#E0991C" if needs_attention else "#1F9A86"
    body.html.append(f'<tr><td style="padding:24px;background:{colour};">')
    body.paragraph("BTO · " + label, style="font-family:monospace;font-size:11px;letter-spacing:.12em;text-transform:uppercase;" + MUTED)
    square = '<span style="display:inline-block;width:12px;height:12px;border-radius:3px;background:' + mark_colour + ';vertical-align:middle;margin:-4px 10px 0 0;"></span>'
    body.add(f'<h1 style="margin:14px 0 0;font-size:28px;line-height:1.15;font-weight:700;letter-spacing:-.02em;">{square}{escape(hero)}</h1>', hero)
    body.paragraph(summary, style="font-size:15px;line-height:1.5;" + MUTED)
    charges = costs(sorting, context)
    if report.get("collection_cost_incomplete"):
        charges["Collection"] = None
    elif report.get("collection_cost_usd") is not None:
        charges["Collection"] = amount(report["collection_cost_usd"])
    known = [v for v in charges.values() if v is not None]
    total = sum(known, Decimal(0)) if known else None
    metrics = [("Start", clock(start)), ("Detected" if crashed else "End" if completed else "Stopped", clock(finish))]
    if completed and report.get("backfill"):
        metrics = [("Start", clock(start)), ("Stopped", clock(report.get("prior_failure", {}).get("finished_at"))), ("Backfill done", clock(finish))]
    elif not crashed:
        metrics.append(("Duration" if completed else "Ran for", duration(start, finish)))
    if not crashed:
        metrics.append(("Total cost" if completed else "Cost so far", money(total if not completed or len(known) == len(charges) else None)))
    body.html.append('<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin-top:20px;border-top:1px solid #E5E0D5;"><tr><td style="padding-top:16px;font-size:0;">')
    for title, value in metrics:
        width = 180 if title in ("Total cost", "Cost so far") else 132
        display = escape(value)
        if " ~S$" in value:
            usd, sgd = value.split(" ", 1)
            display = escape(usd) + ' <span style="font-size:14px;font-weight:400;' + MUTED + '">' + escape(sgd) + '</span>'
        body.add(f'<table role="presentation" cellspacing="0" cellpadding="0" style="display:inline-table;vertical-align:top;width:{width}px;padding-bottom:12px;"><tr><td><span style="font-size:11px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;{MUTED}">{escape(title)}</span><br><b style="font-size:20px;letter-spacing:-.02em;">{display}</b></td></tr></table>', title + ": " + value)
    body.html.append("</td></tr></table>")
    if not crashed and completed:
        body.paragraph(" · ".join(name + " " + money(value) for name, value in charges.items()), style="padding-top:10px;border-top:1px dotted #B5AE9F;font-size:13px;" + MUTED)
    body.end()
    if not completed:
        problem(body, report, failed_stage, crashed=crashed)
    if not crashed:
        body.section("Jobs to review")
        if completed:
            body.table(["", *MARKETS, "TOTAL"], [*queue_rows, ["To review", *to_review]], {0: "top", 3: "total"})
        else:
            body.paragraph("Tonight's results are not in Review yet.", style=MUTED)
        if completed:
            live = report.get("review_url")
            local = report.get("localhost_url") or "http://127.0.0.1:5050/review"
            body.add('<p style="margin:22px 0 0;">' + link(live, "Live Review", button=True, primary=True) + link(local, "Localhost Review", button=True) + "</p>", "Live Review: " + (live or "—") + "\nLocalhost Review: " + local)
        body.end()
    if completed and needs_attention:
        attention(body, report, rows, source_problems, cost_gap=cost_gap, listing_count=failed_count)
    if completed:
        run_steps(body, report, context, charges, failed_stage)
        body.section("Cost")
        body.table(["", ""], [["Gemini pricing", "Batch (half price) for all requests"], ["Gemini input cached", "Triage " + cache_share(sorting, context, "triage") + " · Extraction " + cache_share(sorting, context, "extraction")], ["Gemini recorded this month", money(amount(facts.get("gemini_month_usd")))], ["Apify account this month", money(amount(context.get("SPENT")))]])
        body.end()
        footer = "Gemini input cached is the share of input tokens billed at the cached rate. It is not a discount on the whole bill. Costs are recorded Apify and Gemini charges; Databricks compute and Gemini cache storage are not included. S$ figures use a fixed rate of 1.28."
        body.html.append('<tr><td style="padding:16px 24px 24px;font-size:12px;' + MUTED + '">' + escape(footer) + "</td></tr>")
        body.text.append(footer)
    return body.render(subject)


def review_url(report):
    """Resolve the Review link from the report or private configuration."""
    value = report.get("review_url")
    if not value:
        from bto.settings import _dotenv

        origin = os.environ.get("BTO_REVIEW_REFRESH_URL") or _dotenv().get("BTO_REVIEW_REFRESH_URL")
        value = origin.rstrip("/") + "/review" if origin else None
    try:
        parsed = urlsplit(value or "")
        if parsed.scheme in {"https", "http"} and parsed.hostname and not parsed.username and not parsed.password:
            return value
    except ValueError:
        pass
    return None


def render_sorting_summary(report, validation):
    """Preview a free re-sort without claiming collection, model work or publication."""
    from bto.filter_and_bucket_job_listings import BUCKET_LABELS

    complete = report.get("status") == "complete"
    subject = "[BTO] Filtering and bucketing · " + ("Complete" if complete else "Incomplete")
    body = Body()
    body.section("Filtering and bucketing")
    body.paragraph("Saved answers were re-evaluated without model calls.")
    body.paragraph("Status: " + ("Complete" if complete else "Incomplete"), bold=True)
    body.paragraph("Duration: " + duration(report.get("started_at"), report.get("finished_at")))
    if not complete:
        body.paragraph(error_explanation(report.get("error")) or "Check the saved re-sort checkpoint and service log.")
    rows = []
    for item in validation.get("buckets", []) + [validation.get("suppressed", {})]:
        if not item:
            continue
        counts = item["counts"]
        rows.append([BUCKET_LABELS.get(item["bucket"], item["bucket"].replace("_", " ").title()),
                     *(counts.get(market) for market in MARKETS), counts.get("Total")])
    totals = validation.get("totals", {})
    rows.append(["All listings", *(totals.get(market) for market in MARKETS), totals.get("Total")])
    body.table(["", *MARKETS, "TOTAL"], rows, {len(rows) - 1: "total"})
    body.end()
    return body.render(subject)
