"""Browse saved Review jobs without starting application preparation."""

PRIORITY_BUCKETS = ("top_picks", "in_lane", "less_interested")


def filter_saved_jobs(jobs, parameters):
    """Apply the saved-job filters and sort by priority or the date added."""
    posted_by = parameters.get("posted_by", "all")
    order = parameters.get("sort", "priority")
    if posted_by not in {"all", "employer", "recruitment"} or order not in {"priority", "added"}:
        raise ValueError("Unknown saved-job filter")
    jobs = [
        job
        for job in jobs
        if (parameters.get("priority") != "1" or job["review_state"].get("starred_at"))
        and (posted_by == "all" or job.get("is_recruitment_firm") is (posted_by == "recruitment"))
    ]
    return sorted(
        jobs,
        key=lambda job: (
            *(
                (
                    job["review_state"].get("starred_at") is None,
                    PRIORITY_BUCKETS.index(job["bucket"])
                    if job.get("bucket") in PRIORITY_BUCKETS
                    else len(PRIORITY_BUCKETS),
                )
                if order == "priority"
                else ()
            ),
            -job["review_state"]["saved_to_apply_at"].timestamp(),
            -job["id"],
        ),
    )
