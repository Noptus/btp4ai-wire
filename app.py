# app.py
# BTP4AI Wire — Publisher (Cloud Foundry)
# - Waits until 08:50 Europe/Paris every target weekday (default Monday), then publishes:
#   * docs/cards/<ISO>.json  (Adaptive Card)
#   * docs/cards/latest.json (stable pointer)
#   * docs/feed.rss and docs/feed.xml (RSS with embedded card JSON + Base64)
#
# Power Automate (no-premium) can use the RSS trigger and extract the embedded
# Adaptive Card JSON (either from <content:encoded> or from the [CARD_B64] block).

import os
import re
import json
import base64
import copy
import hashlib
import time
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Dict, Tuple, Optional
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify

# ========= CONFIG (env) =========
GITHUB_OWNER   = os.getenv("GITHUB_OWNER", "noptus")
GITHUB_REPO    = os.getenv("GITHUB_REPO", "btp4ai-wire")
BRANCH         = os.getenv("BRANCH", "main")
SITE_URL       = os.getenv("SITE_URL", f"https://{GITHUB_OWNER}.github.io/{GITHUB_REPO}")
MAX_FEED_ITEMS = int(os.getenv("MAX_FEED_ITEMS", "10"))

LOCAL_TZ       = os.getenv("LOCAL_TZ", "Europe/Paris")
RUN_HOUR       = int(os.getenv("RUN_HOUR", "8"))    # 08:50 local
RUN_MINUTE     = int(os.getenv("RUN_MINUTE", "50"))
RUN_CATCH_UP   = os.getenv("RUN_CATCH_UP", "false").lower() in ("1", "true", "yes")
RUN_WEEKDAY    = int(os.getenv("RUN_WEEKDAY", "0"))  # 0=Monday
ENABLE_SCHEDULER = os.getenv("ENABLE_SCHEDULER", "true").lower() in ("1", "true", "yes")

# Perplexity (optional but recommended)
PPLX_API_KEY   = os.getenv("PPLX_API_KEY")          # set to enable research
PPLX_MODEL     = os.getenv("PPLX_MODEL", "sonar")

# GitHub token (required to commit)
GITHUB_TOKEN   = os.getenv("GITHUB_TOKEN", "")

# HTTP/API headers
GH_API = "https://api.github.com"
HEADERS_GH = {
    "Accept": "application/vnd.github+json",
    "Authorization": f"Bearer {GITHUB_TOKEN}",
}
HEADERS_PPLX = {
    "Authorization": f"Bearer {PPLX_API_KEY or ''}",
    "Content-Type": "application/json",
}

# Card template
CARD_TEMPLATE_PATH = Path(__file__).with_name("card_template.json")
_CARD_TEMPLATE_CACHE: Optional[Dict] = None

# ========= Flask =========
app = Flask(__name__)


# ========= GitHub Helpers =========
def github_get(path: str):
    """GET /repos/{owner}/{repo}/{path} (contents, etc.). Returns JSON or None on 404."""
    url = f"{GH_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/{path}"
    r = requests.get(url, headers=HEADERS_GH, timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def github_put_file(path: str, content_bytes: bytes, message: str) -> dict:
    """Create or update a file via the Contents API (PUT)."""
    existing = github_get(f"contents/{path}")
    payload = {
        "message": message,
        "content": base64.b64encode(content_bytes).decode("utf-8"),
        "branch": BRANCH,
    }
    if existing and "sha" in existing:
        payload["sha"] = existing["sha"]
    url = f"{GH_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}"
    r = requests.put(url, headers=HEADERS_GH, json=payload, timeout=60)
    r.raise_for_status()
    return r.json()


def github_delete_file(path: str, message: str) -> None:
    """Delete a file via the Contents API (DELETE)."""
    existing = github_get(f"contents/{path}")
    if not existing or "sha" not in existing:
        return
    payload = {
        "message": message,
        "sha": existing["sha"],
        "branch": BRANCH,
    }
    url = f"{GH_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}"
    r = requests.delete(url, headers=HEADERS_GH, json=payload, timeout=60)
    # 200 = deleted, 204 = already gone
    if r.status_code not in (200, 204):
        r.raise_for_status()


def ensure_docs_structure():
    """Make sure docs/ and docs/cards/ exist by committing empty placeholders."""
    for p in ("docs/.keep", "docs/cards/.keep"):
        try:
            github_put_file(p, b"", f"chore: ensure {p}")
        except requests.HTTPError as e:
            # 409/422 when nothing changes or conflicts → ignore
            if not (e.response is not None and e.response.status_code in (409, 422)):
                raise


def list_card_slugs_from_repo() -> List[str]:
    """Return descending list of slugs (YYYY-MM-DDT...) from docs/cards/*.json in repo."""
    resp = github_get("contents/docs/cards")
    if not resp or isinstance(resp, dict) and resp.get("type") == "file":
        return []
    names = [item["name"] for item in resp if item["name"].endswith(".json") and item["name"] != "latest.json"]
    return sorted([n[:-5] for n in names], reverse=True)


def cleanup_cards_except(keep_slugs: List[str]) -> None:
    """Delete all docs/cards/*.json except the ones listed in keep_slugs."""
    if not keep_slugs:
        return
    resp = github_get("contents/docs/cards")
    if not resp or isinstance(resp, dict) and resp.get("type") == "file":
        return
    keep_set = set(keep_slugs)
    for item in resp:
        name = item.get("name")
        if not name or not name.endswith(".json") or name == "latest.json":
            continue
        slug = name[:-5]
        if slug in keep_set:
            continue
        github_delete_file(f"docs/cards/{name}", f"chore: prune card {slug}")


def get_card_json_text_b64(slug: str) -> Tuple[str, str]:
    """
    Fetch docs/cards/<slug>.json via Contents API.
    Returns (json_text, base64_text). If not found, returns ("{}", "").
    """
    data = github_get(f"contents/docs/cards/{slug}.json")
    if not data or "content" not in data:
        return "{}", ""
    b64 = data["content"].replace("\n", "")
    try:
        js = base64.b64decode(b64).decode("utf-8")
    except Exception:
        js = "{}"
    return js, b64


def file_exists(path: str) -> bool:
    return github_get(f"contents/{path}") is not None


# ========= Card + Feed =========


def _load_card_template() -> Dict:
    """Load adaptive card template once and return a deep copy for mutation."""
    global _CARD_TEMPLATE_CACHE
    if _CARD_TEMPLATE_CACHE is None:
        if not CARD_TEMPLATE_PATH.exists():
            raise FileNotFoundError(f"Missing card template at {CARD_TEMPLATE_PATH}")
        with CARD_TEMPLATE_PATH.open("r", encoding="utf-8") as f:
            _CARD_TEMPLATE_CACHE = json.load(f)
    return copy.deepcopy(_CARD_TEMPLATE_CACHE)


def _apply_mapping_to_string(value: str, mapping: Dict[str, str]) -> str:
    result = value
    for placeholder, replacement in mapping.items():
        result = result.replace(placeholder, replacement)
    return result


def _replace_placeholders(obj, mapping: Dict[str, str]) -> None:
    if isinstance(obj, dict):
        for key, val in obj.items():
            if isinstance(val, str):
                obj[key] = _apply_mapping_to_string(val, mapping)
            else:
                _replace_placeholders(val, mapping)
    elif isinstance(obj, list):
        for index, item in enumerate(obj):
            if isinstance(item, str):
                obj[index] = _apply_mapping_to_string(item, mapping)
            else:
                _replace_placeholders(item, mapping)


def _build_news_container(idx: int, item: Dict) -> Dict:
    block_id = f"s_{idx}"
    return {
        "type": "Container",
        "style": "default",
        "selectAction": {"type": "Action.OpenUrl", "title": "Read", "url": item["url"]},
        "items": [
            {
                "type": "ColumnSet",
                "columns": [
                    {
                        "type": "Column",
                        "width": "auto",
                        "items": [
                            {
                                "type": "Image",
                                "url": item.get("source_logo", ""),
                                "size": "Small"
                            }
                        ]
                    },
                    {
                        "type": "Column",
                        "width": "stretch",
                        "items": [
                            {
                                "type": "TextBlock",
                                "text": f"[{item['headline']}]({item['url']})",
                                "wrap": True,
                                "weight": "Bolder"
                            },
                            {
                                "type": "TextBlock",
                                "text": item.get("meta", ""),
                                "isSubtle": True,
                                "spacing": "None"
                            }
                        ]
                    }
                ]
            },
            {
                "type": "TextBlock",
                "id": block_id,
                "text": item.get("btp_angle", ""),
                "wrap": True,
                "isVisible": False
            }
        ],
        "actions": [
            {"type": "Action.OpenUrl", "title": "Read", "url": item["url"]},
            {"type": "Action.ToggleVisibility", "title": "SAP angle", "targetElements": [block_id]}
        ]
    }


def _news_items_or_placeholder(items: List[Dict]) -> List[Dict]:
    if items:
        return [_build_news_container(i, it) for i, it in enumerate(items, start=1)]
    return [{
        "type": "TextBlock",
        "text": "No curated items available for this week.",
        "isSubtle": True
    }]


def build_adaptive_card(title: str, when_local: str, items: List[Dict]) -> Dict:
    """
    Build a Teams-ready Adaptive Card using the external template.
    items: list of dicts with keys:
      - source_logo (str)
      - headline (str)
      - meta (str)
      - url (str)
      - btp_angle (str, optional)
    """
    card = _load_card_template()
    _replace_placeholders(card, {
        "{{TITLE}}": title,
        "{{WHEN_LOCAL}}": when_local,
    })

    body = card.get("body", [])
    for idx, block in enumerate(body):
        if isinstance(block, dict) and block.get("type") == "Placeholder" and block.get("id") == "NEWS_ITEMS":
            body[idx:idx + 1] = _news_items_or_placeholder(items)
            break
    return card


def generate_feed(slugs_desc: List[str]) -> str:
    """
    Build RSS 2.0 with both:
      - <content:encoded> containing RAW card JSON (CDATA)
      - <description> containing a [CARD_B64]...[/CARD_B64] block (Base64 of JSON)
    """
    slugs_desc = slugs_desc[:MAX_FEED_ITEMS]
    now_http = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    items_xml = []

    for slug in slugs_desc:
        name = f"{slug}.json"
        link = f"{SITE_URL}/cards/{name}"
        title = f"BTP4AI Wire — Weekly Brief — {week_label_from_slug(slug)}"
        guid = hashlib.sha1(name.encode("utf-8")).hexdigest()

        card_json_text, card_json_b64 = get_card_json_text_b64(slug)
        # Fallback if somehow missing
        if not card_json_b64:
            card_json_b64 = base64.b64encode(card_json_text.encode("utf-8")).decode("utf-8")

        description = f"""<![CDATA[
<p>Adaptive Card JSON: <a href="{link}">{name}</a></p>
<p>[CARD_B64]{card_json_b64}[/CARD_B64]</p>
]]>"""

        content_encoded = f"<![CDATA[{card_json_text}]]>"

        items_xml.append(f"""
      <item>
        <title>{title}</title>
        <link>{link}</link>
        <guid isPermaLink="false">{guid}</guid>
        <pubDate>{now_http}</pubDate>
        <description>{description}</description>
        <content:encoded>{content_encoded}</content:encoded>
      </item>""")

    return f'''<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
  xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>BTP4AI Wire — Weekly Brief</title>
    <link>{SITE_URL}</link>
    <description>Weekly AI highlights for SAP EMEA BTP4AI Hub</description>
    <language>en</language>
    <lastBuildDate>{now_http}</lastBuildDate>
{''.join(items_xml)}
  </channel>
</rss>
'''


def commit_card_and_feed(card: Dict, slug: str):
    """Commit the card and refresh the feed (also update latest.json, write both feed.rss and feed.xml)."""
    json_bytes = json.dumps(card, ensure_ascii=False, indent=2).encode("utf-8")
    # 1) Card file
    github_put_file(f"docs/cards/{slug}.json", json_bytes, f"feat: add card {slug}")
    # 2) Stable pointer
    github_put_file("docs/cards/latest.json", json_bytes, "chore: update latest.json")
    # 3) Remove older weekly cards to keep GitHub Pages lean
    cleanup_cards_except([slug])
    # 4) Feed with the current week's card only
    feed_xml = generate_feed([slug])
    github_put_file("docs/feed.rss", feed_xml.encode("utf-8"), "chore: update feed.rss (weekly)")
    github_put_file("docs/feed.xml", feed_xml.encode("utf-8"), "chore: update feed.xml (weekly)")


# ========= Perplexity (news items) =========
def ai_research_items(when_local: str) -> List[Dict]:
    """
    Returns up to 3 items: {source_logo, headline, meta, url, btp_angle}
    If PPLX_API_KEY is missing or call fails, returns a small static set as fallback.
    """
    if not PPLX_API_KEY:
        return _fallback_items(when_local)

    prompt = f"""
Return STRICT JSON ONLY:

{{
  "items": [
    {{
      "source_logo": "https://logo.clearbit.com/<publisher-domain>",
      "headline": "concise enterprise AI headline",
      "meta": "<Publisher> • Europe/Paris local time like 07:10",
      "url": "https://link",
      "btp_angle": "1 short line: why it matters for SAP BTP (AI Core/Joule/security/cost)"
    }}
  ]
}}

Rules:
- 3 items max, published within 24–48h, enterprise-relevant.
- Prefer reputable sources (FT, WSJ, Economist, vendor blogs).
- Valid https URLs only. Use Europe/Paris for the time in 'meta'.
- NO markdown, no commentary outside the JSON.
Context banner (do not include in output): {when_local}
"""
    payload = {
        "model": PPLX_MODEL,
        "messages": [
            {"role": "system", "content": "Be precise and concise. Follow the JSON schema exactly."},
            {"role": "user", "content": prompt}
        ]
    }
    try:
        r = requests.post("https://api.perplexity.ai/chat/completions",
                          headers=HEADERS_PPLX, json=payload, timeout=60)
        r.raise_for_status()
        data = r.json()
        text = data["choices"][0]["message"]["content"]
        # Extract JSON (in case of code fences etc.)
        m = re.search(r"\{.*\}\s*$", text, re.S)
        if not m:
            return _fallback_items(when_local)
        obj = json.loads(m.group(0))
        items = obj.get("items", [])[:3]
        # Basic hygiene
        cleaned = []
        for it in items:
            if not all(k in it for k in ("headline", "url")):
                continue
            cleaned.append({
                "source_logo": it.get("source_logo", "https://logo.clearbit.com/openai.com"),
                "headline": it["headline"],
                "meta": it.get("meta", "Today"),
                "url": it["url"],
                "btp_angle": it.get("btp_angle", "")
            })
        return cleaned or _fallback_items(when_local)
    except Exception:
        return _fallback_items(when_local)


def _fallback_items(when_local: str) -> List[Dict]:
    """Static backup in case Perplexity is unavailable."""
    return [
        {
            "source_logo": "https://logo.clearbit.com/openai.com",
            "headline": "Enterprise AI controls gain traction (audit logs, PII filters, isolation)",
            "meta": f"BTP4AI Wire • {when_local.split('•')[0].strip()}",
            "url": "https://openai.com/enterprise",
            "btp_angle": "Governance patterns map cleanly to Joule + AI Core guardrails."
        },
        {
            "source_logo": "https://logo.clearbit.com/microsoft.com",
            "headline": "Vector search & RAG now standard in enterprise stacks",
            "meta": f"Microsoft Learn • {when_local.split('•')[0].strip()}",
            "url": "https://learn.microsoft.com/azure/search/search-what-is-azure-search",
            "btp_angle": "Ground copilots on SAP data (S/4, docs) with managed vector stores."
        },
        {
            "source_logo": "https://logo.clearbit.com/cloud.google.com",
            "headline": "Long-context models in production: design notes",
            "meta": f"Google Cloud Blog • {when_local.split('•')[0].strip()}",
            "url": "https://cloud.google.com/blog/products/ai-machine-learning",
            "btp_angle": "Contracts/specs use-cases benefit; watch cost/latency."
        }
    ]


# ========= Publish Once =========
def current_week_slug(dt_local: datetime) -> str:
    year, week, _ = dt_local.isocalendar()
    return f"{year}-W{week:02d}"


def current_week_label(dt_local: datetime) -> str:
    monday = dt_local - timedelta(days=dt_local.weekday())
    sunday = monday + timedelta(days=6)
    return f"Week of {monday.strftime('%d %b %Y')} - {sunday.strftime('%d %b %Y')}"


def week_label_from_slug(slug: str) -> str:
    match = re.match(r"^(\d{4})-W(\d{2})$", slug)
    if not match:
        return slug
    year = int(match.group(1))
    week = int(match.group(2))
    try:
        monday = datetime.fromisocalendar(year, week, 1)
        sunday = datetime.fromisocalendar(year, week, 7)
    except ValueError:
        return slug
    return f"Week of {monday.strftime('%d %b %Y')} - {sunday.strftime('%d %b %Y')}"


def publish_once():
    if not GITHUB_TOKEN:
        raise RuntimeError("Missing GITHUB_TOKEN")
    ensure_docs_structure()

    tz = ZoneInfo(LOCAL_TZ)
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(tz)

    slug = current_week_slug(now_local)
    path = f"docs/cards/{slug}.json"
    # Idempotency: skip if already published for this week
    if file_exists(path):
        print(f"[publisher] Card already exists for slug {slug}, skipping.")
        return

    label = current_week_label(now_local)
    when_local = f"{label} • {now_local.strftime('%Z')} • SAP EMEA"
    items = ai_research_items(when_local)
    card = build_adaptive_card("BTP4AI Wire — Weekly Brief", when_local, items)
    commit_card_and_feed(card, slug)
    print(f"[publisher] Published {slug}")


# ========= Scheduler (08:50 weekly) =========
def seconds_until_next_run() -> Tuple[int, str]:
    tz = ZoneInfo(LOCAL_TZ)
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(tz)

    if RUN_CATCH_UP:
        slug = current_week_slug(now_local)
        if not file_exists(f"docs/cards/{slug}.json"):
            return 1, now_local.isoformat()

    days_ahead = (RUN_WEEKDAY - now_local.weekday()) % 7
    target = (now_local + timedelta(days=days_ahead)).replace(
        hour=RUN_HOUR, minute=RUN_MINUTE, second=0, microsecond=0
    )

    if days_ahead == 0 and now_local >= target:
        target = target + timedelta(days=7)

    delta = target.astimezone(timezone.utc) - now_utc
    secs = max(1, int(delta.total_seconds()))
    return secs, target.isoformat()


def scheduler_loop():
    while True:
        secs, next_iso = seconds_until_next_run()
        print(f"[scheduler] Sleeping {secs}s until {next_iso}")
        time.sleep(secs)
        try:
            publish_once()
        except Exception as e:
            print(f"[scheduler] publish_once error: {e}; retry in 600s")
            time.sleep(600)
            try:
                publish_once()
            except Exception as e2:
                print(f"[scheduler] retry failed: {e2}")


# Start scheduler in background (works under gunicorn too)
if ENABLE_SCHEDULER:
    threading.Thread(target=scheduler_loop, daemon=True).start()


# ========= HTTP =========
@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "tz": LOCAL_TZ,
        "run_time": f"{RUN_HOUR:02d}:{RUN_MINUTE:02d}",
        "site_url": SITE_URL,
        "repo": f"{GITHUB_OWNER}/{GITHUB_REPO}"
    })


@app.post("/action/run-now")
def run_now():
    """Manual trigger (useful for testing without waiting)."""
    try:
        publish_once()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500


if __name__ == "__main__":
    # Local dev server
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
