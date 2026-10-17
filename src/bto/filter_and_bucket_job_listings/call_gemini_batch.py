"""Durable Gemini Batch submission, correlation and recovery over plain HTTP."""

import fcntl
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .call_gemini import MODEL, NoRedirect, ProviderError, is_busy

TERMINAL = {"JOB_STATE_SUCCEEDED", "JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"}


class BatchStopped(RuntimeError):
    """A terminal or safely resumable operational failure, never a fake answer."""


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def save(path, value):
    """Persist before an external side effect; replacement survives process loss."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as output:
        os.chmod(temporary, 0o600)
        json.dump(value, output, ensure_ascii=False, default=str)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def run_owner(directory):
    """Single-host durable run guard, held without a PostgreSQL connection.

    All paid runners use the same canonical directory even for catch-up runs.
    A different state directory cannot bypass an unfinished logical run.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "batch.lock").open("a") as lock:
        os.chmod(lock.name, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BatchStopped("A Batch runner already owns this host") from None
        try:
            yield directory / "batch-active.json"
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


class API:
    def __init__(self, key):
        self.key = key

    def _request(self, route, data=None, method="GET", headers=None, *, upload=False, raw=False):
        prefix = "https://generativelanguage.googleapis.com/"
        url = prefix + ("upload/v1beta/" if upload else "v1beta/") + route
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "x-goog-api-key": self.key,
                **(headers or {}),
            },
        )
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=120) as reply:
                content = reply.read()
                return content if raw else json.loads(content)
        except urllib.error.HTTPError as error:
            raise ProviderError(
                error.code, error.read().decode("utf-8", errors="replace")
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ProviderError(503, "Connection unavailable") from error

    def json(self, route, body=None, method="GET"):
        return self._request(
            route,
            None if body is None else json.dumps(body).encode(),
            method,
            {"Content-Type": "application/json"},
        )

    def upload(self, path, display):
        content = Path(path).read_bytes()
        request = urllib.request.Request(
            "https://generativelanguage.googleapis.com/upload/v1beta/files",
            data=json.dumps({"file": {"displayName": display}}).encode(),
            method="POST",
            headers={
                "x-goog-api-key": self.key,
                "Content-Type": "application/json",
                "X-Goog-Upload-Protocol": "resumable",
                "X-Goog-Upload-Command": "start",
                "X-Goog-Upload-Header-Content-Length": str(len(content)),
                "X-Goog-Upload-Header-Content-Type": "application/jsonl",
            },
        )
        try:
            opener = urllib.request.build_opener(NoRedirect())
            with opener.open(request, timeout=120) as reply:
                url = reply.headers["X-Goog-Upload-URL"]
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme != "https" or parsed.hostname != "generativelanguage.googleapis.com":
                raise BatchStopped("Unexpected Google upload destination")
            request = urllib.request.Request(
                url,
                data=content,
                method="POST",
                headers={
                    "X-Goog-Upload-Offset": "0",
                    "X-Goog-Upload-Command": "upload, finalize",
                    "Content-Length": str(len(content)),
                },
            )
            with opener.open(request, timeout=120) as reply:
                return json.loads(reply.read())["file"]["name"]
        except urllib.error.HTTPError as error:
            raise ProviderError(error.code, "File upload rejected") from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ProviderError(503, "Connection unavailable") from error

    def download(self, name):
        if not name.startswith("files/") or any(c in name for c in "?#"):
            raise BatchStopped("Unexpected Batch result resource")
        # Download is a separate, documented Google API prefix, never a supplied URL.
        request = urllib.request.Request(
            "https://generativelanguage.googleapis.com/download/v1beta/"
            + name
            + ":download?alt=media",
            headers={"x-goog-api-key": self.key},
        )
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=120) as reply:
                return reply.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            raise ProviderError(error.code, "Batch result download failed") from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ProviderError(503, "Connection unavailable") from error


def read_retry(operation, *, wait=time.sleep, interruptions=None):
    """Reconnect reads to the SAME resource; do not relabel it a failed job."""
    for attempt in range(12):
        try:
            return operation()
        except ProviderError as error:
            if interruptions is not None:
                interruptions.append(
                    {
                        "at": timestamp(),
                        "status": error.status,
                        "kind": "local transport"
                        if error.message == "Connection unavailable"
                        else "provider HTTP",
                    }
                )
            if not (error.status == 408 or is_busy(error)) or attempt == 11:
                raise
            wait(min(30 * 2**attempt, 300))


def metadata(operation):
    return operation.get("metadata") or operation


def remote_state(operation):
    value = metadata(operation).get("state")
    if isinstance(value, str):
        value = value.replace("BATCH_STATE_", "JOB_STATE_")
    if value:
        return value
    if operation.get("done"):
        return "JOB_STATE_FAILED" if operation.get("error") else "JOB_STATE_SUCCEEDED"
    return "JOB_STATE_PENDING"


def find_submission(api, display, *, wait=time.sleep):
    matches = []
    page = ""
    while True:
        result = read_retry(lambda page=page: api.json("batches?pageSize=100" + page), wait=wait)
        for operation in result.get("operations", result.get("batches", [])):
            if metadata(operation).get("displayName") == display:
                matches.append(operation)
        token = result.get("nextPageToken")
        if not token:
            break
        page = "&pageToken=" + urllib.parse.quote(token, safe="")
    if len(matches) > 1:
        raise BatchStopped("Multiple remote jobs share the immutable submission identity")
    return matches[0] if matches else None


def submit(api, path, job, *, wait=time.sleep):
    """An ambiguous POST is reconciled, never blindly repeated."""
    if job.get("name"):
        return
    if job.get("status") == "submitting":
        for attempt in range(6):
            operation = find_submission(api, job["display"], wait=wait)
            if operation:
                job.update(
                    name=operation["name"],
                    status=remote_state(operation),
                    duplicate_submission_prevented=True,
                )
                save(path, job)
                return
            wait(30)
        raise BatchStopped(
            "Submission receipt unavailable; reconcile this identity before any resubmission"
        )
    if not job.get("input_file"):
        job["input_file"] = read_retry(
            lambda: api.upload(job["input_path"], job["display"]), wait=wait
        )
        save(path, job)
    body = {
        "batch": {"displayName": job["display"], "inputConfig": {"fileName": job["input_file"]}}
    }
    for attempt in range(5):
        job.update(status="submitting", submission_at=timestamp(), submission_attempt=attempt + 1)
        save(path, job)
        try:
            operation = api.json(f"models/{MODEL}:batchGenerateContent", body, "POST")
        except ProviderError as error:
            # A received quota rejection did not create a job. HTTP 5xx and
            # lost receipts can be ambiguous and MUST go through listing.
            if error.status == 429 and is_busy(error):
                job.update(status="prepared", submission_error="HTTP 429")
                save(path, job)
                if attempt < 4:
                    wait(min(15 * 2**attempt, 120))
                    continue
            elif error.status >= 500 or error.message == "Connection unavailable":
                return submit(api, path, job, wait=wait)
            raise
        name = operation.get("name")
        if not isinstance(name, str) or not name.startswith("batches/"):
            raise BatchStopped("Submission response lacks a Batch identity; do not resubmit")
        job.update(name=name, status=remote_state(operation))
        save(path, job)  # First action after receipt: make the remote ID durable.
        return
    raise BatchStopped("Batch submission quota did not recover")


def poll(api, path, job, *, wait=time.sleep, interval=60):
    while True:
        interruptions = job.setdefault("polling_interruptions", [])
        try:
            operation = read_retry(
                lambda: api.json(job["name"]), wait=wait, interruptions=interruptions
            )
        finally:
            save(path, job)
        state = remote_state(operation)
        meta = metadata(operation)
        job.update(status=state, last_poll_at=timestamp(), remote=operation)
        job.setdefault("remote_create_at", meta.get("createTime"))
        if meta.get("startTime"):
            job["remote_start_at"] = meta["startTime"]
        if state == "JOB_STATE_RUNNING":
            job.setdefault("first_observed_running_at", timestamp())
        if state in TERMINAL:
            job.update(completed_at=timestamp(), remote_end_at=meta.get("endTime"))
        save(path, job)
        if state in TERMINAL:
            return operation
        wait(interval)


def retrieve(api, path, job, operation, *, wait=time.sleep):
    output = operation.get("response") or metadata(operation).get("output") or {}
    if output.get("responsesFile"):
        text = read_retry(lambda: api.download(output["responsesFile"]), wait=wait)
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        inline = output.get("inlinedResponses", {})
        rows = inline if isinstance(inline, list) else inline.get("inlinedResponses", [])
    result_path = Path(path).with_name("results.json")
    save(result_path, rows)
    job.update(
        results_path=str(result_path),
        results_sha256=hashlib.sha256(result_path.read_bytes()).hexdigest(),
        retrieved_at=timestamp(),
    )
    save(path, job)
    return rows


def correlate(rows, expected):
    found = {}
    for row in rows:
        identity = row.get("key") or row.get("metadata", {}).get("key")
        if identity not in expected or identity in found:
            raise BatchStopped("Unknown or duplicate Batch result correlation identity")
        found[identity] = row
    return found
