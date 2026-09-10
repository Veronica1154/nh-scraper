"""Public, cumulative copy of the existing Telegram selection (no new filters)."""

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def save_filtered_jobs(jobs, path="filtered_jobs.json"):
    """Preserve prior records; never copy application data or arbitrary fields.

    Invalid existing state or an interrupted write must not erase the feed.
    Call before marking the incoming listings as seen.
    """
    path = Path(path)
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or data.get("schema_version") != 1
                or not isinstance(data.get("jobs"), list)):
            raise ValueError("Unsupported filtered jobs feed; preserve it for inspection")
    else:
        data = {"schema_version": 1, "jobs": []}
    records = data["jobs"]
    seen = set()
    for record in records:
        if (not isinstance(record, dict)
                or any(not isinstance(record.get(k), str) or not record[k]
                       for k in ("ID", "Title", "Link", "first_seen_at"))
                or record["ID"] in seen):
            raise ValueError("Invalid existing feed record; preserve it for inspection")
        seen.add(record["ID"])
    changed = False
    now = datetime.now(timezone.utc).isoformat()
    for job in jobs:
        if any(not isinstance(job.get(k), str) or not job[k]
               for k in ("ID", "Title", "Link")):
            raise ValueError("Incomplete incoming job record")
        if job["ID"] not in seen:
            records.append({k: job[k] for k in ("ID", "Title", "Link")}
                           | {"first_seen_at": now})
            seen.add(job["ID"])
            changed = True
    if not changed and path.exists():
        return
    # Same directory keeps os.replace atomic on the filesystem.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                         dir=path.parent, delete=False) as output:
            temporary = output.name
            json.dump(data, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            os.unlink(temporary)
