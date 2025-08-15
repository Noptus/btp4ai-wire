import os, json, html, hashlib
from pathlib import Path
from datetime import datetime, timezone
from email.utils import format_datetime

# --------- CONFIG ---------
SITE_URL = "https://<your-user>.github.io/btp4ai-wire"  # <- change me
DOCS_DIR = Path("docs")
CARDS_DIR = DOCS_DIR / "cards"
FEED_PATH = DOCS_DIR / "feed.xml"
CHANNEL_TITLE = "BTP4AI Wire — Daily Brief"
CHANNEL_DESC = "Daily AI news for SAP EMEA BTP4AI Hub"
CHANNEL_LINK = SITE_URL
MAX_ITEMS = 50  # keep last N items in feed
# --------------------------

def ensure_dirs():
    CARDS_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

def make_slug(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H-%M-%SZ")

def create_card_json(title: str, when_local: str, items: list):
    """
    items: list of dicts like:
      {"source_logo":"https://logo.clearbit.com/openai.com",
       "headline":"OpenAI ships GPT-5 with enterprise focus",
       "meta":"OpenAI • 07:40",
       "url":"https://openai.com/blog",
       "btp_angle":"Reassess workloads for GPT-5 agents"}
    """
    return {
      "type": "AdaptiveCard",
      "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
      "version": "1.5",
      "msteams": { "width": "Full" },
      "body": [
        {
          "type": "ColumnSet",
          "columns": [
            {"type":"Column","width":"auto","items":[
              {"type":"Image","url":"https://logo.clearbit.com/sap.com","size":"Small","style":"Person","altText":"BTP4AI Wire"}
            ]},
            {"type":"Column","width":"stretch","items":[
              {"type":"TextBlock","text": title, "weight":"Bolder","size":"Large"},
              {"type":"TextBlock","text": when_local, "isSubtle":True,"spacing":"None"}
            ]}
          ]
        },
        {"type":"TextBlock","text":"Top AI headlines","weight":"Bolder","size":"Medium","spacing":"Medium"},
      ] + [
        {
          "type":"Container","style":"default","items":[
            {"type":"ColumnSet","columns":[
              {"type":"Column","width":"auto","items":[{"type":"Image","url":it["source_logo"],"size":"Small"}]},
              {"type":"Column","width":"stretch","items":[
                {"type":"TextBlock","text":it["headline"],"wrap":True,"weight":"Bolder"},
                {"type":"TextBlock","text":it["meta"],"isSubtle":True,"spacing":"None"}
              ]}
            ]},
            {"type":"TextBlock","id":f"s_{i}","text":it.get("btp_angle",""),"wrap":True,"isVisible":False}
          ],
          "actions":[
            {"type":"Action.OpenUrl","title":"Read","url":it["url"]},
            {"type":"Action.ToggleVisibility","title":"SAP angle","targetElements":[f"s_{i}"]}
          ]
        } for i, it in enumerate(items, start=1)
      ]
    }

def write_card(card: dict, slug: str) -> Path:
    path = CARDS_DIR / f"{slug}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(card, f, ensure_ascii=False, indent=2)
    return path

def build_rss():
    # Collect cards (newest first)
    files = sorted(CARDS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:MAX_ITEMS]
    now = datetime.now(timezone.utc)
    last_build = format_datetime(now)

    # Build RSS XML manually (simple & robust)
    items_xml = []
    for p in files:
        slug = p.stem
        link = f"{SITE_URL}/cards/{p.name}"
        pub_dt = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
        pub_str = format_datetime(pub_dt)

        # Title fallback: CHANNEL_TITLE + date
        try:
            card = json.loads(p.read_text(encoding="utf-8"))
            header = card["body"][0]["columns"][1]["items"][0]["text"]
            title = f"{header} — {slug[:10]}"
        except Exception:
            title = f"{CHANNEL_TITLE} — {slug[:10]}"

        # Small HTML description w/ link
        desc_html = f'<p>Adaptive Card JSON: <a href="{link}">{p.name}</a></p>'
        # GUID stable from content hash
        guid = hashlib.sha1(p.read_bytes()).hexdigest()

        item_xml = f"""
      <item>
        <title>{html.escape(title)}</title>
        <link>{html.escape(link)}</link>
        <guid isPermaLink="false">{guid}</guid>
        <pubDate>{pub_str}</pubDate>
        <description><![CDATA[{desc_html}]]></description>
        <content:encoded><![CDATA[{desc_html}]]></content:encoded>
      </item>"""
        items_xml.append(item_xml)

    rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
  xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>{html.escape(CHANNEL_TITLE)}</title>
    <link>{html.escape(CHANNEL_LINK)}</link>
    <description>{html.escape(CHANNEL_DESC)}</description>
    <language>en</language>
    <lastBuildDate>{last_build}</lastBuildDate>
{''.join(items_xml)}
  </channel>
</rss>
"""
    FEED_PATH.write_text(rss, encoding="utf-8")

if __name__ == "__main__":
    ensure_dirs()
    # Example payload for "today"
    today = datetime.now(timezone.utc)
    slug = make_slug(today)
    card = create_card_json(
        title="BTP4AI Wire — Daily Brief",
        when_local="Fri, 15 Aug 2025 • 08:30 CEST • SAP EMEA",
        items=[
            {"source_logo":"https://logo.clearbit.com/oracle.com","headline":"Oracle x Google: Gemini on OCI","meta":"Oracle + Google • 07:00","url":"https://www.oracle.com/news/","btp_angle":"Multi-cloud model access for AI Core/Joule."},
            {"source_logo":"https://logo.clearbit.com/gsa.gov","headline":"U.S. GSA launches 'USAi' workspace","meta":"GSA • 07:15","url":"https://www.gsa.gov/","btp_angle":"Blueprint for public-sector AI with data boundaries."},
            {"source_logo":"https://logo.clearbit.com/openai.com","headline":"OpenAI ships GPT-5 (enterprise focus)","meta":"OpenAI • 07:40","url":"https://openai.com/blog","btp_angle":"Reassess agent workloads & licensing."}
        ]
    )
    write_card(card, slug)
    build_rss()
    print(f"Published: cards/{slug}.json and feed.xml")