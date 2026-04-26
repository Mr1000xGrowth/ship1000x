"""Tests pour core/consent_wizard.py : wizard interactif de selection share level."""

from __future__ import annotations

from rich.console import Console

from ship1000x.core.consent_wizard import (
    ProjectInfo,
    collect_detected_repos,
    find_unclassified_projects,
    merge_project_lists,
    prompt_share_levels,
    suggest_default_level,
)


def _p(pid: str, detection: str = "git_remote", **kwargs) -> ProjectInfo:
    return ProjectInfo(project_id=pid, detection=detection, **kwargs)


def test_suggest_default_local_only_user_returns_private():
    project = _p("gh:foo/bar", detection="git_remote")
    assert suggest_default_level(project, share_cloud=False) == "private"


def test_suggest_default_team_user_aggregates_github_remote():
    project = _p("gh:foo/bar", detection="git_remote")
    assert suggest_default_level(project, share_cloud=True) == "aggregated"


def test_suggest_default_team_user_keeps_local_repos_private():
    # Repo sans remote git public : probablement code client / NDA → private safe.
    project = _p("local:secret-client", detection="local_git")
    assert suggest_default_level(project, share_cloud=True) == "private"

    project = _p("dir:downloads-misc", detection="dir")
    assert suggest_default_level(project, share_cloud=True) == "private"


def test_find_unclassified_returns_only_missing_keys():
    share = {"_default": "private", "gh:foo/bar": "aggregated"}
    db_projects = ["gh:foo/bar", "local:secret-client", "dir:scratch"]
    result = find_unclassified_projects(db_projects, share)
    assert result == ["dir:scratch", "local:secret-client"]


def test_find_unclassified_ignores_default_key():
    # _default ne doit jamais etre traite comme un project_id.
    share = {"_default": "aggregated"}
    assert find_unclassified_projects(["_default", "gh:foo"], share) == ["gh:foo"]


def test_find_unclassified_empty_when_all_classified():
    share = {"_default": "private", "gh:foo": "aggregated", "local:bar": "disabled"}
    assert find_unclassified_projects(["gh:foo", "local:bar"], share) == []


def test_collect_detected_repos_extracts_github_slug():
    detected = [
        {"name": "ship1000x", "path": "/Users/x/ship1000x",
         "remote": "git@github.com:Mr1000xGrowth/ship1000x.git"},
    ]
    projects = collect_detected_repos(detected)
    assert len(projects) == 1
    assert projects[0].project_id == "gh:Mr1000xGrowth/ship1000x"
    assert projects[0].detection == "git_remote"


def test_collect_detected_repos_falls_back_local_when_no_remote():
    detected = [{"name": "scratch", "path": "/Users/x/scratch", "remote": ""}]
    projects = collect_detected_repos(detected)
    assert projects[0].project_id == "local:scratch"
    assert projects[0].detection == "dir"


def test_collect_detected_repos_handles_https_url():
    detected = [
        {"name": "foo", "path": "/Users/x/foo",
         "remote": "https://github.com/owner/foo.git"},
    ]
    projects = collect_detected_repos(detected)
    assert projects[0].project_id == "gh:owner/foo"


def test_merge_project_lists_dedups_and_keeps_highest_signal():
    db = [_p("gh:foo/bar", detection="git_remote", sessions=42, commits=10)]
    detected = [_p("gh:foo/bar", detection="git_remote")]  # 0 sessions/commits
    merged = merge_project_lists(db, detected)
    assert len(merged) == 1
    assert merged[0].sessions == 42
    assert merged[0].commits == 10


def test_merge_project_lists_sorts_by_activity_desc():
    a = _p("gh:a", sessions=1)
    b = _p("gh:b", sessions=10)
    c = _p("gh:c", commits=5)
    merged = merge_project_lists([a, b, c])
    assert [p.project_id for p in merged] == ["gh:b", "gh:c", "gh:a"]


def test_prompt_share_levels_no_projects_returns_share_unchanged(monkeypatch):
    console = Console(file=None, quiet=True)
    current = {"_default": "private", "gh:foo": "aggregated"}
    result = prompt_share_levels([], current, console)
    assert result == current


def test_prompt_share_levels_uses_user_choice(monkeypatch):
    """Mock Prompt.ask pour verifier que la fonction utilise les reponses user."""
    from rich import prompt as rich_prompt

    answers = iter(["aggregated", "disabled"])
    monkeypatch.setattr(rich_prompt.Prompt, "ask", lambda *a, **k: next(answers))

    projects = [
        _p("gh:owner/repo", detection="git_remote"),
        _p("local:secret", detection="local_git"),
    ]
    console = Console(file=None, quiet=True)
    result = prompt_share_levels(projects, {"_default": "private"}, console, share_cloud=True)
    assert result["gh:owner/repo"] == "aggregated"
    assert result["local:secret"] == "disabled"
    assert result["_default"] == "private"


def test_prompt_share_levels_preserves_existing_default(monkeypatch):
    from rich import prompt as rich_prompt

    monkeypatch.setattr(rich_prompt.Prompt, "ask", lambda *a, **k: "private")
    console = Console(file=None, quiet=True)
    projects = [_p("gh:a", detection="git_remote")]
    result = prompt_share_levels(
        projects, {"_default": "aggregated"}, console, share_cloud=True
    )
    assert result["_default"] == "aggregated"
