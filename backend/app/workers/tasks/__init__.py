"""Task implementations. One module per background job (§13)."""

from app.workers.tasks.discover import (  # noqa: F401
    run_deduplicate,
    run_discover,
    run_expire_postings,
    run_fetch_details,
    run_prune_raw,
    run_purge_deleted,
)
from app.workers.tasks.matching import (  # noqa: F401
    run_digest,
    run_embed,
    run_match_users,
    run_verify_urls,
)
from app.workers.tasks.registry import HANDLERS, TaskContext

__all__ = ["HANDLERS", "TaskContext"]
