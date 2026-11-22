# Market-path inputs

| File | Window | Source | Licence |
| --- | --- | --- | --- |
| `de_lu_da_hourly.csv.gz` | 2025-11-01 → 2026-10-31, Europe/Berlin, 8,760 hourly means | Energy-Charts / SMARD DE-LU day-ahead price | **CC BY 4.0** — attribute Energy-Charts.info / Bundesnetzagentur \| SMARD.de |
| `ecb_usd_eur_monthly.csv` | 2025-11 → 2026-10 monthly averages | ECB USD/EUR, as used in [`epm-data-pipeline`](https://github.com/DataWithSyed/epm-data-pipeline) | ECB public data |
| `de_lu_da_hourly_2018_2025.csv.gz` | 2018-11-01 → 2025-10-31, Europe/Berlin, 61,368 hourly means | Energy-Charts / SMARD DE-LU day-ahead price (`scripts/fetch_history.py`) | **CC BY 4.0** — attribute Energy-Charts.info / Bundesnetzagentur \| SMARD.de |
| `fx_hicp_monthly.csv` | 2018-01 → 2026-10 | ECB `EXR.M.USD.EUR.SP00.A` (USD per EUR) + Eurostat `prc_hicp_minr` HICP Germany, all items, 2015 = 100 | ECB / Eurostat public data |

Raw Energy-Charts API JSON is not redistributed. These are derived hourly means (native MTU is 60 min before 2025-10-01 and 15 min after).
