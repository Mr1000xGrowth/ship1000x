# Architecture — Ports & Adapters

> Document canonique de l'architecture **Ports & Adapters** de ship1000x
> introduite en Wave 2 (mai 2026, tag `v1.0.0`). Pour le contexte
> stratégique complet, voir les notes internes mainteneur non publiées.

## Vue d'ensemble

```
┌─────────────────────────────────────────────────────────────┐
│                    ship-core (OSS)                          │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ Domain : Event, Usage, Cost, AuthMode, Project       │   │
│  │ Contracts canoniques (cost honesty rules)            │   │
│  └─────────────────────────────────────────────────────┘   │
│                                                              │
│  Ports (Protocols) :                                        │
│    SourceCollector   - .collect(...) -> stats dict          │
│    PricingResolver   - .resolve(provider, model) -> PR|None │
│    PIIScrubber       - .scrub(event) -> event               │
│    EventStorage      - .upsert_event / .query / offsets     │
│    ExportSink        - .export(events) -> None              │
└─────────────────────────────────────────────────────────────┘
            ▲                          ▲
            │ implémentent             │
┌───────────────────────┐  ┌────────────────────────────────┐
│  ship-collectors      │  │  ship-pricing + ship-scrubber  │
│  + ship-tap (OSS)     │  │  + ship-storage (OSS)          │
│  - 19 production +    │  │  - LiteLLMResolver             │
│    6 fixture-only     │  │  - RegexScrubber               │
│  - trace-bridge (W4)  │  │  - SQLiteStorage               │
└───────────────────────┘  └────────────────────────────────┘
            ▲                          ▲
┌─────────────────────────────────────────────────────────────┐
│                ship-premium (closed source)                 │
│  - CloudSyncSink, TeamAggregator (ExportSink)               │
│  - PresidioScrubber (PIIScrubber)                           │
│  - PostgresStorage / ClickHouseStorage (EventStorage)       │
│  - LiveBillingResolver (PricingResolver)                    │
└─────────────────────────────────────────────────────────────┘
```

## Les 5 ports

Tous définis dans `ship1000x/ports/` comme `runtime_checkable` Python
``Protocol``. Les adapters n'ont pas besoin de subclasser — la
conformance est structurelle.

### `PricingResolver` — résoudre un modèle vers son rate card

```python
class PricingResolver(Protocol):
    def resolve(
        self,
        provider: str | None,
        model_raw: str | None,
    ) -> PricingResolution | None: ...
```

**Adapter canonique OSS** : `ship1000x.core.pricing_litellm.LiteLLMResolver`
(Wave 1) — lit le snapshot vendoré de `BerriAI/litellm`. Le fallback
sur la table locale est géré par `resolve_model_pricing_hybrid()` dans
`core/pricing.py`.

**Adapter Premium prévu** : `LiveBillingResolver` qui interroge
l'Anthropic / OpenAI Admin API en temps réel.

### `PIIScrubber` — anonymiser un event avant stockage

```python
class PIIScrubber(Protocol):
    def scrub(self, event: dict[str, Any]) -> dict[str, Any]: ...
```

Contrat : **idempotent** et **fail-safe**. Ne raise jamais.

**Adapter canonique OSS** : `ship1000x.core.scrubber.RegexScrubber`
(Wave 2 / Day 3) — délègue à `sanitize_event` (whitelist
`ALLOWED_META_KEYS` + anonymisation de path). Singleton via
`default_scrubber()`.

**Adapter Premium prévu** : `PresidioScrubber` (Wave 6 / Premium) —
détection PII NER multi-langue via Microsoft Presidio.

### `EventStorage` — persister + interroger les events

```python
class EventStorage(Protocol):
    def upsert_event(self, event: dict, replace: bool = False) -> None: ...
    def query(self, sql: str, params: tuple = ()) -> list: ...
    def get_ingestion_offset(self, source: str, key: str) -> int: ...
    def set_ingestion_offset(self, source: str, key: str, value: int, ts: str) -> None: ...
```

**Garantie privacy** : tout adapter de ce port doit appeler le
`PIIScrubber` injecté à l'intérieur de `upsert_event`. La classe
`SQLiteStorage` le fait via `self._get_scrubber().scrub(event)` —
voir `tests/test_storage_central_sanitize_guard.py` pour la
régression suite.

**Adapter canonique OSS** : `ship1000x.core.storage.SQLiteStorage`.
L'alias `Storage = SQLiteStorage` est maintenu pour compatibilité
ascendante.

**Adapters Premium prévus (Wave 5)** : `PostgresStorage`,
`ClickHouseStorage` — backends scalables pour les workloads à plus
grande échelle.

### `SourceCollector` — lire une source locale, émettre des events

```python
class SourceCollector(Protocol):
    def collect(
        self,
        storage: Any,
        classifier: Any,
        privacy_config: dict[str, Any],
    ) -> dict[str, int]: ...
```

Les collectors sont des **modules** avec une fonction `collect`
au niveau module, pas des classes. La signature historique est
préservée.

**Inventaire ship1000x** (validé par `tests/test_all_collectors_implement_source_collector.py`) :

- **19 production collector modules** : agent_runtime, anthropic_usage,
  claude_code, claude_desktop_sessions, claude_statusline, cline, codex,
  codex_desktop, codex_macapp, codex_sqlite, cursor, git_multi, mac_system,
  openai_usage, openclaw, roo_kilo_code, shell, trace, web_exports.
- **6 fixture-only parsers** en attente de promotion Wave 7+ : aider,
  continue_dev, copilot_agents, cursor_agent, gemini_cli, opencode.
- Les modules drop/API opt-in peuvent être des collectors de production sans
  tourner par défaut dans `ship1000x ingest --source all`; ils restent
  invocables explicitement via `ship1000x ingest --source <name>` ou via
  `privacy.yaml`.

### `ExportSink` — publier des events vers une destination

```python
class ExportSink(Protocol):
    def export(self, events: Iterable[dict[str, Any]]) -> None: ...
```

**Statut Wave 2** : le port est défini. Les exporters historiques
(`s3_push.py`, `insights_push.py`, `markdown_report.py`) ont des
interfaces hétérogènes (push de rollups agrégés, pas d'iterable
d'events) et seront refactorés derrière le port dans une session
dédiée — cleanup reporté à Wave 2.5 dans les notes internes
mainteneur.

**Adapters Premium prévus** : `CloudSyncSink` (Garage S3 push
multi-machine), `TeamAggregator` (cohort benchmarks anonymisés),
`SlackAlertSink`, `LinearIntegration`.

## Pourquoi `runtime_checkable` Protocol

- Évite de forcer les classes existantes à subclasser un ABC —
  **zero breaking change** pour les consumers (et pour Premium qui
  importe des classes concrètes).
- Permet `isinstance(adapter, Port)` pour vérifier la conformance —
  testé exhaustivement dans `tests/test_ports_structural.py`.
- Documente le contrat à la définition du port, pas dispersé dans
  les implémentations.

## Privacy invariants

Tous les events qui passent par `EventStorage.upsert_event` sont
scrubbed via le `PIIScrubber` injecté. Cette garantie est verrouillée
par :

1. Le code de `SQLiteStorage.upsert_event` qui appelle
   `self._get_scrubber().scrub(event)` avant tout `INSERT`.
2. Les 5 tests de régression dans
   `tests/test_storage_central_sanitize_guard.py` qui assertent que :
   - Un event "dirty" passé directement à `upsert_event` est scrubbed
     avant d'atteindre la DB.
   - Le `cwd` absolu est anonymisé même sans appel upstream.
   - Aucun collector ne contourne `upsert_event` via SQL direct
     (lint-style scan du code source).
3. Le contrat **idempotent** du `PIIScrubber` qui permet aux 14
   collectors qui appellent `sanitize_event` upstream de continuer
   à le faire sans surcoût (re-scrub produit le même résultat).

## Cost honesty contract

Toute `PricingResolution` retournée par n'importe quel `PricingResolver`
expose :

- `cost.api_equivalent_usd` — coût équivalent API pour ces tokens.
- `cost.billed_estimated_usd` — estimation locale de la part susceptible
  d'être facturée par ce mode d'authentification. Ce n'est pas une facture
  provider; l'invoice-grade truth vient des exports/API de billing et de
  `ship1000x reconcile`.
- `auth_mode` — `oauth` / `api_key` / `unknown`.
- `pricing_source` — l'identifiant de l'adapter qui a résolu (lit
  `litellm.berriai/...` ou `ship1000x.core.pricing`).

Le **moat défendable** de SHIP1000x repose sur cet invariant. Voir le
plan stratégique pour la gradation L0 → L6 (OSS vs Premium).

## Comment ajouter un nouvel adapter

### Nouvel `EventStorage` (par exemple `PostgresStorage` en Premium)

```python
from ship1000x.ports import EventStorage

class PostgresStorage:
    def __init__(self, dsn: str, scrubber=None):
        ...

    def upsert_event(self, event, replace=False):
        scrubbed = self._scrubber.scrub(event)  # ne JAMAIS sauter
        ...  # INSERT INTO events

    def query(self, sql, params=()):
        ...

    def get_ingestion_offset(self, source, key):
        ...

    def set_ingestion_offset(self, source, key, value, ts):
        ...

# Vérification :
assert isinstance(PostgresStorage("dsn"), EventStorage)
```

### Nouveau `SourceCollector`

Créer un module dans `ship1000x/collectors/` avec une fonction :

```python
def collect(storage, classifier, privacy_config) -> dict[str, int]:
    stats = {"files_seen": 0, "events_ingested": 0, "skipped": 0}
    ...
    return stats
```

Ajouter son nom à `ACTIVE_COLLECTORS` dans
`tests/test_all_collectors_implement_source_collector.py` quand le module
expose un runtime `collect()`, ou à `FIXTURE_ONLY_PARSERS` quand il s'agit
d'un parser synthétique non promu. La suite de conformance valide
automatiquement la signature et empêche une promotion implicite.

## Stabilité du contrat

Les 5 ports sont **verrouillés jusqu'en 2027 minimum**. Aucun ajout
de port (pas de `Notifier`, pas d'`AlertSink`) avant cette échéance.

Les évolutions du contrat existant suivent SemVer strict :

- **Patch (v1.0.x)** : ajouts d'adapters, corrections.
- **Minor (v1.x.0)** : ajouts de méthodes optionnelles aux ports
  (ex: `EventStorage.batch_upsert`).
- **Major (v2.0.0)** : suppression / renommage de méthode dans un
  port — n'aura lieu que sur cas d'usage avéré, jamais par confort.

## Historique

L'ancien doc d'architecture (couvrait `v0.1.0` baseline + V1
hardening notes) a été remplacé par ce document canonique à
l'occasion de Wave 2 / Day 9 (release `v1.0.0`).

## Références

- `ship1000x/ports/` — code source des 5 ports.
- `tests/test_ports_structural.py` — conformance check pour les
  adapters canoniques.
- `tests/test_all_collectors_implement_source_collector.py` — gate
  production collector module vs fixture-only parser.
- Plan stratégique privé du mainteneur : notes internes non publiées.
