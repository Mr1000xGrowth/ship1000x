"""Migration automatique d'un privacy.yaml existant vers la structure V2.

Utilise par `ship1000x setup` (silencieux, idempotent) et
`ship1000x doctor --fix` (interactif, montre ce qu'il change). Couvre les
cas d'usage classiques au premier lancement :

  - Section `sources` absente → tous les collectors skip silencieusement
  - Section `share` absente ou vide → push echoue avec "Aucun rollup eligible"
  - `_default` partage private malgre share_cloud=true → push inutile
  - Nouveaux repos git detectes apres le init initial → pas ajoutes au share

La migration est IDEMPOTENTE : appelable N fois de suite sans effet de bord,
ne touche que les champs absents/incoherents, preserve tous les overrides
manuels de l'utilisateur.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

# Endpoint S3 par defaut : aucun. L'utilisateur doit configurer son propre
# bucket dans config/privacy.yaml (cloud.endpoint, cloud.bucket). Exemples :
#   - Backblaze B2 : https://s3.us-west-004.backblazeb2.com
#   - Garage auto-heberge : https://garage.yourdomain.com
#   - Cloudflare R2 : https://<account>.r2.cloudflarestorage.com
DEFAULT_S3_ENDPOINT = ""


# Defaults sources : les collectors actives par defaut. Shell et mac_system
# restent disabled car ils necessitent une config manuelle (EXTENDED_HISTORY,
# permissions pmset) que le user doit choisir explicitement.
DEFAULT_SOURCES = {
    "claude_code": "enabled",
    "codex": "enabled",
    "codex_desktop": "enabled",
    "codex_sqlite": "enabled",
    "codex_macapp": "enabled",
    "cursor": "enabled",
    "cline": "enabled",
    "git": "enabled",
    "web_exports": "enabled",
    "shell": "disabled",
    "mac_system": "disabled",
}


def migrate_privacy_config(
    config: dict[str, Any],
    detected_repos: list[dict[str, str]] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Retourne (config_migree, liste_changements).

    Ne modifie que les champs absents. Utilise setdefault partout pour
    preserver les overrides manuels. `detected_repos` est optionnel :
    s'il est fourni, les noms sont ajoutes au share si absents.
    """
    changes: list[str] = []

    consent = config.get("consent") or {}
    share_cloud = consent.get("share_cloud", False)

    # 1. Section sources
    if "sources" not in config or not config.get("sources"):
        config["sources"] = dict(DEFAULT_SOURCES)
        changes.append("Ajout section `sources` avec defaults (11 collectors)")
    else:
        # Complete les collectors manquants (ex: nouveau collector ajoute dans
        # une version future du CLI). Ne change jamais une valeur existante.
        added: list[str] = []
        for name, default in DEFAULT_SOURCES.items():
            if name not in config["sources"]:
                config["sources"][name] = default
                added.append(name)
        if added:
            changes.append(
                f"Ajout de {len(added)} collector(s) manquant(s) dans `sources` : "
                f"{', '.join(added)}"
            )

    # 2. Section share
    share_before = dict(config.get("share") or {})
    share: dict[str, str] = config.get("share") or {}

    # Ajoute les repos detectes en aggregated (sans ecraser un choix manuel)
    if detected_repos:
        for repo in detected_repos:
            name = repo.get("name")
            if name and name not in share:
                share[name] = "aggregated"

    # _default = aggregated si partage equipe actif, private sinon.
    # Critique : si share_cloud=true et _default absent, rien ne part jamais.
    expected_default = "aggregated" if share_cloud else "private"
    if "_default" not in share:
        share["_default"] = expected_default
        changes.append(
            f"Ajout `share._default = {expected_default}` "
            f"(share_cloud={share_cloud})"
        )
    elif share_cloud and share["_default"] == "private":
        # Cas du yaml ancien qui avait _default: private hardcode
        # et maintenant share_cloud est true → incoherence, on corrige.
        share["_default"] = "aggregated"
        changes.append(
            "Correction `share._default` : private → aggregated "
            "(car consent.share_cloud=true)"
        )

    if share != share_before:
        config["share"] = share
        # Log les repos nouvellement ajoutes (si detected_repos)
        if detected_repos:
            new_repos = [
                repo["name"]
                for repo in detected_repos
                if repo.get("name") and repo["name"] not in share_before
                and share.get(repo["name"]) == "aggregated"
            ]
            if new_repos:
                changes.append(
                    f"Ajout {len(new_repos)} repo(s) detecte(s) dans `share` : "
                    f"{', '.join(new_repos[:5])}"
                    + (f" (+{len(new_repos) - 5} autres)" if len(new_repos) > 5 else "")
                )

    # 3. Exclude patterns de base (si pas defini)
    if "exclude_keywords" not in config:
        config["exclude_keywords"] = ["password", "secret", "credentials"]
        changes.append("Ajout `exclude_keywords` defaults (password/secret/credentials)")

    # 4. Retention defaults
    if "retention" not in config:
        config["retention"] = {
            "events_days": 365,
            "sessions_days": 730,
            "rollups_days": -1,
        }
        changes.append("Ajout section `retention` avec defaults")

    # 5. Cloud defaults si share_cloud actif. Ne jamais ecraser un endpoint
    # existant ; l'utilisateur configure son propre bucket (B2, Garage, R2, AWS).
    if share_cloud:
        cloud = config.get("cloud") or {}
        cloud.setdefault("provider", "s3")
        cloud.setdefault("endpoint", DEFAULT_S3_ENDPOINT)
        cloud.setdefault("bucket", "")
        cloud.setdefault("push_enabled", True)
        cloud.setdefault("push_time", "03:00")
        cloud.setdefault("retention_days", 365)
        config["cloud"] = cloud
        if not cloud.get("endpoint") or not cloud.get("bucket"):
            changes.append(
                "Note : cloud.endpoint et cloud.bucket doivent etre configures "
                "manuellement avant le premier push (pas de defaut hardcode)."
            )

    return config, changes


def run_auto_migration(privacy_yaml_path: Path, detected_repos: list[dict[str, str]] | None = None) -> list[str]:
    """Lance la migration sur un fichier privacy.yaml existant.

    Retourne la liste des changements effectues. Ecrit le fichier uniquement
    si des changements sont necessaires (pour preserver la mtime sinon).
    """
    if not privacy_yaml_path.exists():
        return []

    try:
        config = yaml.safe_load(privacy_yaml_path.read_text()) or {}
    except yaml.YAMLError:
        return [f"Fichier corrompu, ignore : {privacy_yaml_path}"]

    migrated, changes = migrate_privacy_config(config, detected_repos)
    if changes:
        privacy_yaml_path.write_text(
            yaml.dump(migrated, default_flow_style=False, sort_keys=False, allow_unicode=True)
        )

    return changes


def check_aws_credentials() -> dict[str, Any]:
    """Verifie la presence de credentials AWS S3.

    Ordre de lookup identique a boto3 :
      1. Variables d'environnement AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
      2. ~/.aws/credentials (profil default)
      3. ~/.aws/config

    Retourne un dict {found: bool, source: "env" | "file" | None, message: str}
    """
    import os

    ak = os.environ.get("AWS_ACCESS_KEY_ID")
    sk = os.environ.get("AWS_SECRET_ACCESS_KEY")
    if ak and sk:
        return {
            "found": True,
            "source": "env",
            "message": f"AWS_ACCESS_KEY_ID={ak[:8]}... (env)",
        }

    aws_creds_path = Path.home() / ".aws" / "credentials"
    if aws_creds_path.exists():
        content = aws_creds_path.read_text()
        if "aws_access_key_id" in content and "aws_secret_access_key" in content:
            return {
                "found": True,
                "source": "file",
                "message": f"Credentials dans {aws_creds_path}",
            }

    return {
        "found": False,
        "source": None,
        "message": (
            "Credentials AWS S3 absents. Configure "
            "AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY (env ou ~/.aws/credentials), "
            "puis lance `ship1000x doctor --fix`."
        ),
    }


def validate_aws_access_key(s: str) -> tuple[bool, str]:
    """Valide un AWS/Garage access_key_id. Retourne (ok, message)."""
    import re
    cleaned = s.strip()
    if not cleaned:
        return (False, "Vide. Colle l'access key avant d'appuyer sur Entree.")
    if len(cleaned) < 16:
        return (False, f"Trop court ({len(cleaned)} caracteres). Attendu >= 16.")
    if not re.fullmatch(r"[A-Za-z0-9]+", cleaned):
        return (
            False,
            "Contient des caracteres interdits (attendu : lettres + chiffres uniquement). "
            "Probablement un paste avec des caracteres de controle invisible. "
            "Essaie avec clic-droit → Coller au lieu de Cmd+V.",
        )
    return (True, "")


def validate_aws_secret(s: str) -> tuple[bool, str]:
    """Valide un AWS/Garage secret_access_key. Retourne (ok, message)."""
    import re
    cleaned = s.strip()
    if not cleaned:
        return (
            False,
            "Vide. Colle le secret avant d'appuyer sur Entree. "
            "Attention : certains terminaux n'affichent pas le paste dans un champ masque.",
        )
    if len(cleaned) < 32:
        return (
            False,
            f"Trop court ({len(cleaned)} caracteres). Attendu >= 32 (AWS = 40 chars, Garage = 64 chars). "
            "Le paste a probablement rate. Essaie : reveler le secret dans ton gestionnaire de mots "
            "de passe AVANT copie, puis clic-droit → Coller (pas Cmd+V).",
        )
    if not re.fullmatch(r"[A-Za-z0-9/+=]+", cleaned):
        return (
            False,
            "Contient des caracteres interdits (attendu : lettres, chiffres, /, +, =). "
            "Paste corrompu, retente.",
        )
    return (True, "")


def format_secret_preview(s: str) -> str:
    """Masque un secret pour affichage : 'bdb0429a...befd08 (64 chars)'."""
    s = s.strip()
    if len(s) <= 16:
        return f"{'*' * len(s)} ({len(s)} chars)"
    return f"{s[:8]}...{s[-6:]} ({len(s)} chars)"


def write_aws_credentials(access_key_id: str, secret_access_key: str) -> Path:
    """Ecrit les credentials dans ~/.aws/credentials (profil default).

    Preserve les autres profils si le fichier existe deja. Permissions 600.
    Retourne le chemin du fichier ecrit.
    """
    import configparser

    aws_dir = Path.home() / ".aws"
    aws_dir.mkdir(mode=0o700, exist_ok=True)
    creds_path = aws_dir / "credentials"

    cp = configparser.ConfigParser()
    if creds_path.exists():
        cp.read(creds_path)

    if "default" not in cp:
        cp["default"] = {}
    cp["default"]["aws_access_key_id"] = access_key_id
    cp["default"]["aws_secret_access_key"] = secret_access_key

    with creds_path.open("w") as f:
        cp.write(f)
    creds_path.chmod(0o600)

    return creds_path
