"""Tests du classifier de lignes (real/seed/vendored/generated)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ship1000x.core.line_classifier import (
    LineClassificationConfig,
    _glob_match,
    classify_commit_lines,
    is_generated,
    is_seed_commit,
    is_vendored,
    load_config,
    parse_gitattributes,
)


# ---- Fixtures ----

@pytest.fixture
def default_config() -> LineClassificationConfig:
    """Config realiste avec les patterns par defaut du projet."""
    return LineClassificationConfig(
        generated_patterns=[
            "**/package-lock.json",
            "**/yarn.lock",
            "**/*.min.js",
            "**/dist/**",
            "**/build/**",
            "**/.next/**",
            "**/__pycache__/**",
            "**/prisma/migrations/**/migration.sql",
        ],
        vendored_patterns=[
            "**/node_modules/**",
            "**/vendor/**",
            "**/third_party/**",
        ],
        seed_lines_threshold=5000,
        seed_files_threshold=50,
    ).compile()


# ---- Tests glob matching ----

class TestGlobMatch:
    def test_simple_filename(self):
        assert _glob_match("package-lock.json", "package-lock.json")

    def test_double_star_any_depth(self):
        assert _glob_match("**/package-lock.json", "package-lock.json")
        assert _glob_match("**/package-lock.json", "sub/package-lock.json")
        assert _glob_match("**/package-lock.json", "a/b/c/package-lock.json")

    def test_double_star_directory(self):
        assert _glob_match("**/node_modules/**", "node_modules/foo/bar.js")
        assert _glob_match("**/node_modules/**", "apps/web/node_modules/foo.js")
        assert _glob_match("**/dist/**", "dist/main.js")
        assert _glob_match("**/dist/**", "apps/front/dist/main.js")

    def test_single_star_not_crossing_slash(self):
        assert _glob_match("*.min.js", "app.min.js")
        assert not _glob_match("*.min.js", "sub/app.min.js")

    def test_extension_match(self):
        assert _glob_match("**/*.min.js", "dist/app.min.js")
        assert _glob_match("**/*.min.js", "app.min.js")

    def test_non_matching(self):
        assert not _glob_match("**/package-lock.json", "src/app.ts")
        assert not _glob_match("**/node_modules/**", "src/module/file.ts")


# ---- Tests is_generated ----

class TestIsGenerated:
    def test_lockfile_at_root(self, default_config):
        assert is_generated("package-lock.json", default_config)

    def test_lockfile_nested(self, default_config):
        assert is_generated("apps/web/package-lock.json", default_config)

    def test_min_js(self, default_config):
        assert is_generated("dist/app.min.js", default_config)

    def test_dist_folder(self, default_config):
        assert is_generated("dist/chunks/main.js", default_config)

    def test_next_build(self, default_config):
        assert is_generated(".next/server/pages/index.js", default_config)

    def test_prisma_migration_auto(self, default_config):
        assert is_generated("prisma/migrations/20240101/migration.sql", default_config)

    def test_pycache(self, default_config):
        assert is_generated("app/__pycache__/main.cpython-311.pyc", default_config)

    def test_real_code_not_generated(self, default_config):
        assert not is_generated("src/App.tsx", default_config)
        assert not is_generated("lib/utils.py", default_config)

    def test_gitattributes_generated_overrides(self, default_config):
        rules = [("src/generated/*.ts", {"generated"})]
        assert is_generated("src/generated/api.ts", default_config, rules)
        assert not is_generated("src/real/app.ts", default_config, rules)

    def test_gitattributes_force_real_wins(self, default_config):
        # Un fichier dans dist/ serait generated par pattern,
        # mais .gitattributes force_real l'empeche
        rules = [("dist/manual.js", {"force_real"})]
        assert not is_generated("dist/manual.js", default_config, rules)


# ---- Tests is_vendored ----

class TestIsVendored:
    def test_node_modules(self, default_config):
        assert is_vendored("node_modules/react/index.js", default_config)

    def test_vendor_nested(self, default_config):
        assert is_vendored("apps/web/vendor/lib/x.rb", default_config)

    def test_third_party(self, default_config):
        assert is_vendored("third_party/lib/x.c", default_config)

    def test_real_code_not_vendored(self, default_config):
        assert not is_vendored("src/App.tsx", default_config)


# ---- Tests is_seed_commit ----

class TestIsSeedCommit:
    def test_first_commit_always_seed(self, default_config):
        assert is_seed_commit("any message", 10, 1, True, default_config)

    def test_massive_with_init_message(self, default_config):
        assert is_seed_commit(
            "Initial import from old repo",
            total_lines_added=10_000,
            files_count=100,
            is_first_commit=False,
            config=default_config,
        )

    def test_small_with_init_message_not_seed(self, default_config):
        # Message seed mais volume normal → PAS seed
        assert not is_seed_commit(
            "init auth module",
            total_lines_added=200,
            files_count=5,
            is_first_commit=False,
            config=default_config,
        )

    def test_massive_with_normal_message_not_seed(self, default_config):
        # Volume enorme mais message non-seed → PAS seed (refactor)
        assert not is_seed_commit(
            "Big refactor of auth system",
            total_lines_added=10_000,
            files_count=100,
            is_first_commit=False,
            config=default_config,
        )

    def test_scaffold_message(self, default_config):
        assert is_seed_commit(
            "scaffold Next.js app",
            total_lines_added=8_000,
            files_count=80,
            is_first_commit=False,
            config=default_config,
        )


# ---- Tests classify_commit_lines ----

class TestClassifyCommitLines:
    def test_pure_real_commit(self, default_config):
        result = classify_commit_lines(
            "feat: add login",
            [("src/Login.tsx", 50, 5), ("src/utils.ts", 20, 2)],
            default_config,
        )
        assert result["real"]["lines_added"] == 70
        assert result["real"]["lines_deleted"] == 7
        assert result["real"]["files"] == 2
        assert result["generated"]["lines_added"] == 0
        assert result["seed"]["lines_added"] == 0
        assert result["vendored"]["lines_added"] == 0

    def test_mixed_commit_with_lockfile(self, default_config):
        result = classify_commit_lines(
            "feat: install react-query",
            [
                ("src/queries.ts", 100, 0),
                ("package.json", 2, 0),
                ("package-lock.json", 15_000, 0),
            ],
            default_config,
        )
        assert result["real"]["lines_added"] == 102  # src + package.json
        assert result["generated"]["lines_added"] == 15_000
        assert result["real"]["files"] == 2
        assert result["generated"]["files"] == 1

    def test_first_commit_all_seed(self, default_config):
        result = classify_commit_lines(
            "Initial commit",
            [
                ("src/app.ts", 500, 0),
                ("README.md", 50, 0),
                ("package-lock.json", 20_000, 0),  # generated
                ("node_modules/react/index.js", 1000, 0),  # vendored
            ],
            default_config,
            is_first_commit=True,
        )
        # generated et vendored passent AVANT seed, donc seed ne contient
        # que les fichiers non-generated/vendored
        assert result["seed"]["lines_added"] == 550  # src + README
        assert result["generated"]["lines_added"] == 20_000
        assert result["vendored"]["lines_added"] == 1000
        assert result["real"]["lines_added"] == 0

    def test_refactor_big_not_seed(self, default_config):
        result = classify_commit_lines(
            "refactor: migrate to v2 API",
            [
                ("src/api.ts", 3000, 2500),
                ("src/client.ts", 2500, 2000),
            ],
            default_config,
            is_first_commit=False,
        )
        # Grosse commit mais message non-seed → reste real
        assert result["real"]["lines_added"] == 5500
        assert result["seed"]["lines_added"] == 0

    def test_empty_commit(self, default_config):
        result = classify_commit_lines("merge", [], default_config)
        assert result["real"]["lines_added"] == 0
        assert result["generated"]["lines_added"] == 0

    def test_totals_match_input(self, default_config):
        files = [
            ("src/a.ts", 100, 10),
            ("package-lock.json", 5000, 200),
            ("node_modules/x.js", 300, 0),
        ]
        result = classify_commit_lines("feat: add x", files, default_config)
        total_added = sum(a for _, a, _ in files)
        total_deleted = sum(d for _, _, d in files)
        added_sum = sum(c["lines_added"] for c in result.values())
        deleted_sum = sum(c["lines_deleted"] for c in result.values())
        assert added_sum == total_added
        assert deleted_sum == total_deleted

    def test_files_count_match_input(self, default_config):
        files = [("a.ts", 10, 0), ("b.ts", 20, 0), ("package-lock.json", 100, 0)]
        result = classify_commit_lines("feat", files, default_config)
        assert sum(c["files"] for c in result.values()) == 3


# ---- Tests parse_gitattributes ----

class TestParseGitattributes:
    def test_generated_attr(self, tmp_path):
        (tmp_path / ".gitattributes").write_text(
            "src/api.ts linguist-generated=true\n"
            "# comment\n"
            "*.min.js linguist-generated\n"
        )
        rules = parse_gitattributes(tmp_path)
        assert len(rules) == 2
        assert rules[0] == ("src/api.ts", {"generated"})
        assert rules[1] == ("*.min.js", {"generated"})

    def test_vendored_attr(self, tmp_path):
        (tmp_path / ".gitattributes").write_text("lib/** linguist-vendored\n")
        rules = parse_gitattributes(tmp_path)
        assert rules[0] == ("lib/**", {"vendored"})

    def test_force_real(self, tmp_path):
        (tmp_path / ".gitattributes").write_text("dist/manual.js -linguist-generated\n")
        rules = parse_gitattributes(tmp_path)
        assert rules[0] == ("dist/manual.js", {"force_real"})

    def test_empty_file(self, tmp_path):
        (tmp_path / ".gitattributes").write_text("")
        assert parse_gitattributes(tmp_path) == []

    def test_missing_file(self, tmp_path):
        assert parse_gitattributes(tmp_path) == []


# ---- Tests load_config ----

class TestLoadConfig:
    def test_load_base_only(self, tmp_path):
        base = tmp_path / "classification.yaml"
        base.write_text(
            "generated_patterns:\n"
            "  - '**/*.lock'\n"
            "vendored_patterns:\n"
            "  - '**/vendor/**'\n"
            "seed:\n"
            "  lines_threshold: 3000\n"
        )
        cfg = load_config(base)
        assert cfg.generated_patterns == ["**/*.lock"]
        assert cfg.vendored_patterns == ["**/vendor/**"]
        assert cfg.seed_lines_threshold == 3000

    def test_local_override_extends(self, tmp_path):
        base = tmp_path / "classification.yaml"
        local = tmp_path / "classification.local.yaml"
        base.write_text(
            "generated_patterns:\n"
            "  - '**/*.lock'\n"
            "seed:\n"
            "  lines_threshold: 5000\n"
        )
        local.write_text(
            "generated_patterns:\n"
            "  - 'custom/generated/**'\n"
            "seed:\n"
            "  lines_threshold: 10000\n"
        )
        cfg = load_config(base, local)
        # Concat
        assert "**/*.lock" in cfg.generated_patterns
        assert "custom/generated/**" in cfg.generated_patterns
        # Override seuil
        assert cfg.seed_lines_threshold == 10_000
