#!/usr/bin/env python3
"""
VIBRAIZE DAILY — static site generator
--------------------------------------
Reads feeds.json, fetches the RSS/Atom feeds, and rebuilds index.html:

  - a "TOP STORY" ticker teasing the newest notable item
  - 5 job listings (from feeds tagged as jobs; remote-US / Arizona prioritized)
  - the latest ~20 news stories WITH THUMBNAILS, newest first
  - a footer listing the sources actually pulled + a generated timestamp

Design is preserved from the original template — same palette, fonts, and
section styling. The only CSS added is a small thumbnail block for the news
list (marked in the <style> section).

Run once to generate index.html:  python build_site.py
Intended to run on a schedule (GitHub Actions) — one build-and-write per run.
"""

import html
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import requests

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "feeds.json"
OUTPUT_PATH = BASE_DIR / "index.html"

NEWS_LIMIT = 20          # how many news stories to list down the page
JOBS_LIMIT = 5           # how many job listings to show
FETCH_TIMEOUT = 20       # seconds per feed
SUMMARY_CHARS = 150      # one-line summary cap

# Arizona is MST (UTC-7) all year — no DST.
MST = timezone(timedelta(hours=-7))

USER_AGENT = (
    "Mozilla/5.0 (compatible; VibraizeDailyBot/1.0; "
    "+https://vibraize.github.io/)"
)

# A feed counts as a JOB feed if its config has "category":"jobs",
# or if "job" appears in its name or URL. Everything else is news.
def is_job_feed(feed):
    if str(feed.get("category", "")).lower() == "jobs":
        return True
    blob = (feed.get("name", "") + " " + feed.get("url", "")).lower()
    return "job" in blob

# Remote / Arizona detection for job prioritization + tagging.
REMOTE_HINTS = ("remote", "anywhere", "work from home", "wfh", "distributed", "us-remote")
AZ_HINTS = (
    "phoenix", "arizona", " az ", " az,", "az)", "tempe", "scottsdale", "mesa",
    "chandler", "gilbert", "glendale", "prescott", "flagstaff", "tucson",
)


# ----------------------------------------------------------------------------
# FETCH + PARSE
# ----------------------------------------------------------------------------
def load_feeds():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    return [f for f in cfg.get("feeds", []) if f.get("enabled", True)]


def fetch(url):
    """Fetch a feed with a real UA (some hosts block default parsers)."""
    try:
        resp = requests.get(url, timeout=FETCH_TIMEOUT, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        return feedparser.parse(resp.content)
    except Exception as e:
        print(f"  [!] fetch failed: {url} -> {e}")
        try:
            # Fallback: let feedparser fetch directly
            return feedparser.parse(url)
        except Exception as e2:
            print(f"  [!] fallback failed: {e2}")
            return None


def entry_datetime(entry):
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            try:
                return datetime.fromtimestamp(time.mktime(st), tz=timezone.utc)
            except Exception:
                pass
    return None


def clean_text(raw, limit=SUMMARY_CHARS):
    if not raw:
        return ""
    txt = re.sub(r"<[^>]+>", " ", raw)      # strip tags
    txt = html.unescape(txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    if len(txt) > limit:
        txt = txt[:limit].rsplit(" ", 1)[0].rstrip() + "…"
    return txt


def entry_image(entry):
    """Best-effort thumbnail URL from an RSS entry."""
    for key in ("media_content", "media_thumbnail"):
        val = entry.get(key)
        if val:
            for m in val:
                u = m.get("url")
                if u:
                    return u
    for l in entry.get("links", []):
        if str(l.get("type", "")).startswith("image") or l.get("rel") == "enclosure":
            href = l.get("href", "")
            if re.search(r"\.(jpe?g|png|webp|gif)(\?|$)", href, re.I):
                return href
    # look inside content / summary for an <img>
    blob = ""
    if entry.get("content"):
        blob = entry["content"][0].get("value", "")
    blob = blob or entry.get("summary", "") or entry.get("description", "")
    m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', blob)
    if m:
        return m.group(1)
    return None


def domain(url):
    try:
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return ""


def time_ago(dt, now):
    if not dt:
        return ""
    secs = (now - dt).total_seconds()
    if secs < 0:
        return "NEW"
    if secs < 3600:
        return f"{int(secs // 60)}M AGO"
    if secs < 86400:
        return f"{int(secs // 3600)}H AGO"
    return f"{int(secs // 86400)}D AGO"


def collect(feeds):
    """Return (jobs, news) lists of normalized item dicts."""
    jobs, news = [], []
    seen_links = set()
    used_sources = set()

    for feed in feeds:
        name, url = feed.get("name", "Feed"), feed.get("url", "")
        if not url:
            continue
        print(f"Fetching: {name}")
        parsed = fetch(url)
        if not parsed or not parsed.entries:
            continue
        job_feed = is_job_feed(feed)
        for entry in parsed.entries:
            link = entry.get("link", "")
            if not link or link in seen_links:
                continue
            seen_links.add(link)
            item = {
                "title": entry.get("title", "Untitled"),
                "link": link,
                "summary": clean_text(entry.get("summary", "") or entry.get("description", "")),
                "dt": entry_datetime(entry),
                "source": name,
                "image": entry_image(entry),
            }
            (jobs if job_feed else news).append(item)
            used_sources.add(name)

    return jobs, news, used_sources


# ----------------------------------------------------------------------------
# SELECT + RANK
# ----------------------------------------------------------------------------
def job_location_tag(item):
    text = (item["title"] + " " + item["summary"]).lower()
    if any(h in text for h in AZ_HINTS):
        return ("onsite", "ARIZONA")
    if any(h in text for h in REMOTE_HINTS):
        return ("remote", "REMOTE")
    return (None, None)


def pick_jobs(jobs, now):
    for j in jobs:
        cls, label = job_location_tag(j)
        j["loc_class"], j["loc_label"] = cls, label
        # priority: remote/AZ first, then by recency
        j["_priority"] = 1 if cls else 0
        j["_ts"] = j["dt"].timestamp() if j["dt"] else 0
    jobs.sort(key=lambda x: (x["_priority"], x["_ts"]), reverse=True)
    return jobs[:JOBS_LIMIT]


def pick_news(news):
    dated = [n for n in news if n["dt"]]
    undated = [n for n in news if not n["dt"]]
    dated.sort(key=lambda x: x["dt"], reverse=True)
    return (dated + undated)[:NEWS_LIMIT]


# ----------------------------------------------------------------------------
# RENDER
# ----------------------------------------------------------------------------
def esc(s):
    return html.escape(s or "", quote=True)


def render_job(item):
    cls, label = item.get("loc_class"), item.get("loc_label")
    tags = ""
    if cls:
        tags += f'<span class="tag {cls}">{esc(label)}</span>'
    tags += f'<span class="tag fit">{esc(item["source"])}</span>'
    summary = f'<div class="item-summary">{esc(item["summary"])}</div>' if item["summary"] else ""
    return f"""      <div class="item">
        <div class="item-head">
          <a class="item-title" href="{esc(item['link'])}">{esc(item['title'])}</a>
          <span class="item-meta">{esc(item['source'])}</span>
        </div>
        <div class="item-tags">{tags}</div>
        {summary}
        <a class="item-link" href="{esc(item['link'])}">{esc(domain(item['link']))}</a>
      </div>"""


def render_news(item, now):
    if item.get("image"):
        thumb = f'<img class="item-thumb" src="{esc(item["image"])}" alt="" loading="lazy" referrerpolicy="no-referrer">'
    else:
        thumb = '<div class="item-thumb placeholder"></div>'
    ago = time_ago(item["dt"], now)
    meta = esc(item["source"]) + (f" · {ago}" if ago else "")
    summary = f'<div class="item-summary">{esc(item["summary"])}</div>' if item["summary"] else ""
    return f"""      <div class="item news">
        {thumb}
        <div class="item-body">
          <div class="item-head">
            <a class="item-title" href="{esc(item['link'])}">{esc(item['title'])}</a>
            <span class="item-meta">{meta}</span>
          </div>
          {summary}
          <a class="item-link" href="{esc(item['link'])}">{esc(domain(item['link']))}</a>
        </div>
      </div>"""


def build_html(jobs, news, sources, now):
    dt_mst = now.astimezone(MST)
    datetime_label = dt_mst.strftime("%a %m.%d.%y — %H:%M MST").upper()
    footer_stamp = dt_mst.strftime("%a %b %d %Y, %H:%M MST")

    # ---- ticker: newest news item as top story ----
    if news:
        top = news[0]
        ticker = (
            f"<b>TOP STORY —</b> {esc(top['title'])} "
            f"<span style=\"color:var(--text-dim)\">(via {esc(top['source'])})</span>"
        )
    elif jobs:
        top = jobs[0]
        ticker = f"<b>TOP STORY —</b> New opening: {esc(top['title'])} — {esc(top['source'])}."
    else:
        ticker = "<b>TOP STORY —</b> Feeds are quiet right now — check back shortly."

    # ---- jobs section ----
    if jobs:
        jobs_html = "\n\n".join(render_job(j) for j in jobs)
    else:
        jobs_html = '      <div class="empty-state">No qualifying job listings in the feeds right now.</div>'

    # ---- news section ----
    if news:
        news_html = "\n\n".join(render_news(n, now) for n in news)
    else:
        news_html = '      <div class="empty-state">No news items available from the feeds right now.</div>'

    sources_label = ", ".join(sorted(sources)) if sources else "none reachable this run"

    return (
        PAGE
        .replace("{{DATETIME}}", datetime_label)
        .replace("{{TICKER}}", ticker)
        .replace("{{JOBS}}", jobs_html)
        .replace("{{NEWS}}", news_html)
        .replace("{{NEWS_COUNT}}", str(len(news)))
        .replace("{{FOOTER_STAMP}}", footer_stamp)
        .replace("{{SOURCES}}", esc(sources_label))
    )


# ----------------------------------------------------------------------------
# PAGE TEMPLATE  (original CSS preserved verbatim; only .item-thumb block added)
# ----------------------------------------------------------------------------
PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>VIBRAIZE DAILY</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700;800&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root{
    --bg:#060016;
    --bg-raised:#0a0224;
    --magenta:#f700ff;
    --hotpink:#ff0066;
    --blue:#0432c8;
    --steel:#08307b;
    --mint:#00ffbf;
    --violet:#d200fc;
    --purple:#8c00ff;
    --plum:#510b88;
    --text:#e8e3f5;
    --text-dim:#8a82ad;
    --border:#1c1240;
  }

  *{margin:0;padding:0;box-sizing:border-box;}

  html,body{
    background:var(--bg);
    color:var(--text);
    font-family:'Inter',sans-serif;
    -webkit-font-smoothing:antialiased;
  }

  body{
    background-image:
      radial-gradient(circle at 15% 0%, rgba(247,0,255,0.05), transparent 40%),
      radial-gradient(circle at 85% 15%, rgba(4,50,200,0.08), transparent 45%);
    padding-bottom:60px;
  }

  .wrap{max-width:760px;margin:0 auto;padding:0 20px;}

  /* ===== MASTHEAD ===== */
  .masthead-bar{
    border-bottom:1px solid var(--border);
    background:linear-gradient(180deg, rgba(247,0,255,0.04), transparent);
    padding:18px 0 0;
  }
  .masthead-top{
    display:flex;
    justify-content:space-between;
    align-items:center;
    font-family:'JetBrains Mono',monospace;
    font-size:11px;
    letter-spacing:0.12em;
    color:var(--text-dim);
    padding-bottom:14px;
  }
  .masthead-top .blink{
    color:var(--mint);
    animation:blink 1.6s steps(2,jump-none) infinite;
  }
  @keyframes blink{50%{opacity:0.25;}}

  .masthead-title{
    font-family:'JetBrains Mono',monospace;
    font-weight:800;
    font-size:42px;
    letter-spacing:-0.01em;
    line-height:1;
    color:var(--text);
    text-shadow:0 0 24px rgba(247,0,255,0.35);
    padding-bottom:6px;
  }
  .masthead-title .accent{color:var(--magenta);}

  .masthead-sub{
    display:flex;
    gap:14px;
    flex-wrap:wrap;
    font-family:'JetBrains Mono',monospace;
    font-size:11.5px;
    color:var(--steel-text);
    color:#6f7fb8;
    padding-bottom:16px;
    border-bottom:1px dashed var(--border);
    margin-bottom:0;
  }
  .masthead-sub span{display:flex;align-items:center;gap:6px;}
  .dot{width:6px;height:6px;border-radius:50%;display:inline-block;}

  /* ticker / top story */
  .ticker{
    margin:16px 0 0;
    padding:12px 14px;
    background:var(--bg-raised);
    border:1px solid var(--plum);
    border-left:3px solid var(--violet);
    border-radius:4px;
    font-family:'JetBrains Mono',monospace;
    font-size:12.5px;
    line-height:1.6;
    color:#d9c8f7;
    margin-bottom:20px;
  }
  .ticker b{color:var(--violet);font-weight:700;}

  /* ===== SECTION TABS ===== */
  .section{margin-top:34px;}

  .tab{
    display:inline-flex;
    align-items:center;
    gap:8px;
    padding:7px 16px 7px 12px;
    border-radius:6px 6px 0 0;
    font-family:'JetBrains Mono',monospace;
    font-weight:700;
    font-size:13px;
    letter-spacing:0.04em;
    text-transform:uppercase;
    position:relative;
    top:1px;
  }
  .tab .dot-icon{width:7px;height:7px;border-radius:50%;}

  .section-body{
    border-top:2px solid;
    background:linear-gradient(180deg, rgba(255,255,255,0.015), transparent 60%);
    padding:18px 0 4px;
  }

  /* color variants per section */
  .c-jobs .tab{background:rgba(255,0,102,0.12);color:var(--hotpink);}
  .c-jobs .tab .dot-icon{background:var(--hotpink);box-shadow:0 0 8px var(--hotpink);}
  .c-jobs .section-body{border-color:var(--hotpink);}

  .c-discuss .tab{background:rgba(4,50,200,0.16);color:#7c95ff;}
  .c-discuss .tab .dot-icon{background:var(--blue);box-shadow:0 0 8px var(--blue);}
  .c-discuss .section-body{border-color:var(--blue);}

  .c-extjobs .tab{background:rgba(210,0,252,0.12);color:var(--violet);}
  .c-extjobs .tab .dot-icon{background:var(--violet);box-shadow:0 0 8px var(--violet);}
  .c-extjobs .section-body{border-color:var(--violet);}

  .c-news .tab{background:rgba(0,255,191,0.10);color:var(--mint);}
  .c-news .tab .dot-icon{background:var(--mint);box-shadow:0 0 8px var(--mint);}
  .c-news .section-body{border-color:var(--mint);}

  /* ===== ITEMS ===== */
  .item{
    padding:14px 4px;
    border-bottom:1px solid var(--border);
  }
  .item:last-child{border-bottom:none;}

  .item-head{
    display:flex;
    justify-content:space-between;
    align-items:baseline;
    gap:10px;
    flex-wrap:wrap;
  }

  .item-title{
    font-family:'Inter',sans-serif;
    font-weight:600;
    font-size:15.5px;
    color:var(--text);
    text-decoration:none;
  }
  .item-title:hover{text-decoration:underline;}

  .item-meta{
    font-family:'JetBrains Mono',monospace;
    font-size:11px;
    color:var(--text-dim);
    white-space:nowrap;
  }

  .item-tags{
    display:flex;
    gap:6px;
    margin-top:6px;
    flex-wrap:wrap;
  }
  .tag{
    font-family:'JetBrains Mono',monospace;
    font-size:10px;
    padding:2px 7px;
    border-radius:3px;
    letter-spacing:0.03em;
  }
  .tag.remote{background:rgba(0,255,191,0.12);color:var(--mint);border:1px solid rgba(0,255,191,0.3);}
  .tag.onsite{background:rgba(255,0,102,0.12);color:var(--hotpink);border:1px solid rgba(255,0,102,0.3);}
  .tag.fit{background:rgba(140,0,255,0.14);color:#b78cff;border:1px solid rgba(140,0,255,0.3);}

  .item-summary{
    font-size:13.5px;
    line-height:1.55;
    color:#a89fc9;
    margin-top:5px;
  }

  .item-link{
    display:inline-block;
    margin-top:7px;
    font-family:'JetBrains Mono',monospace;
    font-size:11px;
    color:var(--blue);
    text-decoration:none;
  }
  .item-link:hover{color:#7c95ff;text-decoration:underline;}
  .item-link::before{content:"> ";color:var(--text-dim);}

  .empty-state{
    font-family:'JetBrains Mono',monospace;
    font-size:12.5px;
    color:var(--text-dim);
    padding:16px 4px;
    font-style:italic;
  }

  /* ===== NEWS THUMBNAILS (added for the feed layout) ===== */
  .item.news{display:flex;gap:14px;align-items:flex-start;}
  .item.news .item-body{flex:1;min-width:0;}
  .item-thumb{
    width:132px;height:88px;flex:0 0 132px;
    border-radius:5px;
    border:1px solid var(--border);
    object-fit:cover;
    background:var(--bg-raised);
    display:block;
  }
  .item-thumb.placeholder{
    background:
      radial-gradient(circle at 28% 24%, rgba(247,0,255,0.28), transparent 60%),
      radial-gradient(circle at 76% 82%, rgba(0,255,191,0.20), transparent 55%),
      var(--bg-raised);
  }

  /* ===== FOOTER ===== */
  .footer{
    margin-top:48px;
    padding-top:18px;
    border-top:1px dashed var(--border);
    font-family:'JetBrains Mono',monospace;
    font-size:10.5px;
    color:var(--text-dim);
    line-height:1.7;
  }
  .footer .accent{color:var(--steel);}

  @media (max-width:480px){
    .masthead-title{font-size:32px;}
    .item-title{font-size:14.5px;}
    .item-thumb{width:96px;height:66px;flex-basis:96px;}
  }
</style>
</head>
<body>
<div class="wrap">

  <div class="masthead-bar">
    <div class="masthead-top">
      <span><span class="blink">●</span> LIVE FEED</span>
      <span id="datetime">{{DATETIME}}</span>
    </div>
    <div class="masthead-title">VIBRAIZE <span class="accent">DAILY</span></div>
    <div class="masthead-sub">
      <span><span class="dot" style="background:var(--violet)"></span>JOB WATCH</span>
      <span><span class="dot" style="background:var(--mint)"></span>LIVE NEWS FEED</span>
    </div>
  </div>

  <div class="ticker">
    {{TICKER}}
  </div>

  <!-- SECTION: JOB LISTINGS -->
  <div class="section c-extjobs">
    <div class="tab"><span class="dot-icon"></span>Job Listings — Creative Technologist</div>
    <div class="section-body">

{{JOBS}}

    </div>
  </div>

  <!-- SECTION: NEWS FEED -->
  <div class="section c-news">
    <div class="tab"><span class="dot-icon"></span>Creative Tech News — Latest {{NEWS_COUNT}}</div>
    <div class="section-body">

{{NEWS}}

    </div>
  </div>

  <div class="footer">
    Generated <span class="accent">{{FOOTER_STAMP}}</span>. Sources: <span class="accent">{{SOURCES}}</span>.<br>
    VIBRAIZE DAILY — built for Malachi / Vibraize Visuals. Auto-updates from RSS feeds.
  </div>

</div>
</body>
</html>
"""


# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------
def main():
    now = datetime.now(timezone.utc)
    feeds = load_feeds()
    print(f"Loaded {len(feeds)} enabled feeds.\n")

    jobs_raw, news_raw, sources = collect(feeds)
    jobs = pick_jobs(jobs_raw, now)
    news = pick_news(news_raw)

    print(f"\nSelected {len(jobs)} jobs, {len(news)} news items.")
    html_out = build_html(jobs, news, sources, now)
    OUTPUT_PATH.write_text(html_out, encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH} ({len(html_out)} bytes).")


if __name__ == "__main__":
    main()
