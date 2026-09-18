#!/usr/bin/env python3
"""
Fetches Warren Buffett / Berkshire Hathaway's LATEST quarterly 13F-HR
institutional holdings report directly from SEC EDGAR - free, official,
no API key needed. Writes a summarized JSON to data/buffett_holdings.json
for generate_dashboard.py to render as a donut chart.

Why this needs its own script (not folded into the daily run):
13F filings are only required quarterly, due within 45 days of quarter end,
so there's nothing new to fetch on a daily basis. Run this weekly or
whenever you want to check for a newly-filed quarter - it's a no-op (keeps
existing data) if no newer 13F-HR has been filed since the last run.

SEC EDGAR fair-access policy asks for a descriptive User-Agent identifying
the requester. The default below is a generic placeholder - consider
editing SEC_USER_AGENT to include your own contact email.

Data source (official, free, no key):
  https://data.sec.gov/submissions/CIK0001067983.json

Usage:
    python scripts/fetch_buffett_holdings.py
"""

import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

BERKSHIRE_CIK = "0001067983"
SEC_USER_AGENT = "PM-Capital-Dashboard research-tool contact@example.com"  # consider personalizing
HEADERS = {"User-Agent": SEC_USER_AGENT}

TOP_N_HOLDINGS = 10  # remaining positions get grouped into "其他 (Other)"


def get_latest_13f_accession():
    url = f"https://data.sec.gov/submissions/CIK{BERKSHIRE_CIK}.json"
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    data = r.json()
    recent = data["filings"]["recent"]
    forms = recent["form"]
    accessions = recent["accessionNumber"]
    report_dates = recent["reportDate"]
    filing_dates = recent["filingDate"]

    for i, form in enumerate(forms):
        if form == "13F-HR":  # skip 13F-NT (notice only, no holdings table)
            return {
                "accession": accessions[i],
                "reportDate": report_dates[i],
                "filingDate": filing_dates[i],
            }
    return None


def fetch_information_table(accession):
    acc_nodash = accession.replace("-", "")
    index_url = f"https://www.sec.gov/Archives/edgar/data/1067983/{acc_nodash}/index.json"
    r = requests.get(index_url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    items = r.json()["directory"]["item"]

    xml_candidates = [it["name"] for it in items if it["name"].lower().endswith(".xml")]

    for name in xml_candidates:
        file_url = f"https://www.sec.gov/Archives/edgar/data/1067983/{acc_nodash}/{name}"
        fr = requests.get(file_url, headers=HEADERS, timeout=20)
        fr.raise_for_status()
        text = fr.text
        if "informationTable" in text or "infoTable" in text:
            return text
    return None


def strip_ns(tag):
    return re.sub(r"^\{.*\}", "", tag)


def parse_information_table(xml_text):
    root = ET.fromstring(xml_text)
    holdings = []
    for elem in root.iter():
        if strip_ns(elem.tag) == "infoTable":
            row = {strip_ns(child.tag): child for child in elem}
            issuer = row.get("nameOfIssuer")
            value = row.get("value")
            shares_parent = row.get("shrsOrPrnAmt")
            shares = None
            if shares_parent is not None:
                for c in shares_parent:
                    if strip_ns(c.tag) == "sshPrnamt":
                        shares = c.text
            if issuer is None or value is None:
                continue
            try:
                value_usd = float(value.text) * 1000  # 13F values are reported in thousands
            except (TypeError, ValueError):
                continue
            holdings.append({
                "issuer": issuer.text.strip(),
                "value_usd": value_usd,
                "shares": float(shares) if shares else None,
            })
    return holdings


def aggregate_by_issuer(holdings):
    """A single issuer can appear multiple times (e.g. different share classes)."""
    agg = {}
    for h in holdings:
        key = h["issuer"]
        if key not in agg:
            agg[key] = {"issuer": key, "value_usd": 0.0, "shares": 0.0}
        agg[key]["value_usd"] += h["value_usd"]
        if h["shares"]:
            agg[key]["shares"] += h["shares"]
    return list(agg.values())


def main():
    print("Looking up Berkshire Hathaway's latest 13F-HR on SEC EDGAR...")
    latest = get_latest_13f_accession()
    if not latest:
        print("ERROR: no 13F-HR filing found.", file=sys.stderr)
        sys.exit(1)
    print(f"Found filing: accession={latest['accession']} "
          f"reportDate={latest['reportDate']} filingDate={latest['filingDate']}")

    existing = None
    out_path = DATA_DIR / "buffett_holdings.json"
    if out_path.exists():
        existing = json.loads(out_path.read_text(encoding="utf-8"))
        if existing.get("reportDate") == latest["reportDate"]:
            print("No newer 13F filed since last run - nothing to update.")
            return

    xml_text = fetch_information_table(latest["accession"])
    if not xml_text:
        print("ERROR: could not locate the information table XML in this filing.", file=sys.stderr)
        sys.exit(1)

    holdings = parse_information_table(xml_text)
    if not holdings:
        print("ERROR: parsed 0 holdings from the information table.", file=sys.stderr)
        sys.exit(1)

    agg = aggregate_by_issuer(holdings)
    agg.sort(key=lambda h: h["value_usd"], reverse=True)
    total_value = sum(h["value_usd"] for h in agg)

    top = agg[:TOP_N_HOLDINGS]
    rest = agg[TOP_N_HOLDINGS:]
    rest_value = sum(h["value_usd"] for h in rest)

    result_holdings = [
        {
            "issuer": h["issuer"],
            "value_usd": round(h["value_usd"], 0),
            "pct": round(h["value_usd"] / total_value * 100, 2) if total_value else 0,
        }
        for h in top
    ]
    if rest_value > 0:
        result_holdings.append({
            "issuer": "其他 (Other)",
            "value_usd": round(rest_value, 0),
            "pct": round(rest_value / total_value * 100, 2) if total_value else 0,
        })

    output = {
        "manager": "Berkshire Hathaway Inc",
        "reportDate": latest["reportDate"],
        "filingDate": latest["filingDate"],
        "totalValueUsd": round(total_value, 0),
        "positionsCount": len(agg),
        "holdings": result_holdings,
        "source": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={BERKSHIRE_CIK}&type=13F-HR",
    }

    DATA_DIR.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\nDone. Total portfolio value: ${total_value/1e9:.1f}B across {len(agg)} positions.")
    print(f"Top holding: {result_holdings[0]['issuer']} ({result_holdings[0]['pct']}%)")
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
