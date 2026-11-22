#!/usr/bin/env python3
"""Fetch the multi-year DE-LU day-ahead history and ECB monthly FX / HICP.

Sources (all public, no key):
* Energy-Charts price API, bzn=DE-LU (Bundesnetzagentur | SMARD.de, CC BY 4.0).
  DE-LU exists as a bidding zone from 2018-10-01. Native MTU is 60 min before
  2025-10-01 and 15 min after; we store hourly means.
* ECB Data Portal: EXR.M.USD.EUR.SP00.A (USD per EUR, monthly average).
* Eurostat prc_hicp_minr (HICP, ECOICOP ver. 2), geo=DE, coicop18=TOTAL,
  unit=I15 (Germany, 2015 = 100). The ECB ICP 2015-base series stop at
  2025-12; the Eurostat series runs to 2026-10 and agrees with it within
  0.05 index points on 2018-01 → 2025-12.

Writes data/processed/de_lu_da_hourly_2018_2025.csv.gz (2018-11-01 → 2025-11-01,
exclusive) and data/processed/fx_hicp_monthly.csv. The 2025-11 → 2026-10
window stays in the existing de_lu_da_hourly.csv.gz (unchanged).
"""
from __future__ import annotations

import io
import sys
import time
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "processed"
EC_URL = "https://api.energy-charts.info/price"
ECB = "https://data-api.ecb.europa.eu/service/data/{flow}/{key}?format=csvdata&startPeriod=2018-01"


def _get(url: str, params: dict | None = None, tries: int = 4) -> requests.Response:
    for k in range(tries):
        try:
            r = requests.get(url, params=params, timeout=90)
            r.raise_for_status()
            return r
        except requests.RequestException as exc:  # pragma: no cover - network
            if k == tries - 1:
                raise
            print("retry", url, exc, file=sys.stderr)
            time.sleep(5 * (k + 1))
    raise RuntimeError("unreachable")


def fetch_prices(start: str, end: str) -> pd.DataFrame:
    frames = []
    s = pd.Timestamp(start, tz="Europe/Berlin")
    e_all = pd.Timestamp(end, tz="Europe/Berlin")
    while s < e_all:
        e = min(s + pd.DateOffset(months=6), e_all)
        r = _get(EC_URL, {"bzn": "DE-LU", "start": s.isoformat(), "end": e.isoformat()})
        j = r.json()
        df = pd.DataFrame({"ts": pd.to_datetime(j["unix_seconds"], unit="s", utc=True), "p": j["price"]})
        frames.append(df)
        print(s.date(), "→", e.date(), len(df), "rows")
        s = e
        time.sleep(1)
    raw = pd.concat(frames).drop_duplicates("ts").sort_values("ts")
    raw = raw[(raw["ts"] >= pd.Timestamp(start, tz="Europe/Berlin")) & (raw["ts"] < e_all)]
    raw["hour"] = raw["ts"].dt.floor("h")
    hourly = raw.groupby("hour", as_index=False)["p"].mean()
    hourly["timestamp"] = hourly["hour"].dt.tz_convert("Europe/Berlin")
    return hourly[["timestamp", "p"]].rename(columns={"p": "da_price_eur_mwh"})


def fetch_ecb(flow: str, key: str, name: str) -> pd.DataFrame:
    r = _get(ECB.format(flow=flow, key=key))
    df = pd.read_csv(io.StringIO(r.text), usecols=["TIME_PERIOD", "OBS_VALUE"])
    return df.rename(columns={"TIME_PERIOD": "month", "OBS_VALUE": name})


EUROSTAT = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/prc_hicp_minr"


def fetch_hicp_de() -> pd.DataFrame:
    params = {"geo": "DE", "coicop18": "TOTAL", "unit": "I15", "sinceTimePeriod": "2018-01", "format": "JSON"}
    j = _get(EUROSTAT, params).json()
    idx = j["dimension"]["time"]["category"]["index"]
    vals = j["value"]
    rows = [(m, vals.get(str(i))) for m, i in idx.items()]
    return pd.DataFrame(rows, columns=["month", "hicp_de_2015_100"]).dropna().sort_values("month")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    if "--fx-only" in sys.argv:
        hourly = None
    else:
        hourly = fetch_prices("2018-11-01", "2025-11-01")
    if hourly is not None and hourly["da_price_eur_mwh"].isna().any():
        raise SystemExit("missing prices")
    if hourly is not None:
        hourly.to_csv(OUT / "de_lu_da_hourly_2018_2025.csv.gz", index=False, float_format="%.4f")
    fx = fetch_ecb("EXR", "M.USD.EUR.SP00.A", "usd_per_eur")
    hicp = fetch_hicp_de()
    m = fx.merge(hicp, on="month", how="outer").sort_values("month")
    m.to_csv(OUT / "fx_hicp_monthly.csv", index=False)
    print(m.tail(3).to_string(index=False))


if __name__ == "__main__":
    main()
