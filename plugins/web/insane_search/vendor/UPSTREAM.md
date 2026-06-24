# Upstream insane-search sync ledger

Vendored from `fivetaku/insane-search` at commit `49306346b59aa89b5e96d98e1104da0890deed72`.

Upstream repository: <https://github.com/fivetaku/insane-search>
Upstream source path copied into Hermes: `skills/insane-search/engine/`
Hermes destination: `plugins/web/insane_search/vendor/insane_search_engine/`
License: MIT, preserved at `plugins/web/insane_search/vendor/LICENSE`.

## Copied upstream paths

- `skills/insane-search/engine/__init__.py`
- `skills/insane-search/engine/__main__.py`
- `skills/insane-search/engine/bias_check.py`
- `skills/insane-search/engine/executor.py`
- `skills/insane-search/engine/fetch_chain.py`
- `skills/insane-search/engine/learning.py`
- `skills/insane-search/engine/phase0.py`
- `skills/insane-search/engine/safety.py`
- `skills/insane-search/engine/templates/.gitignore`
- `skills/insane-search/engine/templates/package.json`
- `skills/insane-search/engine/templates/playwright_mobile_chrome.js`
- `skills/insane-search/engine/templates/playwright_real_chrome.js`
- `skills/insane-search/engine/tests/test_smoke.py`
- `skills/insane-search/engine/tests/test_u1.py`
- `skills/insane-search/engine/tests/test_u4.py`
- `skills/insane-search/engine/tests/test_u5.py`
- `skills/insane-search/engine/tests/test_u7.py`
- `skills/insane-search/engine/transport.py`
- `skills/insane-search/engine/url_transforms.py`
- `skills/insane-search/engine/validators.py`
- `skills/insane-search/engine/waf_detector.py`
- `skills/insane-search/engine/waf_profiles.yaml`

## Rejected upstream wrapper paths

The following upstream paths were intentionally not vendored into the runtime engine namespace for this slice:

- `.claude-plugin/` — Claude Code plugin wrapper metadata and lifecycle hooks are not part of Hermes runtime integration.
- `setup/` — setup/install scripts are excluded because Hermes readiness must be non-mutating and must not install dependencies during tool calls.
- `skills/insane-search/SKILL.md` — Claude-oriented skill prompt is not a Hermes runtime contract.
- `skills/insane-search/references/` — explanatory/reference material is not needed for the vendored engine runtime.
- root README/CHANGELOG/DISCLAIMER/PLATFORMS/assets files — product docs and marketing/assets are not runtime engine code.

## Local patch ledger

- No upstream engine source files were modified during the initial vendor import.
- Added Hermes-owned `plugins/web/insane_search/plugin.yaml` and `plugins/web/insane_search/__init__.py` as inert package/manifest scaffolding only. They do not register live providers, install dependencies, or alter extraction behavior.
- Upstream engine tests are preserved under `vendor/insane_search_engine/tests/` for later adapter/test-porting slices; this slice does not wire them into Hermes test discovery.

## Boundary notes

- No dependency installation was performed.
- No live runtime, gateway, provider, or external service was mutated.
- Upstream auto-install/setup behavior remains excluded from Hermes.
