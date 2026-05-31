# Trust audit — handoff note (confidence_flag vs measurement quality)

> Note de passation pour une prochaine session. Rédigée après analyse statique
> du repo (clone vierge, sans base de données réelle). Suite tests : 581 passent,
> 2 échecs purement liés à l'environnement (build wheel + anonymisation de chemin
> avec `home=/root`).

## TL;DR

Le `confidence_flag` par event — l'unité atomique qui alimente le Trust Score
global (`insights/trust_score.py`) — est, pour les sources de plus fort poids
(`claude_code`, `codex`, `git`), **dérivé de la confiance d'attribution projet**,
alors que `docs/TRUST_SCORE.md:52-59` documente qu'il devrait refléter la
**qualité de mesure** (tokens/cache/coût/lignes). Le chiffre vendu comme
« confiance de mesure » mesure en partie autre chose.

**Correctif recommandé : corriger le CODE, pas la doc** (voir §Décision).

## Constat vérifié à la source

| Fait | Preuve | Certitude |
|---|---|---|
| `confidence_flag` jamais recalculé en aval, stocké tel quel | `core/storage.py:42,359` | 100 % |
| `claude_code` dérive le flag de `conf` (attribution projet) | `collectors/claude_code.py:697` ← `classify_session` ligne 624 | 100 % |
| `codex` idem | `collectors/codex.py:439` ← ligne 397 | 100 % |
| `git_multi` idem | `collectors/git_multi.py:372` | 100 % |
| Les règles doc par collecteur ne matchent aucun des 3 | `docs/TRUST_SCORE.md:52-59` | 100 % |
| La vraie qualité de mesure existe sur le même event | `collectors/claude_code.py:743` → `core/usage.py:build_usage_metadata` → `usage.quality.{tokens,cost,active_time}` | 100 % |
| Un collecteur fait DÉJÀ le bon mapping (template) | `collectors/trace.py:43` `_confidence_from_quality(usage_quality)` | 100 % |

### Nuance importante (le système est hétérogène)

Le `confidence_flag` n'est pas uniformément basé sur l'attribution. Trois familles :

- **Correct / vérité comptable** : `anthropic_usage`, `openai_usage` codent
  `"high"` en dur ; `trace` mappe depuis `usage_quality`. → sémantique « mesure » OK.
- **Attribution / cwd** (le bug) : `claude_code`, `codex`, `git_multi`,
  `claude_statusline`, `cline`, `cursor`, `openclaw`, `codex_macapp`.
- **Placeholder statique** : `aider`, `continue_dev`, `copilot_agents`,
  `cursor_agent`, `gemini_cli`, `opencode`, `roo_kilo_code`, `agent_runtime`
  codent `"medium"` en dur.

→ L'incohérence (mélange de sémantiques sous un même nom de champ) est sans
doute pire qu'une erreur uniforme : le score global agrège des flags qui ne
veulent pas tous dire la même chose.

## Décision : corriger le code

Réconcilier par la doc reviendrait à écrire « le Trust Score mesure la confiance
d'attribution projet, la vraie qualité de mesure est dans une autre commande ».
Or dans les rapports le score est affiché **collé à des chiffres de tokens/coût**
(`docs/TRUST_SCORE.md:176-178`) : une confiance d'attribution à côté d'un montant
est activement trompeuse pour la cible auditeur/CFO du produit. Le doc-fix
sauverait la stabilité au prix de la thèse même du produit.

Le code-fix est une **réconciliation, pas une réécriture** : la donnée correcte
est déjà calculée, et le template (`trace._confidence_from_quality`) existe déjà.

### Forme du correctif

1. Dans chaque collecteur attribution-driven, dériver `confidence_flag` de
   `usage.quality` (min des dimensions pertinentes : tokens/cost pour
   Claude/Codex ; `line_quality` pour git) — généraliser le pattern
   `trace._confidence_from_quality`.
2. Conserver `project_conf` tel quel et l'exposer comme **signal d'attribution
   distinct**, ne plus le déguiser en confiance de mesure.
3. Remettre `docs/TRUST_SCORE.md` en cohérence (elle redevient vraie sans
   changement de fond — c'est ce qu'elle décrit déjà).

### Réserves à traiter dans le correctif

- **Backfill historique** : les events stockés ont un flag figé depuis
  l'attribution → re-backfill nécessaire, mais l'outillage existe
  (`ship1000x reclassify`).
- **Les scores vont bouger** (souvent à la baisse là où l'attribution était
  bonne mais les cache-tokens manquaient). C'est le but. À annoncer au CHANGELOG
  comme correction de fiabilité, pas régression.

## Angles morts à lever avant d'implémenter (NON vérifiés)

1. **Ampleur réelle de l'impact** sur le score d'un utilisateur : dépend de la
   distribution `event_count` (pondération du global score, `trust_score.py:105`).
   Nécessite une vraie base de données. Non mesurable sur clone vierge.
2. **Surface d'affichage** : confirmer que le nombre vu par l'utilisateur est
   bien `compute_global_score` et pas une voie alternative dans
   `insights/engine.py` ou `web/app.py`. Module canonique tracé, surfaces non
   exhaustivement vérifiées.
3. **Intention design** : vérifier (git blame / historique / discussions) qu'il
   n'y a pas eu un choix délibéré d'utiliser l'attribution comme proxy. Aucune
   doc trouvée le justifiant, mais non infirmé.

## Prochaine session — point de départ

- [ ] Lever les 3 angles morts ci-dessus (surtout #1 sur une vraie base).
- [ ] Généraliser `_confidence_from_quality` aux collecteurs attribution-driven.
- [ ] Exposer `project_conf` comme signal d'attribution séparé.
- [ ] Mettre à jour `docs/TRUST_SCORE.md` + tests + note CHANGELOG.
- [ ] Re-backfill via `ship1000x reclassify`.
