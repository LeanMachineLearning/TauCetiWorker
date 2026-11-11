#!/usr/bin/env python3
"""Adopting other authors' inactive PRs is opt-in, waits for inactivity, and is re-authorized live."""

import importlib
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
survey_mod = importlib.import_module("tauceti_worker.survey")
adoption = importlib.import_module("tauceti_worker.adoption")
from tauceti_worker.constants import TAUCETI
from tauceti_worker.github import GitHubError

NOW = time.time()
HOUR = 3600


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def pr(
    number, author, *, idle_h=30, updated_h=None, bot=False, owner="alice", failed=True, conflicting=False, labels=()
):
    """A raw open-PR page entry whose head was built `idle_h` hours ago and last updated `updated_h`."""
    return {
        "number": number,
        "headRefOid": f"head-{number}",
        "headRefName": "feature",
        "headRepositoryOwner": {"login": owner},
        "headRepository": {"name": "LMLExplore"},
        "statusCheckRollup": [
            {"context": "build", "state": "FAILURE" if failed else "SUCCESS", "startedAt": iso(NOW - idle_h * HOUR)}
        ],
        "author": {"login": author, "is_bot": bot},
        "mergeable": "CONFLICTING" if conflicting else "MERGEABLE",
        "updatedAt": iso(NOW - (idle_h if updated_h is None else updated_h) * HOUR),
        "labels": [{"name": name} for name in labels],
    }


RAW = [
    pr(1, "me"),  # our own red PR: always tended
    pr(2, "alice"),  # another author's red PR, idle 30 h
    pr(3, "alice", idle_h=2),  # pushed 2 h ago
    pr(4, "alice", failed=False, conflicting=True),  # green but conflicting, idle 30 h
    pr(5, "alice", labels=("hold",)),  # paused by a label
    pr(6, "app/some-bot", bot=True, owner="external"),  # bots keep their own rule
    pr(7, "alice", updated_h=1),  # head idle 30 h, but commented on an hour ago
]


class Counters:
    def read(self, _name):
        return 0


def classify(env):
    saved = survey_mod.me, survey_mod.can_push, os.environ.get(adoption.ENV)
    survey_mod.me = lambda: "me"
    survey_mod.can_push = lambda _repo: False
    if env is None:
        os.environ.pop(adoption.ENV, None)
    else:
        os.environ[adoption.ENV] = env
    try:
        sv = survey_mod.survey(
            SimpleNamespace(wid="test"),
            SimpleNamespace(open_prs=lambda: RAW),
            None,
            Counters(),
            deep=False,
        )
        return {
            "adopted": sorted(sv.adopted),
            "rebase": [c.pr for c in sv.rebaseable.actionable],
            "fix-ci": [c.pr for c in sv.red_ci.actionable],
        }
    finally:
        survey_mod.me, survey_mod.can_push = saved[0], saved[1]
        if saved[2] is None:
            os.environ.pop(adoption.ENV, None)
        else:
            os.environ[adoption.ENV] = saved[2]


def live_pr(**changes):
    live = {
        "state": "open",
        "draft": False,
        "maintainer_can_modify": True,
        "user": {"login": "alice", "type": "User"},
        "head": {"sha": "head-2", "ref": "feature", "repo": {"full_name": "alice/LMLExplore", "fork": True}},
        "base": {"ref": "main", "repo": {"full_name": TAUCETI}},
        "labels": [],
        "updated_at": iso(NOW - 30 * HOUR),
    }
    live.update(changes)
    return live


def authorized(live, *, push="true", env="24", p=None, error=False):
    """authorize() against a faked live read; returns (result, GitHub calls)."""
    p = p or SimpleNamespace(head_ref="feature", head_owner="alice", head_repo="LMLExplore", author="alice")

    def api(path, _jq):
        if error:
            raise GitHubError("API unavailable")
        if path == f"repos/{TAUCETI}/pulls/2":
            return json.dumps(live)
        if path == f"repos/{TAUCETI}":
            return push
        raise AssertionError(path)

    gh = SimpleNamespace(api_jq=Mock(side_effect=api))
    saved = adoption.me, os.environ.get(adoption.ENV)
    adoption.me = lambda: "me"
    if env is None:
        os.environ.pop(adoption.ENV, None)
    else:
        os.environ[adoption.ENV] = env
    try:
        return adoption.authorize(gh, 2, "head-2", p, now=NOW), gh.api_jq.call_count
    finally:
        adoption.me = saved[0]
        if saved[1] is None:
            os.environ.pop(adoption.ENV, None)
        else:
            os.environ[adoption.ENV] = saved[1]


def launch():
    """The paid launch re-authorizes an adopted PR, refunds a refused attempt, and says so in the prompt."""
    import tempfile
    from unittest.mock import patch

    from tauceti_worker import work_units
    from tauceti_worker.github import GitHub

    p = survey_mod.PRInfo.from_json(pr(2, "alice", failed=False, conflicting=True))
    sv = SimpleNamespace(open_prs=[p], adopted=[2], me_login="me")
    c = survey_mod.Candidate(2, "head-2", rebase_budget_key="rebase-pr-2")
    count = {"rebase-pr-2": 0}
    counters = SimpleNamespace(
        read=lambda k: count.get(k, 0),
        write=lambda k, v: count.update({k: v}),
        incr=lambda k: count.update({k: count.get(k, 0) + 1}),
    )
    push = {"permission": "false"}
    gh = GitHub()
    gh.issue_comments = Mock(return_value=[])
    gh.pr_progress_state = Mock(return_value=dict(head="head-2", state="OPEN", isDraft=False, labels=[], ncomments=0))

    def api(path, _jq):
        if path == f"repos/{TAUCETI}/pulls/2":
            return json.dumps(live_pr())
        if path == f"repos/{TAUCETI}":
            return push["permission"]
        raise AssertionError(path)

    gh.api_jq = Mock(side_effect=api)
    w = SimpleNamespace(
        gh=gh,
        counters=counters,
        rs=SimpleNamespace(bust=Mock()),
        claims=SimpleNamespace(
            begin_branch_work=Mock(return_value=True),
            held=("branch/2", "claims"),
            stop_heartbeat=Mock(),
            release=Mock(),
        ),
    )
    opts = SimpleNamespace(agent_name="Codex")
    import subprocess

    real_run, real_popen = subprocess.run, subprocess.Popen

    def offline(real):
        # Every GitHub read the launch makes is faked above: a live `gh` call is a test bug.
        def call(argv, *args, **kwargs):
            if isinstance(argv, (list, tuple)) and argv and Path(str(argv[0])).name == "gh":
                raise AssertionError(f"live GitHub call: {argv[:4]}")
            return real(argv, *args, **kwargs)

        return call

    with (
        patch.object(subprocess, "run", offline(real_run)),
        patch.object(subprocess, "Popen", offline(real_popen)),
        tempfile.TemporaryDirectory() as directory,
        patch.dict(os.environ, {adoption.ENV: "24"}),
        patch.object(adoption, "me", lambda: "me"),
        patch.object(work_units, "run_in_bubble", return_value=0) as agent,
    ):
        w.cfg = SimpleNamespace(state=Path(directory))
        refused = work_units.do_rebase(w, sv, c, opts, True)
        first = (refused, count["rebase-pr-2"], w.claims.begin_branch_work.called, agent.called)
        push["permission"] = "true"
        rc = work_units.do_rebase(w, sv, c, opts, True)
        prompt = agent.call_args.args[2] if agent.called else ""
        second = (
            rc,
            agent.call_args.kwargs.get("allow_push") if agent.called else None,
            "This PR is by @alice" in prompt,
            "You may NOT close this PR" in prompt,
        )
    return [
        ("launch: a refused adoption is not claimed and refunds its attempt", first, (None, 0, False, False)),
        (
            "launch: an adopted PR runs on its own branch, with the adoption note",
            second,
            (0, "alice/LMLExplore", True, True),
        ),
    ]


def main():
    off = {"adopted": [], "rebase": [], "fix-ci": [1]}
    checks = [
        ("survey: unset", classify(None), off),
        ("survey: 0", classify("0"), off),
        ("survey: unreadable", classify("soon"), off),
        ("survey: 24 h", classify("24"), {"adopted": [2, 4], "rebase": [4], "fix-ci": [1, 2]}),
        ("survey: 48 h", classify("48"), off),
    ]
    canonical = SimpleNamespace(head_ref="feature", head_owner=TAUCETI.split("/")[0], head_repo=TAUCETI.split("/")[1])
    auth = [
        ("authorize: fork allowing maintainer edits", authorized(live_pr())[0], True),
        ("authorize: fork refusing maintainer edits", authorized(live_pr(maintainer_can_modify=False))[0], False),
        ("authorize: head moved", authorized(live_pr(head={**live_pr()["head"], "sha": "other"}))[0], False),
        ("authorize: no push access", authorized(live_pr(), push="false")[0], False),
        ("authorize: hold label", authorized(live_pr(labels=[{"name": "hold"}]))[0], False),
        ("authorize: updated an hour ago", authorized(live_pr(updated_at=iso(NOW - HOUR)))[0], False),
        ("authorize: our own PR", authorized(live_pr(user={"login": "me", "type": "User"}))[0], False),
        ("authorize: a bot's PR", authorized(live_pr(user={"login": "x[bot]", "type": "Bot"}))[0], False),
        ("authorize: draft", authorized(live_pr(draft=True))[0], False),
        ("authorize: closed", authorized(live_pr(state="closed"))[0], False),
        (
            "authorize: other base branch",
            authorized(live_pr(base={"ref": "dev", "repo": {"full_name": TAUCETI}}))[0],
            False,
        ),
        (
            "authorize: branch on canonical, no maintainer flag needed",
            authorized(
                live_pr(
                    maintainer_can_modify=False,
                    head={"sha": "head-2", "ref": "feature", "repo": {"full_name": TAUCETI, "fork": False}},
                ),
                p=SimpleNamespace(**vars(canonical), author="alice"),
            )[0],
            True,
        ),
        ("authorize: API failure", authorized(live_pr(), error=True)[0], False),
        ("authorize: adoption off reads nothing", authorized(live_pr(), env=None), (False, 0)),
    ]
    failures = 0
    for name, got, want in checks + auth + launch():
        ok = got == want
        print(f"[{'OK ' if ok else 'XX '}] {name}: got={got!r} want={want!r}")
        failures += not ok
    print(f"\n{'PASS' if not failures else 'FAIL'}: {failures} mismatch(es)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
