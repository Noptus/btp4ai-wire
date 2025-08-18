import os, re, json, base64, hashlib, time, threading
from datetime import datetime, timezone, timedelta
from typing import List, Dict
from zoneinfo import ZoneInfo

import requests
from flask import Flask, request, jsonify

# ======== CONFIG (env) ========
GITHUB_OWNER   = os.getenv("GITHUB_OWNER", "noptus")
GITHUB_REPO    = os.getenv("GITHUB_REPO", "btp4ai-wire")
SITE_URL       = os.getenv("SITE_URL", f"https://{GITHUB_OWNER}.github.io/{GITHUB_REPO}")
BRANCH         = os.getenv("BRANCH", "main")
MAX_FEED_ITEMS = int(os.getenv("MAX_FEED_ITEMS", "10"))     # keep 10 entries max
LOCAL_TZ       = os.getenv("LOCAL_TZ", "Europe/Paris")      # run clock
RUN_HOUR       = int(os.getenv("RUN_HOUR", "8"))            # 08:50 local
RUN_MINUTE     = int(os.getenv("RUN_MINUTE", "50"))

PPLX_API_KEY   = os.getenv("PPLX_API_KEY")                  # Perplexity
PPLX_MODEL     = os.getenv("PPLX_MODEL", "sonar")

GITHUB_TOKEN   = os.getenv("GITHUB_TOKEN")                  # GitHub PAT
# ==============================

GH_API = "https://api.github.com"
HEADERS_GH = {
    "Accept": "application/vnd.github+json",
    "Authorization": f"Bearer {GITHUB_TOKEN or ''}",
}
HEADERS_PPLX = {
    "Authorization": f"Bearer {PPLX_API_KEY or ''}",
    "Content-Type": "application/json",
}

app = Flask(__name__)

# ---------- GitHub helpers ----------
def github_get(path):
    r = requests.get(f"{GH_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/{path}", headers=HEADERS_GH, timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()

def github_put_file(path, content_bytes: bytes, message: str):
    existing = github_get(f"contents/{path}")
    payload = {
        "message": message,
        "content": base64.b64encode(content_bytes).decode("utf-8"),
        "branch": BRANCH,
    }
    if existing and "sha" in existing:
        payload["sha"] = existing["sha"]
    r = requests.put(f"{GH_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}",
                     headers=HEADERS_GH, json=payload, timeout=60)
    r.raise_for_status()
    return r.json()

def ensure_docs_structure():
    # Create placeholder files so Pages /docs path exists
    for p in ["docs/.keep", "docs/cards/.keep"]:
        try:
            github_put_file(p, b"", f"chore: ensure {p}")
        except requests.HTTPError as e:
            if not (e.response is not None and e.response.status_code in (409, 422)):
                raise

def list_card_slugs_from_repo() -> List[str]:
    resp = github_get("contents/docs/cards")
    if not resp or isinstance(resp, dict) and resp.get("type") == "file":
        return []
    names = [item["name"] for item in resp if item["name"].endswith(".json")]
    return sorted([n[:-5] for n in names], reverse=True)  # newest first (ISO filename)

def file_exists(path: str) -> bool:
    return github_get(f"contents/{path}") is not None

# ---------- Card + RSS ----------
def build_adaptive_card(title: str, when_local: str, items: List[Dict]) -> Dict:
    body = [
        {
            "type":"ColumnSet",
            "columns":[
                {"type":"Column","width":"auto","items":[
                    {"type":"Image","url":"https://logo.clearbit.com/sap.com","size":"Small","style":"Person","altText":"BTP4AI Wire"}
                ]},
                {"type":"Column","width":"stretch","items":[
                    {"type":"TextBlock","text": title, "weight":"Bolder","size":"Large"},
                    {"type":"TextBlock","text": when_local, "isSubtle": True, "spacing":"None"}
                ]}
            ]
        },
        {"type":"TextBlock","text":"Top AI headlines","weight":"Bolder","size":"Medium","spacing":"Medium"}
    ]
    for i, it in enumerate(items, 1):
        body.append({
            "type":"Container","style":"default","items":[
                {"type":"ColumnSet","columns":[
                    {"type":"Column","width":"auto","items":[{"type":"Image","url":it.get("source_logo",""),"size":"Small"}]},
                    {"type":"Column","width":"stretch","items":[
                        {"type":"TextBlock","text":it["headline"],"wrap":True,"weight":"Bolder"},
                        {"type":"TextBlock","text":it.get("meta",""),"isSubtle":True,"spacing":"None"}
                    ]}
                ]},
                {"type":"TextBlock","id":f"s_{i}","text":it.get("btp_angle",""),"wrap":True,"isVisible":False}
            ],
            "actions":[
                {"type":"Action.OpenUrl","title":"Read","url":it["url"]},
                {"type":"Action.ToggleVisibility","title":"SAP angle","targetElements":[f"s_{i}"]}
            ]
        })
    return {
        "type":"AdaptiveCard",
        "$schema":"http://adaptivecards.io/schemas/adaptive-card.json",
        "version":"1.5",
        "msteams":{"width":"Full"},
        "body": body
    }

def generate_feed(slugs_desc: List[str]) -> str:
    slugs_desc = slugs_desc[:MAX_FEED_ITEMS]
    now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    items_xml = []
    for slug in slugs_desc:
        name = f"{slug}.json"
        link = f"{SITE_URL}/cards/{name}"
        title = f"BTP4AI Wire — Daily Brief — {slug[:10]}"
        desc_html = f'<p>Adaptive Card JSON: <a href="{link}">{name}</a></p>'
        guid = hashlib.sha1(name.encode("utf-8")).hexdigest()
        items_xml.append(f"""
      <item>
        <title>{title}</title>
        <link>{link}</link>
        <guid isPermaLink="false">{guid}</guid>
        <pubDate>{now}</pubDate>
        <description><![CDATA[{desc_html}]]></description>
      </item>""")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>BTP4AI Wire — Daily Brief</title>
    <link>{SITE_URL}</link>
    <description>Daily AI news for SAP EMEA BTP4AI Hub</description>
    <language>en</language>
    <lastBuildDate>{now}</lastBuildDate>
{''.join(items_xml)}
  </channel>
</rss>
"""

def commit_card_and_feed(card: Dict, slug: str):
    # commit card
    json_bytes = json.dumps(card, ensure_ascii=False, indent=2).encode("utf-8")
    path = f"docs/cards/{slug}.json"
    github_put_file(path, json_bytes, f"feat: add card {slug}")

    # refresh feed (max 10)
    slugs = list_card_slugs_from_repo()
    if slug not in slugs:
        slugs = sorted([slug] + slugs, reverse=True)
    feed_xml = generate_feed(slugs)
    github_put_file("docs/feed.xml", feed_xml.encode("utf-8"), "chore: update feed.xml (limit 10)")

# ---------- Perplexity ----------
def ai_research_items(today_local_str: str) -> List[Dict]:
    if not PPLX_API_KEY:
        raise RuntimeError("Missing PPLX_API_KEY")
    prompt = f"""
Return STRICT JSON ONLY:
{{
  "items": [
    {{
      "source_logo": "https://logo.clearbit.com/<publisher-domain>",
      "headline": "concise headline",
      "meta": "<Publisher> • time like 07:10 Europe/Paris",
      "url": "https://link",
      "btp_angle": "1 line: why it matters for SAP BTP"
    }}
  ]
}}
Rules: 3 items max; enterprise AI; reputable sources; fresh (24–48h); valid https URLs; Europe/Paris times. No markdown.
"""
    payload = {
        "model": PPLX_MODEL,
        "messages": [
            {"role": "system", "content": "Be precise and concise. Follow the JSON schema exactly."},
            {"role": "user", "content": prompt}
        ]
    }
    r = requests.post("https://api.perplexity.ai/chat/completions",
                      headers=HEADERS_PPLX, json=payload, timeout=60)
    r.raise_for_status()
    data = r.json()
    text = data["choices"][0]["message"]["content"]
    m = re.search(r"\{.*\}\s*$", text, re.S)
    if not m:
        raise ValueError("Model did not return JSON")
    obj = json.loads(m.group(0))
    return obj.get("items", [])[:3]

# ---------- Publish once ----------
def publish_once():
    if not GITHUB_TOKEN:
        raise RuntimeError("Missing GITHUB_TOKEN")
    ensure_docs_structure()

    tz = ZoneInfo(LOCAL_TZ)
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(tz)

    slug = now_utc.strftime("%Y-%m-%dT%H-%M-%SZ")
    card_path = f"docs/cards/{slug}.json"
    # Idempotency: if today's exact slug exists (app restarted right after run), skip.
    if file_exists(card_path):
        print(f"[publisher] Card already exists for slug {slug}, skipping.")
        return

    when_local = now_local.strftime("%a, %d %b %Y • %H:%M %Z") + " • SAP EMEA"
    items = ai_research_items(when_local)
    card = build_adaptive_card("BTP4AI Wire — Daily Brief", when_local, items)
    commit_card_and_feed(card, slug)
    print(f"[publisher] Published {slug}")

# ---------- Scheduler (08:50 weekdays) ----------
def seconds_until_next_run() -> tuple[int, str]:
    tz = ZoneInfo(LOCAL_TZ)
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(tz)

    # Candidate today at RUN_HOUR:RUN_MINUTE
    target = now_local.replace(hour=RUN_HOUR, minute=RUN_MINUTE, second=0, microsecond=0)

    def next_weekday_after(dt):
        d = dt + timedelta(days=1)
        while d.weekday() >= 5:  # 5=Sat, 6=Sun
            d += timedelta(days=1)
        return d

    # Decide next run time
    if now_local.weekday() >= 5:  # weekend -> next Monday 08:50
        target = next_weekday_after(now_local).replace(hour=RUN_HOUR, minute=RUN_MINUTE, second=0, microsecond=0)
    elif now_local >= target:     # past today’s 08:50 -> next weekday 08:50
        target = next_weekday_after(now_local).replace(hour=RUN_HOUR, minute=RUN_MINUTE, second=0, microsecond=0)

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
            # basic retry once after 10 minutes
            print(f"[scheduler] publish_once error: {e}; retrying in 600s")
            time.sleep(600)
            try:
                publish_once()
            except Exception as e2:
                print(f"[scheduler] retry failed: {e2}")

# Start scheduler in background (works under gunicorn as well)
threading.Thread(target=scheduler_loop, daemon=True).start()

# ---------- HTTP (health/info) ----------
@app.get("/health")
def health():
    return {"ok": True, "tz": LOCAL_TZ, "run_time": f"{RUN_HOUR:02d}:{RUN_MINUTE:02d}"}

if __name__ == "__main__":
    # local testing
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))