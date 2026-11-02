#!/usr/bin/env python3
"""curate.py — build Marcu's personal AI news feed from real sources, not an algorithm.

Reads sources.json (people/terms harvested from his X bookmarks corpus + topic feeds), fetches
each feed with a browser UA, dedups with the watchers watermark helper, scores items against
his interest graph, applies kill rules for press-release spam / hype / ads, optionally asks the
local Strata lane to judge borderline items (skips gracefully if the single-session gate is busy),
then writes feed.xml (subscribe this URL from FreshRSS or the EvenOS G2 reader app), digest.md
for humans, and stats.json for operators. Print one line per kept item so cron can stay silent
on empty deltas.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(Path.home() / ".hermes/profiles/bender/skills/devops/watchers/scripts"))
from _watermark import Watermark  # type: ignore

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.5) AppleWebKit/605.1.95"}
BRAIN_URL = os.environ.get("MARCUBRAIN", "http://127.0.0.1:8320/v1/chat/completions")
MODEL = os.environ.get("MARCU_MODEL", "qwen3.8-flash-next-coder-iq1_m")


def fetch(url):
    try:
        raw = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25).read()
    except Exception as e:
        return None, repr(e)[:90]
    return raw, None


def parse_items(raw):
    """Loose RSS/Atom entry extraction, stdlib only."""
    txt = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    m = re.search(r"<item>.*?</item>|<entry>.*?</entry>", txt, re.S)
    if not m:
        return []
    out = []
    for block in re.findall(r"<(?:item|entry)>(.*?)</(?:item|entry)>", txt, re.S):
        def grab(tag):
            mm = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", block, re.S)
            v = (mm.group(1) if mm else "")
            v = re.sub(r"^\s*<!\[CDATA\[|\]\]>\s*$", "", v, flags=re.S)
            v = v.replace("<![CDATA[", "").replace("]]>", "")
            return html.unescape(v).strip()
        title, link = grab("title"), grab("link") or ""
        if not link:
            mm = re.search(r'href="([^"]+)"', block)
            link = mm.group(1) if mm else ""
        body = (grab("description") or grab("summary") or grab("content") or "")[:600]
        date = grab("pubDate") or grab("updated") or grab("published") or ""
        guid = grab("guid") or grab("id") or hashlib.sha1((title + link).encode()).hexdigest()
        if title and link:
            out.append({"title": title, "link": link, "body": body, "date": date, "guid": guid})
    return out


def score(item, cfg):
    text = f"{item['title']} {item['body']}".lower()
    if re.fullmatch(r"v?\d+\.\d+(\.\d+)?", item["title"].strip()):
        return -9, {"hit_terms": [], "hit_people": [], "kill": ["version-tag release noise"]}
    kill = [k for k in cfg["kill_rules"] if k.lower() in text]
    hit_terms = [t for t in cfg["terms"] if t.lower() in text]
    hit_people = [p for p in cfg["boost_people_terms"] if p.lower() in text]
    s = len(hit_terms) * 2 + len(hit_people) * 5 - (14 if kill else 0)
    return s, {"hit_terms": hit_terms[:6], "hit_people": hit_people[:4], "kill": kill}


def brain_judge(items):
    """Ask the local lane about borderline items; never let a busy gate break the run."""
    if not items:
        return {}
    try:
        body = json.dumps({
            "model": MODEL,
            "messages": [{"role": "user", "content":
                "Keep only items matching an engineer who cares about agents, coding models, local "
                "inference engines, quantization, computer-use tooling and hardware builds. Reject "
                "press releases, hype claims with no evidence, ads, and follower-acquisition spam. "
                "Return comma-separated indexes to KEEP from this list:\n" +
                "\n".join(f"{i}. {it['title'][:90]}" for i, it in enumerate(items))}],
            "temperature": 1.0, "max_tokens": 64}).encode()
        res = json.loads(urllib.request.urlopen(
            urllib.request.Request(BRAIN_URL, data=body, headers={**UA, "Content-Type": "application/json"}), timeout=35).read())
        reply = (((res.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        keep = set(int(x) for x in re.findall(r"\d+", reply)[: len(items)])
        return {items[i]["guid"]: True for i in range(len(items)) if i in keep}
    except Exception:
        return None


def main():
    cfg = json.load(open(HERE / "sources.json"))
    kept, stats = [], {"fetched": 0, "new": 0, "dropped_kill": 0, "dropped_low": 0}
    borderline = []
    for src in cfg["feeds"]:
        raw, err = fetch(src["url"])
        if raw is None:
            stats.setdefault("errors", []).append(f"{src['name']}: {err}")
            continue
        items = parse_items(raw)
        for it in items:
            it.setdefault("id", it.get("guid") or hashlib.sha1((it["title"] + it["link"]).encode()).hexdigest())
        wm = Watermark.load(f"marcu-feed-{src['name']}")
        if "--eval-baseline" in sys.argv:
            fresh = list(items)
        else:
            fresh = wm.filter_new(items, id_key="id")
        wm.save()
        stats["fetched"] += len(items)
        stats["new"] += len(fresh)
        for it in fresh:
            sc, why = score(it, cfg)
            it["_score"], it["_why"] = sc, why
            if why["kill"]:
                stats["dropped_kill"] += 1
            elif sc >= 4:
                kept.append(it)
            elif sc >= 1:
                borderline.append(it)
            else:
                stats["dropped_low"] += 1

    verdicts = brain_judge(borderline[:25]) if borderline else {}
    if verdicts:
        kept += [it for it in borderline if verdicts.get(it["guid"])]
        stats["brain"] = {"sent": min(len(borderline), 25), "kept": sum(1 for v in verdicts.values() if v)}
    else:
        stats["brain"] = {"sent": 0, "kept": 0, "note": "gate busy or judge skipped"}

    kept.sort(key=lambda i: -i["_score"])
    top = kept[:60]
    # RSS out
    def esc(s):
        return html.escape(s or "", quote=True)
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    items_xml = []
    for it in top:
        why = ", ".join((it["_why"]["hit_people"] + it["_why"]["hit_terms"])[:6])
        items_xml.append("    <item>\n"
                         f"      <title>{esc(it['title'][:180])}</title>\n"
                         f"      <link>{esc(it['link'])}</link>\n"
                         f"      <guid isPermaLink=\"false\">{esc(it['guid'])}</guid>\n"
                         f"      <pubDate>{dt.datetime.now().strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>\n"
                         f"      <description>{esc((it['body'][:240] + ' | why kept: ' + why))}</description>\n"
                         "    </item>")
    if top:  # zero-kept ticks must not wipe the published snapshot
        (HERE / "feed.xml").write_text(
            '<?xml version="1.0"?><rss version="2.0"><channel><title>Marcu curated AI feed</title>'
            f"<link>https://github.com/prodbymarcu/marcu-feed</link><description>No algorithm, no ads; "
            f"interest graph from his bookmarks corpus plus local-model judgment.</description>"
            "<language>en</language>" + "\n".join(items_xml) + "</channel></rss>\n")
        (HERE / "digest.md").write_text("\n".join(
            f"- {it['title'][:120]} — {it['link']} (score {it['_score']})" for it in top) + "\n")
    stats.update({"kept": len(top), "at": now})
    (HERE / "stats.json").write_text(json.dumps(stats, indent=1))
    print(f"tick: fetched={stats['fetched']} new={stats['new']} kept={len(top)} "
          f"killed={stats['dropped_kill']} low={stats['dropped_low']}", flush=True)


main()
