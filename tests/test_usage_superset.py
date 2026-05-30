"""Capture exhaustive de l'usage + invariant anti-doublon (Claude Code).

Deux garanties verrouillées ici :

1. **Anti-doublon par `message.id`** — quand le même tour assistant
   (même `message.id` Anthropic) apparaît deux fois dans un fichier
   (cas des sessions reprises qui recopient l'historique), ses tokens
   ne sont comptés qu'UNE fois. Empêche le gonflement de volume
   observé en scan brut (doublons ~43 %).

2. **Superset usage Anthropic** — tous les champs exposés par l'API
   sont agrégés par jour (cache 5m/1h, thinking, server tools,
   service_tier), même ceux à 0, pour ne jamais perdre de donnée.
"""

from __future__ import annotations

import json
from pathlib import Path

from ship1000x.collectors.claude_code import parse_session_file
from ship1000x.core.usage import canonicalize_model, format_token_count


def test_format_token_count_adaptive_units():
    assert format_token_count(0) == "0"
    assert format_token_count(950) == "950"
    assert format_token_count(1234) == "1 k"
    assert format_token_count(2_300_000) == "2.3 M"
    assert format_token_count(1_500_000_000) == "1.50 Md"


def test_precise_versions_not_flattened_to_generic():
    """Régression : les versions précises observées (turn_context.model Codex,
    model Claude) NE doivent PAS être rabattues sur un bucket générique plus
    ancien par le fallback substring. Sinon l'usage réel d'un modèle détecté
    devient invisible (fusionné dans gpt-5 / claude-opus-4)."""
    # OpenAI / Codex 2026
    assert canonicalize_model("gpt-5.4") == "gpt-5.4"
    assert canonicalize_model("gpt-5.3-codex") == "gpt-5.3-codex"
    assert canonicalize_model("gpt-5.3-codex-spark") == "gpt-5.3-codex-spark"
    assert canonicalize_model("gpt-5.5") == "gpt-5.5"
    # Le vrai legacy générique reste gpt-5
    assert canonicalize_model("gpt-5") == "gpt-5"
    # Anthropic : la version la plus récente ne doit pas devenir l'ancienne
    assert canonicalize_model("claude-opus-4-8") == "claude-opus-4-8"
    assert canonicalize_model("claude-opus-4-8[1m]") == "claude-opus-4-8"
    assert canonicalize_model("claude-opus-4") == "claude-opus-4"


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")


def _assistant(msg_id: str, ts: str = "2026-05-14T10:00:00.000Z") -> dict:
    return {
        "type": "assistant",
        "timestamp": ts,
        "sessionId": "abc",
        "uuid": f"u-{msg_id}",
        "message": {
            "id": msg_id,
            "model": "claude-opus-4-7",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 2000,
                "cache_creation_input_tokens": 300,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 100,
                    "ephemeral_1h_input_tokens": 200,
                },
                "output_tokens_details": {"thinking_tokens": 40},
                "server_tool_use": {
                    "web_search_requests": 2,
                    "web_fetch_requests": 1,
                },
                "service_tier": "standard",
            },
        },
    }


def test_duplicate_message_id_counted_once(tmp_path):
    """Même message.id deux fois (resume) -> tokens comptés une seule fois."""
    session = tmp_path / "resume.jsonl"
    # msg_1 apparaît 2x (historique recopié), msg_2 une fois (tour neuf).
    _write_jsonl(session, [_assistant("msg_1"), _assistant("msg_1"), _assistant("msg_2")])
    day = next(iter(parse_session_file(session)["daily"].values()))
    # 2 tours uniques x (100+2000+300) = 4800, PAS 7200.
    assert day["tokens_input"] == 4800, day["tokens_input"]
    assert day["tokens_output"] == 100
    assert day["assistant_turns"] == 2


def test_anthropic_usage_superset_captured(tmp_path):
    """Tous les sous-champs Anthropic sont agrégés (cache TTL, thinking, tools, tier)."""
    session = tmp_path / "rich.jsonl"
    _write_jsonl(session, [_assistant("msg_1")])
    day = next(iter(parse_session_file(session)["daily"].values()))
    assert day["tokens_input"] == 2400          # 100 + 2000 + 300
    assert day["cache_read_tokens"] == 2000
    assert day["cache_write_tokens"] == 300
    assert day["cache_write_5m_tokens"] == 100
    assert day["cache_write_1h_tokens"] == 200
    assert day["thinking_tokens"] == 40
    assert day["web_search_requests"] == 2
    assert day["web_fetch_requests"] == 1
    assert day["service_tier"] == "standard"


def test_superset_defaults_zero_when_fields_absent(tmp_path):
    """Un usage minimal (champs avancés absents) -> superset à 0, pas d'erreur."""
    session = tmp_path / "minimal.jsonl"
    _write_jsonl(
        session,
        [
            {
                "type": "assistant",
                "timestamp": "2026-05-14T10:00:00.000Z",
                "sessionId": "abc",
                "uuid": "u1",
                "message": {
                    "id": "m1",
                    "model": "claude-opus-4-7",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            }
        ],
    )
    day = next(iter(parse_session_file(session)["daily"].values()))
    assert day["tokens_input"] == 10
    assert day["cache_write_5m_tokens"] == 0
    assert day["cache_write_1h_tokens"] == 0
    assert day["thinking_tokens"] == 0
    assert day["web_search_requests"] == 0
    assert day["service_tier"] is None
