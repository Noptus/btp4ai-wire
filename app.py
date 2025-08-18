# app.py
# BTP4AI Wire — Publisher (Cloud Foundry)
# - Waits until 08:50 Europe/Paris every weekday, then publishes:
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
import hashlib
import time
import threading
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Tuple
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
def build_adaptive_card(title: str, when_local: str, items: List[Dict]) -> Dict:
    """
    Build a Teams-ready Adaptive Card.
    items: list of dicts with keys:
      - source_logo (str)
      - headline (str)
      - meta (str)
      - url (str)
      - btp_angle (str, optional)
    """
    body = [
        {
            "type": "ColumnSet",
            "columns": [
                {
                    "type": "Column",
                    "width": "auto",
                    "items": [
                        {
                            "type": "Image",
                            "url": "https://logo.clearbit.com/sap.com",
                            "size": "Small",
                            "style": "Person",
                            "altText": "BTP4AI Wire"
                        }
                    ]
                },
                {
                    "type": "Column",
                    "width": "stretch",
                    "items": [
                        {"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Large"},
                        {"type": "TextBlock", "text": when_local, "isSubtle": True, "spacing": "None"}
                    ]
                }
            ]
        },
        {"type": "TextBlock", "text": "Top AI headlines", "weight": "Bolder", "size": "Medium", "spacing": "Medium"}
    ]

    for i, it in enumerate(items, start=1):
        body.append({
            "type": "Container",
            "style": "default",
            "selectAction": {"type": "Action.OpenUrl", "title": "Read", "url": it["url"]},
            "items": [
                {
                    "type": "ColumnSet",
                    "columns": [
                        {"type": "Column", "width": "auto",
                         "items": [{"type": "Image", "url": it.get("source_logo", ""), "size": "Small"}]},
                        {"type": "Column", "width": "stretch",
                         "items": [
                             {"type": "TextBlock",
                              "text": f"[{it['headline']}]({it['url']})",
                              "wrap": True, "weight": "Bolder"},
                             {"type": "TextBlock", "text": it.get("meta", ""), "isSubtle": True, "spacing": "None"}
                         ]}
                    ]
                },
                {"type": "TextBlock", "id": f"s_{i}", "text": it.get("btp_angle", ""), "wrap": True, "isVisible": False}
            ],
            "actions": [
                {"type": "Action.OpenUrl", "title": "Read", "url": it["url"]},
                {"type": "Action.ToggleVisibility", "title": "SAP angle", "targetElements": [f"s_{i}"]}
            ]
        })

    # Optional sections — can be filled/changed in code that calls this builder
    body.extend([
        {"type": "TextBlock", "text": "Use case of the day", "weight": "Bolder", "size": "Medium", "spacing": "Medium"},
        {
            "type": "Container", "style": "emphasis", "bleed": True,
            "items": [
                {"type": "TextBlock", "text": "Supplier risk & compliance co-pilot in S/4HANA", "wrap": True, "weight": "Bolder"},
                {"type": "TextBlock", "id": "uc1",
                 "text": "Pull vendor master (S/4HANA), scan contracts (DMS), enrich with news; flag high-risk suppliers with mitigation. BTP AI Core + Workflow.",
                 "wrap": True, "isVisible": False}
            ],
            "actions": [{"type": "Action.ToggleVisibility", "title": "Show details", "targetElements": ["uc1"]}]
        },
        {"type": "TextBlock", "text": "Joke about AI", "weight": "Bolder", "size": "Medium", "spacing": "Medium"},
        {
            "type": "Container", "style": "default",
            "items": [{"type": "TextBlock", "text": "My AI asked for a vacation. I said, “Sure—take some time off-line.”", "wrap": True}]
        },
        {"type": "TextBlock", "text": "Vote the next deep-dive", "weight": "Bolder", "size": "Medium", "spacing": "Medium"},
        {
            "type": "Input.ChoiceSet", "id": "poll_topic", "style": "expanded",
            "choices": [
                {"title": "Joule prompts & guardrails", "value": "joule_prompts"},
                {"title": "Multi-cloud model gateway on BTP", "value": "multicloud_gateway"},
                {"title": "RAG with SAP data (S/4, Signavio, docs)", "value": "rag_sap"}
            ]
        },
        {
            "type": "FactSet",
            "facts": [
                {"title": "Hub:", "value": "EMEA BTP4AI"},
                {"title": "Edition:", "value": "EU/Paris"},
                {"title": "Planned run:", "value": "08:50 every weekday"}
            ]
        }
    ])

    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "msteams": {"width": "Full"},
        "body": body,
        "actions": [
            {"type": "Action.Execute", "title": "Submit vote", "verb": "voteDeepDive",
             "data": {"source": "daily_brief", "date": datetime.now().strftime("%Y-%m-%d")}},
            {"type": "Action.Execute", "title": "Refresh", "verb": "refreshBrief", "data": {"edition": "eu"}},
            {"type": "Action.Submit", "title": "👍 Useful", "data": {"verb": "feedback", "value": "up"}},
            {"type": "Action.Submit", "title": "👎 Not useful", "data": {"verb": "feedback", "value": "down"}}
        ]
    }


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
        title = f"BTP4AI Wire — Daily Brief — {slug[:10]}"
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
    <title>BTP4AI Wire — Daily Brief</title>
    <link>{SITE_URL}</link>
    <description>Daily AI news for SAP EMEA BTP4AI Hub</description>
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
    # 3) Feed from current slugs (ensure newest first)
    slugs = list_card_slugs_from_repo()
    if slug not in slugs:
        slugs = sorted([slug] + slugs, reverse=True)
    feed_xml = generate_feed(slugs)
    github_put_file("docs/feed.rss", feed_xml.encode("utf-8"), "chore: update feed.rss (limit 10)")
    github_put_file("docs/feed.xml", feed_xml.encode("utf-8"), "chore: update feed.xml (limit 10)")


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
def publish_once():
    if not GITHUB_TOKEN:
        raise RuntimeError("Missing GITHUB_TOKEN")
    ensure_docs_structure()

    tz = ZoneInfo(LOCAL_TZ)
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(tz)

    slug = now_utc.strftime("%Y-%m-%dT%H-%M-%SZ")
    path = f"docs/cards/{slug}.json"
    # Idempotency: skip if already published for this exact minute
    if file_exists(path):
        print(f"[publisher] Card already exists for slug {slug}, skipping.")
        return

    when_local = now_local.strftime("%a, %d %b %Y • %H:%M %Z") + " • SAP EMEA"
    items = ai_research_items(when_local)
    card = build_adaptive_card("BTP4AI Wire — Daily Brief", when_local, items)
    commit_card_and_feed(card, slug)
    print(f"[publisher] Published {slug}")


# ========= Scheduler (08:50 weekdays) =========
def _next_weekday(dt_local: datetime) -> datetime:
    d = dt_local + timedelta(days=1)
    while d.weekday() >= 5:  # 5=Sat, 6=Sun
        d += timedelta(days=1)
    return d


def seconds_until_next_run() -> Tuple[int, str]:
    tz = ZoneInfo(LOCAL_TZ)
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(tz)

    target = now_local.replace(hour=RUN_HOUR, minute=RUN_MINUTE, second=0, microsecond=0)

    if now_local.weekday() >= 5:  # weekend -> next Monday
        target = _next_weekday(now_local).replace(hour=RUN_HOUR, minute=RUN_MINUTE, second=0, microsecond=0)
    else:
        if now_local >= target:
            # already past target time today → next weekday
            target = _next_weekday(now_local).replace(hour=RUN_HOUR, minute=RUN_MINUTE, second=0, microsecond=0)
        elif RUN_CATCH_UP and (target - now_local) > timedelta(minutes=1):
            # if catching up, run immediately when app starts earlier than target
            # (i.e., don't wait until 08:50 if you want an immediate run on boot)
            target = now_local + timedelta(seconds=1)

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