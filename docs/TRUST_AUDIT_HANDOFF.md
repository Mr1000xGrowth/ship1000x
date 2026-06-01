# Trust audit — handoff & remediation map

> Note de passation. Audit statique du repo, **version confirmée à jour** :
> branche `claude/repo-trust-audit-I4NGm` = `origin/main` (dernier commit
> 2026-05-30) + commits d'audit ; 0 retard ; aucune autre branche plus récente.
> Suite tests : 581 passent, 2 échecs purement environnementaux (build wheel +
> anonymisation de chemin avec `home=/root`).

## Contexte validé (ne PAS écraser)

Le travail d'observation des **tokens d'abonnement** est présent et vérifié à la
source — aucune correction ci-dessous ne le réécrit :
- Codex CLI lit `total_token_usage` natif + `auth_mode` oauth (`codex.py:236-249`).
- Codex Desktop parse les vrais tokens de `response.completed`
  (`codex_desktop.py:106-167,341-345`) ; l'horaire n'est qu'un fallback jours-idle.
- Claude Code lit tokens natifs + cache depuis le JSONL.
- 12 collecteurs utilisent `TokenBreakdown` natif ; `cost_truth` /
  subscription-absorbed / unknown-basis en place (CHANGELOG « Unreleased », Wave 4).

Le tarif horaire `$10/h` ne subsiste que sur `codex_macapp` (sans tokens) et
`codex_desktop` (fallback idle) — résidu **by-design**, correctement `indicative`.

## Thème racine

Les signaux de qualité existent mais vivent dans des canaux annexes et ne
s'injectent pas dans la confiance/base affichée à côté du chiffre — surtout
quand ce chiffre **sort du tool**.

## Critère de priorité : le chiffre sort-il du tool ?

| Chiffre | Base | Sort en externe ? |
|---|---|---|
| `highlights` (WOW local) | `lines_real` ✅ `cli.py:2502-2536` | non |
| `compute_multiplier` (« Nx senior ») | `lines_added` **brut** ❌ `multiplier.py:40` | **OUI** → `insights_push.py:181` (S3) + `markdown_report.py:261` |
| Trust Score global (finding A) | attribution | non (CLI/dashboard only) |

Le filtre `share_config` ne protège pas le multiplicateur : `factor_vs_senior` /
`lines_per_hour` ne sont pas « financiers », ils passent même en défaut conservateur.

---

## 🔴 URGENT — chiffre mou exporté

### U1 — Multiplicateur : `lines_added` → `lines_real_added`
- `multiplier.py:40,43,46` utilisent le brut ; `lines_real_added` est déjà dans
  `overview["totals"]` (`engine.py:342`) et déjà utilisé par `highlights`.
- Urgent car seul chiffre mou **poussé en externe** (S3 + Markdown), alimente le
  pitch « facteur vs senior ». Biais brut documenté ≥ +8 %.
- Risque sur la collecte tokens : **nul** (choix de colonne). Effort : ~10 lignes.

### U2 — Le multiplicateur exporté n'a aucun label de confiance
- `compute_multiplier` renvoie des floats nus (`multiplier.py:63-90`).
- Joindre la base (`real`) + caveat benchmark. Effort faible.

---

## 🟠 Axes d'amélioration (différables)

| Axe | Détail | Effort | Statut |
|---|---|---|---|
| **A1** | `confidence_flag` → brancher sur `usage.quality` (généraliser `trace._confidence_from_quality`) ; exposer `project_conf` séparément. Rend le Trust Score honnête. | Moyen + re-backfill `reclassify` | à faire |
| **A2** | Câbler `pricing_freshness().stale` dans le downgrade `cost_quality` (`usage.py`). | Faible | ✅ fait |
| **A3** | Marquer « hypothèse interne » le benchmark `lines_per_hour_no_ai` (`benchmarks.py`) — dénominateur du facteur exporté. | Faible | ✅ fait |
| **A4** | `quality_for_tokens` granulaire (cache manquant → `defensible`). Capture déjà OK (Wave 4) ; seul le label est grossier. | Moyen | à faire |
| **A5** | Repondérer le score global autrement que par `event_count` brut. Conception, pas bug. | Conception | à faire |

## Ordre de remédiation

1. ✅ **U1 + U2** — multiplicateur exporté sur lignes `real` + bande de confiance.
2. ✅ **A2 + A3** — fraîcheur pricing → `cost_quality` ; benchmark marqué hypothèse.
3. **A1** (Trust Score honnête + re-backfill). ← prochaine étape
4. **A4 / A5** (fond, non bloquant).

### Détail A2 (fait)
`usage.py` : quand `pricing_freshness().stale` (carte tarifaire > 60j), un coût
`factual` sur tokens natifs est rétrogradé `defensible` (les taux publiés ont pu
bouger). Dormant aujourd'hui (41j<60) ; testé en forçant `stale` dans
`test_usage.py::test_stale_pricing_downgrades_factual_cost_to_defensible`.

### Détail A3 (fait)
`benchmarks.py` : le commentaire « (industrie) » est remplacé par un bloc
PROVENANCE explicite — les `lines_per_hour_no_ai` sont des hypothèses internes
non sourcées, à citer comme telles ou à remplacer via `config/benchmarks.yaml`.
Cohérent avec `multiplier.confidence.benchmark_source = "internal_assumption"`.

## Findings retirés / requalifiés

- **E** (`$10/h`) : retiré comme « problème » → résidu by-design (2 sources, desktop=fallback).
- **C** : requalifié — la capture cache est OK ; seul le label binaire reste à affiner (A4).
