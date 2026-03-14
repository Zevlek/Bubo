from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

SOURCES = {
    "IVV": "https://www.ishares.com/us/products/239726/ishares-core-sp-500-etf/1467271812596.ajax?fileType=csv&fileName=IVV_holdings&dataType=fund",
    "IJH": "https://www.ishares.com/us/products/239763/ishares-core-sp-mid-cap-etf/1467271812596.ajax?fileType=csv&fileName=IJH_holdings&dataType=fund",
    "IJR": "https://www.ishares.com/us/products/239774/ishares-core-sp-smallcap-etf/1467271812596.ajax?fileType=csv&fileName=IJR_holdings&dataType=fund",
}

QUOTAS = {"IVV": 500, "IJH": 300, "IJR": 200}
TARGET_COUNT = 1000
OUT_FILE = Path("data/universe_us_1000_v1.txt")
OUT_META = Path("data/universe_us_1000_v1_meta.json")


def _normalize_symbol(raw: object) -> str:
    return str(raw or "").replace("\ufeff", "").strip().upper()


def _load_holdings(url: str) -> pd.DataFrame:
    resp = requests.get(url, timeout=45)
    resp.raise_for_status()
    lines = resp.text.splitlines()
    header_idx = next(
        (i for i, line in enumerate(lines) if "Ticker" in line and "Name" in line and "Location" in line),
        None,
    )
    if header_idx is None:
        raise RuntimeError("Impossible de trouver l'entête des holdings")
    csv_data = "\n".join(lines[header_idx:])
    df = pd.read_csv(io.StringIO(csv_data))
    if "Ticker" not in df.columns:
        raise RuntimeError("Colonne Ticker absente")
    return df


def _extract_us_tickers(df: pd.DataFrame) -> list[str]:
    if "Location" in df.columns:
        df = df[df["Location"].astype(str).str.strip().str.lower() == "united states"]
    symbols: list[str] = []
    seen: set[str] = set()
    for raw in df["Ticker"].tolist():
        sym = _normalize_symbol(raw)
        if not sym or sym == "-":
            continue
        if sym in seen:
            continue
        seen.add(sym)
        symbols.append(sym)
    return symbols


def build_universe() -> tuple[list[str], dict[str, list[str]]]:
    pools: dict[str, list[str]] = {}
    for code, url in SOURCES.items():
        df = _load_holdings(url)
        pools[code] = _extract_us_tickers(df)

    selected: list[str] = []
    seen: set[str] = set()

    # Primary quotas.
    for code in ("IVV", "IJH", "IJR"):
        needed = QUOTAS[code]
        added = 0
        for sym in pools.get(code, []):
            if sym in seen:
                continue
            selected.append(sym)
            seen.add(sym)
            added += 1
            if added >= needed:
                break

    # Fill if dedup shrank below target.
    if len(selected) < TARGET_COUNT:
        for code in ("IVV", "IJH", "IJR"):
            for sym in pools.get(code, []):
                if sym in seen:
                    continue
                selected.append(sym)
                seen.add(sym)
                if len(selected) >= TARGET_COUNT:
                    break
            if len(selected) >= TARGET_COUNT:
                break

    return selected[:TARGET_COUNT], pools


def main() -> None:
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    symbols, pools = build_universe()
    OUT_FILE.write_text("\n".join(symbols) + "\n", encoding="utf-8")

    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "method": "Top holdings from IVV(500)+IJH(300)+IJR(200), dedup + fill, US location filter",
        "selected_count": len(symbols),
        "us_location_filter": "Location == United States",
        "sources": SOURCES,
        "quotas": QUOTAS,
        "source_pool_sizes": {k: len(v) for k, v in pools.items()},
        "file": str(OUT_FILE).replace("/", "\\"),
    }
    OUT_META.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"OK: {len(symbols)} tickers -> {OUT_FILE}")
    print(f"META: {OUT_META}")


if __name__ == "__main__":
    main()
