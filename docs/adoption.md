# Adopting inactive pull requests

A worker tends its own account's pull requests: it rebases them, repairs their build and fixes
their review findings. A project's maintainer can also let a worker finish other authors' pull
requests once their authors have stopped working on them:

```bash
TAUCETI_ADOPT_AFTER_HOURS=24 tauceti work --loop
```

With this setting, an open pull request by another author becomes one this worker tends once
nothing has changed on it for 24 hours: no new head, and no comment, review or label. The worker
then rebases it, repairs its build or fixes its review findings, like one of its own. Unset, empty,
0 or an unreadable value turns adoption off, which is the default.

Before working on an adopted pull request, the worker reads it again, with this account's
permission, and skips it unless all of these hold:

- it is open, not a draft, authored by a User account other than this one, at the head the worker
  selected, against the canonical repository's base branch;
- it has no `keep`, `hold`, `wip`, `human`, `on-hold` or `do-not-close` label;
- nothing changed on it for the configured time;
- this account can push to its branch: the pull request's author allows edits by maintainers
  (always the case for a branch on the canonical repository), and this account has push access
  to the canonical repository.

The work then follows the usual rules: the branch claim, the push lease and the check that the
head has not moved. The worker adds its commits on top of the author's, on the pull request's own
branch. It never closes an adopted pull request; that stays with its author. A new push or a
comment by the author resets the clock, and a pause label stops adoption at once.

A pull request from a fork owned by an organization cannot allow edits by maintainers, so it is
never adopted.
