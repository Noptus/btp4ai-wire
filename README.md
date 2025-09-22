# btp4ai-wire

Weekly automation that assembles the BTP4AI Wire adaptive card and RSS feed backed by GitHub Pages.

## How it works

- Scheduler targets 08:50 Europe/Paris every Monday (`RUN_WEEKDAY=0` by default).
- `publish_once()` creates a `docs/cards/<year>-W<week>.json` adaptive card, refreshes `latest.json`, and regenerates an RSS feed that only advertises the current week's card.
- Previous card JSON files get deleted after each successful publish so GitHub Pages stays lean.
- The adaptive card layout lives in `card_template.json`; modify that file to tweak static sections (logo, use case, poll, etc.).

## Running locally

```bash
export GITHUB_OWNER=<github-user>
export GITHUB_REPO=<repository>
export GITHUB_TOKEN=<fine-grained-personal-access-token>
# optional: export ENABLE_SCHEDULER=0 to skip the background scheduler when running scripts
# optional: export RUN_CATCH_UP=true to run immediately if this week's card is missing
python -m flask --app app.py run
# or trigger once without waiting for the scheduler
python - <<'PY'
from app import publish_once
publish_once()
PY
```

The fallback headlines are used automatically when no Perplexity API key is set, ensuring a valid adaptive card is always produced for the current week.

## Customising the template

- Update `card_template.json` to adjust copy, sections, or actions without touching Python code.
- Keep the `{{TITLE}}` and `{{WHEN_LOCAL}}` placeholders so the publisher can inject weekly metadata.
- The news headline placeholder (`{"type": "Placeholder", "id": "NEWS_ITEMS"}`) is replaced at runtime; leave it in place unless you also update `build_adaptive_card`.
