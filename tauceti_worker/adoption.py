"""Adopt other authors' inactive pull requests: an opt-in for a project's maintainers.

`TAUCETI_ADOPT_AFTER_HOURS=<hours>` lets this worker rebase an open pull request by another author,
repair its build and fix its review findings, once nothing has changed on it for that many hours:
no new head (the time the `build` status of its head was posted) and no comment, review or label
(GitHub's `updatedAt`). Unset, empty, 0 or unreadable keeps the default: a worker tends only its own
account's pull requests, and bot pull requests on the canonical repository when it can push there.

Each adopted unit gets the usual branch claim, push lease and head check. The survey selects
candidates from the open-PR page (`adoptable`). Before any work, `authorize` re-reads the pull
request and this account's permission and refuses on any doubt: an open, non-draft pull request by
another User account, still at the selected head, against the canonical base branch, with no pause
label, idle for the configured time, that this account may push to (a fork whose author allows
edits by maintainers, or the canonical repository), with push access to the canonical repository.
An adopted pull request is never closed by the worker: closing stays with its author.
"""

import json
import math
import os
import time
from datetime import datetime

from .constants import BASE_BRANCH, TAUCETI
from .github import GitHubError, me
from .github_transport import read_context

ENV = "TAUCETI_ADOPT_AFTER_HOURS"
PAUSE = {"keep", "hold", "wip", "human", "on-hold", "do-not-close"}


def adopt_after_seconds() -> float:
    """The configured inactivity, in seconds; 0 when adoption is off."""
    try:
        hours = float(os.environ.get(ENV, "") or 0)
    except ValueError:
        return 0.0
    return hours * 3600 if math.isfinite(hours) and hours > 0 else 0.0


def adopt_after_label() -> str:
    """The configured inactivity, for logs and prompts ("24 hours")."""
    hours = adopt_after_seconds() / 3600
    return f"{hours:g} hour" + ("" if hours == 1 else "s")


def _epoch(stamp) -> float | None:
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _same(a, b) -> bool:
    return str(a or "").casefold() == str(b or "").casefold()


def adoptable(p, me_login: str, now: float | None = None) -> bool:
    """Whether the survey may tend another author's pull request `p` (a PRInfo) this round."""
    after = adopt_after_seconds()
    if not after or not me_login or not p.author or _same(p.author, me_login) or p.author_is_bot:
        return False
    if {label.lower() for label in p.labels} & PAUSE:
        return False
    if not (p.head_owner and p.head_repo and p.head_ref):
        return False
    updated = _epoch(p.updated_at) if p.updated_at else None
    if updated is None or p.build_status_at is None:
        return False
    now = time.time() if now is None else now
    return now - max(updated, p.build_status_at) >= after


def authorize(gh, pr: int, head: str, p, now: float | None = None) -> bool:
    """Re-read the pull request and this account's permission right before adopting it."""
    after = adopt_after_seconds()
    if not after:
        return False
    try:
        with read_context(fresh=True, direct=True):
            live = json.loads(gh.api_jq(f"repos/{TAUCETI}/pulls/{pr}", ".") or "null")
            if not isinstance(live, dict):
                return False
            source, base, user = live.get("head") or {}, live.get("base") or {}, live.get("user") or {}
            head_repository = (source.get("repo") or {}).get("full_name") or ""
            labels = {str(label.get("name", "")).lower() for label in live.get("labels") or []}
            updated = _epoch(live.get("updated_at"))
            now = time.time() if now is None else now
            if (
                live.get("state") != "open"
                or live.get("draft") is not False
                or user.get("type") != "User"
                or not user.get("login")
                or _same(user.get("login"), me())
                or source.get("sha") != head
                or source.get("ref") != p.head_ref
                or not _same(head_repository, f"{p.head_owner}/{p.head_repo}")
                or base.get("ref") != BASE_BRANCH
                or not _same((base.get("repo") or {}).get("full_name"), TAUCETI)
                or labels & PAUSE
                or updated is None
                or now - updated < after
            ):
                return False
            if not _same(head_repository, TAUCETI) and live.get("maintainer_can_modify") is not True:
                return False
            return gh.api_jq(f"repos/{TAUCETI}", ".permissions.push") == "true"
    except (GitHubError, ValueError, TypeError, AttributeError):
        return False
