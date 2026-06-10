"""Classifier : categorise les lignes de code de chaque commit.

4 categories (mutually exclusive, first match wins) :
  - generated : lockfiles, bundles, builds, caches — via patterns ou
                .gitattributes linguist-generated=true
  - vendored  : node_modules/, vendor/, third_party/ — via patterns ou
                .gitattributes linguist-vendored=true
  - seed      : premier commit d'un repo, ou commit massif dont le message
                matche "init|scaffold|import|..." avec volume atypique
  - real      : tout le reste — le "vrai" travail de code

Objectif : remplacer la mesure brute `lines_added` par un chiffre defendable
qui separe le travail humain/IA de l'auto-genere / scaffolding / imports.

Module pur (aucun I/O en dehors du `load_config` / `parse_gitattributes`).
Testable a 100%.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Work-class taxonomy (orthogonal to real/seed/vendored/generated). It splits
# the PRODUCTIVE ("real") lines by the NATURE of the work, so a leverage factor
# can compare code against a code benchmark — and docs/config/data are reported
# as their own volumes rather than inflating a "lines of code" claim.
# `data` is the fallback for any extension not listed below.
DEFAULT_WORK_CLASS_EXTENSIONS: dict[str, list[str]] = {
    "code": [
        "ts", "tsx", "js", "jsx", "mjs", "cjs", "py", "pyi", "go", "rs",
        "java", "kt", "kts", "rb", "php", "c", "cc", "cpp", "cxx", "h",
        "hpp", "cs", "swift", "scala", "sh", "bash", "zsh", "fish", "sql",
        "css", "scss", "sass", "less", "vue", "svelte", "prisma", "graphql",
        "gql", "proto", "lua", "r", "dart", "ex", "exs", "erl", "clj",
        "cljs", "hs", "ml", "mm", "pl", "ps1", "tf",
    ],
    "docs": ["md", "mdx", "rst", "txt", "adoc", "asciidoc", "org", "tex", "rtf"],
    "config": [
        "yaml", "yml", "toml", "ini", "cfg", "conf", "env", "properties",
        "editorconfig", "xml", "plist",
    ],
    # data = fallback (json, csv, srt, svg, html, ...)
}

WORK_CLASS_FALLBACK = "data"
WORK_CLASSES = ("code", "docs", "config", "data")


@dataclass
class LineClassificationConfig:
    generated_patterns: list[str] = field(default_factory=list)
    vendored_patterns: list[str] = field(default_factory=list)
    seed_lines_threshold: int = 5000
    seed_files_threshold: int = 50
    seed_message_regex: str = r"^\s*(init|initial|scaffold|import|bootstrap|seed|first commit|template|fork)"
    seed_always_first_commit: bool = True
    # ext (lowercased, no dot) -> work class. Built from work_class_extensions.
    work_class_by_ext: dict[str, str] = field(default_factory=dict)
    _compiled_message_regex: re.Pattern[str] | None = None

    def compile(self) -> LineClassificationConfig:
        self._compiled_message_regex = re.compile(self.seed_message_regex, re.IGNORECASE)
        if not self.work_class_by_ext:
            self.work_class_by_ext = build_work_class_by_ext(DEFAULT_WORK_CLASS_EXTENSIONS)
        return self

    def message_matches_seed(self, message: str) -> bool:
        if self._compiled_message_regex is None:
            self.compile()
        assert self._compiled_message_regex is not None
        return bool(self._compiled_message_regex.search(message or ""))


def load_config(
    base_config_path: Path, local_override_path: Path | None = None
) -> LineClassificationConfig:
    """Charge les patterns depuis base + local override (concat).

    L'override local ETEND les listes (pas d'ecrasement), sauf pour les
    seuils seed qui prennent la valeur locale si fournie.
    """
    base = _load_yaml_dict(base_config_path)
    config = LineClassificationConfig(
        generated_patterns=list(base.get("generated_patterns") or []),
        vendored_patterns=list(base.get("vendored_patterns") or []),
        seed_lines_threshold=(base.get("seed") or {}).get("lines_threshold", 5000),
        seed_files_threshold=(base.get("seed") or {}).get("files_threshold", 50),
        seed_message_regex=(base.get("seed") or {}).get(
            "message_regex",
            r"^\s*(init|initial|scaffold|import|bootstrap|seed|first commit|template|fork)",
        ),
        seed_always_first_commit=(base.get("seed") or {}).get("always_first_commit", True),
    )

    # Work-class extension map : base config overrides defaults per class;
    # classes absent from config keep their built-in extension list.
    work_class_extensions = dict(DEFAULT_WORK_CLASS_EXTENSIONS)
    base_work_class = base.get("work_class") or {}
    if isinstance(base_work_class, dict):
        for cls, exts in base_work_class.items():
            if isinstance(exts, list):
                work_class_extensions[cls] = list(exts)
    config.work_class_by_ext = build_work_class_by_ext(work_class_extensions)

    if local_override_path and local_override_path.exists():
        local = _load_yaml_dict(local_override_path)
        # Concat pour les listes (ne remplace pas, ajoute)
        config.generated_patterns.extend(local.get("generated_patterns") or [])
        config.vendored_patterns.extend(local.get("vendored_patterns") or [])
        # Work-class : l'override local ETEND chaque classe (ajoute des ext).
        local_work_class = local.get("work_class") or {}
        if isinstance(local_work_class, dict):
            for cls, exts in local_work_class.items():
                for ext in exts or []:
                    config.work_class_by_ext[str(ext).lower().lstrip(".")] = cls
        # Override seuils seed si definis
        seed_local = local.get("seed") or {}
        if "lines_threshold" in seed_local:
            config.seed_lines_threshold = seed_local["lines_threshold"]
        if "files_threshold" in seed_local:
            config.seed_files_threshold = seed_local["files_threshold"]
        if "message_regex" in seed_local:
            config.seed_message_regex = seed_local["message_regex"]
        if "always_first_commit" in seed_local:
            config.seed_always_first_commit = seed_local["always_first_commit"]

    return config.compile()


def build_work_class_by_ext(work_class_extensions: dict[str, list[str]]) -> dict[str, str]:
    """Invert {class: [ext, ...]} into {ext: class} (lowercased, no dot)."""
    out: dict[str, str] = {}
    for work_class, exts in (work_class_extensions or {}).items():
        for ext in exts or []:
            out[str(ext).lower().lstrip(".")] = work_class
    return out


def classify_work_class(path: str, config: LineClassificationConfig) -> str:
    """Return the work class of a file path: code / docs / config / data.

    Extension-based; anything not mapped falls back to ``data`` (json, csv,
    srt, html, svg, ...). This splits productive ("real") lines by the NATURE
    of the work so a code-vs-code leverage factor stays honest and docs/config/
    data are reported as their own volumes.
    """
    mapping = config.work_class_by_ext or build_work_class_by_ext(
        DEFAULT_WORK_CLASS_EXTENSIONS
    )
    base = path.replace("\\", "/").rsplit("/", 1)[-1]
    if "." not in base:
        return WORK_CLASS_FALLBACK
    ext = base.rsplit(".", 1)[-1].lower()
    return mapping.get(ext, WORK_CLASS_FALLBACK)


def _empty_bucket() -> dict[str, int]:
    return {"lines_added": 0, "lines_deleted": 0, "files": 0}


def _load_yaml_dict(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text()) or {}
        return data if isinstance(data, dict) else {}
    except yaml.YAMLError:
        return {}


def parse_gitattributes(repo: Path) -> list[tuple[str, set[str]]]:
    """Lit .gitattributes et retourne [(pattern, {'generated', 'vendored'})].

    Ordre preserve : le premier match gagne (convention git).
    """
    attrs_path = repo / ".gitattributes"
    if not attrs_path.exists():
        return []

    rules: list[tuple[str, set[str]]] = []
    try:
        content = attrs_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []

    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Format : "<pattern> attr1=value attr2=value ..."
        parts = line.split()
        if len(parts) < 2:
            continue
        pattern = parts[0]
        attrs: set[str] = set()
        for attr in parts[1:]:
            if attr == "linguist-generated" or attr == "linguist-generated=true":
                attrs.add("generated")
            elif attr == "linguist-vendored" or attr == "linguist-vendored=true":
                attrs.add("vendored")
            elif attr == "-linguist-generated" or attr == "linguist-generated=false":
                # Negation explicite : marque comme "real" meme si autre
                # regle aurait classe generated
                attrs.add("force_real")
        if attrs:
            rules.append((pattern, attrs))
    return rules


def _matches_any_pattern(path: str, patterns: list[str]) -> bool:
    """fnmatch avec support ** (any depth)."""
    # fnmatch.fnmatch ne gere pas ** comme glob. On traduit en regex.
    for pat in patterns:
        if _glob_match(pat, path):
            return True
    return False


def _glob_match(pattern: str, path: str) -> bool:
    """Match glob pattern contre path. Support ** (n'importe quelle depth)."""
    # Normalise les path separators pour compat cross-platform
    path = path.replace("\\", "/")
    pattern = pattern.replace("\\", "/")

    # Convertit le glob en regex
    # ** → .* (any depth incluant /)
    # *  → [^/]* (any chars sauf /)
    # ?  → . (un seul char)
    regex_parts = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*" and i + 1 < len(pattern) and pattern[i + 1] == "*":
            regex_parts.append(".*")
            i += 2
            # Consomme le / qui suit ** si present
            if i < len(pattern) and pattern[i] == "/":
                i += 1
        elif c == "*":
            regex_parts.append("[^/]*")
            i += 1
        elif c == "?":
            regex_parts.append("[^/]")
            i += 1
        elif c in ".+()[]{}^$|\\":
            regex_parts.append("\\" + c)
            i += 1
        else:
            regex_parts.append(c)
            i += 1
    regex = "^" + "".join(regex_parts) + "$"
    return bool(re.match(regex, path))


def is_generated(
    path: str,
    config: LineClassificationConfig,
    gitattributes_rules: list[tuple[str, set[str]]] | None = None,
) -> bool:
    """True si le fichier matche generated_patterns ou linguist-generated."""
    if gitattributes_rules:
        for pattern, attrs in gitattributes_rules:
            if _glob_match(pattern, path) or fnmatch.fnmatch(path, pattern):
                if "force_real" in attrs:
                    return False
                if "generated" in attrs:
                    return True
    return _matches_any_pattern(path, config.generated_patterns)


def is_vendored(
    path: str,
    config: LineClassificationConfig,
    gitattributes_rules: list[tuple[str, set[str]]] | None = None,
) -> bool:
    """True si le fichier matche vendored_patterns ou linguist-vendored."""
    if gitattributes_rules:
        for pattern, attrs in gitattributes_rules:
            if _glob_match(pattern, path) or fnmatch.fnmatch(path, pattern):
                if "force_real" in attrs:
                    return False
                if "vendored" in attrs:
                    return True
    return _matches_any_pattern(path, config.vendored_patterns)


def is_seed_commit(
    commit_message: str,
    total_lines_added: int,
    files_count: int,
    is_first_commit: bool,
    config: LineClassificationConfig,
) -> bool:
    """True si le commit entier doit etre classe comme seed.

    Regles :
      - Premier commit du repo (max-parents=0) → toujours seed (si active)
      - Sinon : volume atypique (lignes > threshold ET fichiers > threshold)
        ET message matche le regex seed
    """
    if config.seed_always_first_commit and is_first_commit:
        return True
    return bool(total_lines_added >= config.seed_lines_threshold and files_count >= config.seed_files_threshold and config.message_matches_seed(commit_message))


def classify_commit_lines(
    commit_message: str,
    files_with_stats: list[tuple[str, int, int]],
    config: LineClassificationConfig,
    is_first_commit: bool = False,
    gitattributes_rules: list[tuple[str, set[str]]] | None = None,
) -> dict[str, dict[str, int]]:
    """Categorise chaque fichier d'un commit.

    Args:
        commit_message : subject du commit (pour detection seed)
        files_with_stats : [(path, added, deleted)] issus de `git log --numstat -M -C`
        config : patterns charges
        is_first_commit : True si c'est le 1er commit du repo
        gitattributes_rules : issus de parse_gitattributes (cache par repo)

    Returns:
        {
          "real":      {"lines_added": X, "lines_deleted": Y, "files": N},
          "seed":      {"lines_added": X, "lines_deleted": Y, "files": N},
          "vendored":  {"lines_added": X, "lines_deleted": Y, "files": N},
          "generated": {"lines_added": X, "lines_deleted": Y, "files": N},
        }
    """
    result: dict[str, dict[str, int]] = {
        "real":      _empty_bucket(),
        "seed":      _empty_bucket(),
        "vendored":  _empty_bucket(),
        "generated": _empty_bucket(),
        # Decomposition de la categorie "real" par nature de travail. Permet un
        # facteur de levier code-vs-code honnete + un reporting volume pour
        # docs / config / data (jamais compares a un benchmark de code).
        "real_code":   _empty_bucket(),
        "real_docs":   _empty_bucket(),
        "real_config": _empty_bucket(),
        "real_data":   _empty_bucket(),
    }

    total_added = sum(a for _, a, _ in files_with_stats)
    commit_is_seed = is_seed_commit(
        commit_message, total_added, len(files_with_stats), is_first_commit, config
    )

    for path, added, deleted in files_with_stats:
        # Ordre : generated > vendored > seed > real
        if is_generated(path, config, gitattributes_rules):
            category = "generated"
        elif is_vendored(path, config, gitattributes_rules):
            category = "vendored"
        elif commit_is_seed:
            category = "seed"
        else:
            category = "real"

        result[category]["lines_added"] += added
        result[category]["lines_deleted"] += deleted
        result[category]["files"] += 1

        # Sous-classe uniquement le travail productif ("real").
        if category == "real":
            sub = "real_" + classify_work_class(path, config)
            result[sub]["lines_added"] += added
            result[sub]["lines_deleted"] += deleted
            result[sub]["files"] += 1

    return result
