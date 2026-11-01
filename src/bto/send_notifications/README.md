# send_notifications

Notifications report collection, cleaning and filtering outcomes by email.
The nightly workflow sends one terminal message, using the current HTML design
and a plain-text alternative. Manual collection and cleaning commands can
send their own summaries.

## How nightly notification works

```text
saved collection, cleaning, sorting and Review-publication results
        ↓
collect recorded counts, costs and source facts
        ↓
render the terminal HTML email and plain-text alternative
        ↓
hand the message to the configured SMTP server
        ↓
record delivery status in the shared nightly checkpoint
```

The message distinguishes completion, completion needing attention, interrupted
work and a process crash. A backfill keeps the original collection night and
reports the saved work it resumed. Missing costs and counts remain unavailable
rather than being presented as zero.

## Structure

```text
src/bto/send_notifications/
├── README.md
├── __init__.py
├── notify.py                         # SMTP, manual summaries and the service-failure fallback
├── nightly_email_data.py             # recorded source, listing and cost facts
├── nightly_email.py                  # HTML/plain-text rendering and the free re-sort report
└── nightly_email.html                # shared email page layout
```

`notify.py` also exposes collection handoff reads and bucket reports used by
the workflow. The [PostgreSQL data dictionary](../storage/postgres/data-dictionary.md)
owns stored listing, Review and cost contracts.

## Delivery and recovery

The nightly parent owns the completion or failure email. Its systemd fallback
uses the same checkpoint to retry an undelivered message or report a process
crash. A recorded delivery prevents another send for the same invocation.

Sending fails softly: missing configuration, message-formatting errors and SMTP
failures return `False` without undoing the work being reported. `True` means
the SMTP server accepted the message. Port 465 uses implicit TLS; other ports
use STARTTLS.

Nightly emails use short explanations. Full provider diagnostics stay in the
private checkpoint and service log. The free re-sort command writes a local
report of recorded buckets; it does not send mail or claim that collection,
model processing or Review publication ran.

## Configuration

Keep SMTP settings in the environment or ignored `.env`: `SMTP_HOST`,
`SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD` and `NOTIFY_EMAIL_TO`.
The username supplies the From address.

The live Review link comes from an explicit report URL or private
`BTO_REVIEW_REFRESH_URL`. Without one, the email shows the link as unavailable.
Times are shown in Singapore time. SGD figures are approximate and use the
fixed conversion stated in the email footer.
