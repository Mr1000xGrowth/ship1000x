# LiteLLM model_prices_and_context_window.json

This directory vendors a snapshot of the LiteLLM model pricing registry,
used by `ship1000x.core.pricing_litellm.LiteLLMResolver` as the primary
pricing source. Falls back to the local table in `ship1000x/core/pricing.py`
for models not present in the snapshot.

## Upstream

- Source : <https://github.com/BerriAI/litellm>
- File   : `model_prices_and_context_window.json`
- License: MIT (BerriAI Inc.)

## Snapshot pin

- Commit : `35f6961526023a89635885194272e894d1b8454f`
- Date   : 2026-05-23
- Entries: 2731

## Why vendored, not pip-installed

We vendor the JSON file directly (mode 2 of the "inspired-by" strategy
documented in the SHIP1000x roadmap) rather than depending on the
`litellm` Python package for three reasons :

1. **Offline-first** — the package brings hundreds of transitive deps
   (httpx, pydantic, openai, anthropic, etc.). We only need the JSON.
2. **Reproducibility** — the snapshot pin is explicit; a pip
   re-resolve cannot silently change our prices behind our back.
3. **Privacy** — no LiteLLM runtime telemetry or proxy calls happen
   at import time.

## Sync workflow

Run `python -m ship1000x.scripts.sync_litellm_prices` to refresh the
snapshot. The script :

1. Fetches the latest commit SHA of the file from the GitHub API.
2. If different from the pinned SHA, downloads the new JSON.
3. Updates this attribution file with the new SHA / date.
4. Prints a diff summary (number of added / changed / removed model
   entries) so the reviewer can spot pricing drift before committing.

Pass `--check` to only compare without writing.

## License notice

LiteLLM is released under the MIT License. Per its terms, we keep the
upstream copyright notice intact :

```
MIT License

Copyright (c) 2023 Berri AI Inc.

Permission is hereby granted, free of charge, to any person obtaining
a copy of this software and associated documentation files (the
"Software"), to deal in the Software without restriction, including
without limitation the rights to use, copy, modify, merge, publish,
distribute, sublicense, and/or sell copies of the Software, and to
permit persons to whom the Software is furnished to do so, subject to
the following conditions:

The above copyright notice and this permission notice shall be
included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
```
