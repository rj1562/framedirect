# CPCB outdoor AQI -- data source research (2026-09-24)

Tags: [TESTED] fetched live, [DOC] from docs/third parties, [UNVERIFIED].

## Recommendation
Use the data.gov.in "Real time Air Quality Index from various locations" feed
(official CPCB, GODL licence: commercial use OK with attribution). Needs a free
personal API key per Airvoice account (register on data.gov.in -> My Account ->
"Generate Your New API KEY"). Do not use the public sample key (always 429) or
any key scraped from the portal page. CPCB's own portals (CCR, Sameer) have no
public API (AES-encrypted payloads + captcha) -- avoid.

| Option | Key | Freshness | Data | Use |
|---|---|---|---|---|
| data.gov.in AQI feed | free personal | hourly, ~30-50 min lag [TESTED] | CPCB sub-indices | **primary** |
| CPCB CCR / Sameer | no public API | 15 min-1 h | official | avoid |
| OpenAQ v3 | free | 1-6 h lag [UNVERIFIED] | concentrations | optional secondary |
| WAQI / aqicn | free token | near real-time | US EPA scale; for-profit needs agreement | no |
| Open-Meteo | none | 12 h model runs | **modelled**, ~45 km grid; free tier non-commercial | labelled last resort |

## data.gov.in feed
```
GET https://api.data.gov.in/resource/3b01bcb8-0b14-4abf-b6f2-c1bfd384ba69
    ?api-key=<KEY>&format=json&limit=4000
    [&filters[station]=<exact station name>] [&filters[city]=..] [&filters[state]=..]
```
- No key -> `400 {"error":"Authorization field missing"}` [TESTED].
- 505 stations / 264 cities / 31 states, 7 rows per station (PM2.5, PM10, NO2,
  SO2, CO, OZONE, NH3); full feed ~3,535 rows, ~1.5 MB [TESTED].
- JSON fields: `country, state, city, station, last_update, latitude, longitude,
  pollutant_id, min_value, max_value, avg_value` (CSV names them pollutant_min/max/avg).
  All strings; missing = `"NA"`.
- `last_update` ("24-09-2026 23:00:00", IST, DD-MM-YYYY) is the same on every row
  -> snapshot time, not per-station measurement time.
- **Values are AQI sub-indices, not concentrations** (matched CPCB's 24-Sep 4 PM
  bulletin for several stations: e.g. 20=20, 52=52, Leh CO 197 vs 193).
  `avg_value` looks like the sub-index of the 24 h (CO/O3 8 h) average [UNVERIFIED].
- Offline stations stay in the feed as NA.

Sample record:
```json
{"state":"Delhi","city":"Delhi","station":"Aya Nagar, Delhi - IITM",
 "last_update":"24-09-2026 23:00:00","latitude":"28.4706914","longitude":"77.1099364",
 "pollutant_id":"CO","min_value":"52","max_value":"68","avg_value":"60"}
```

## Station AQI (Indian National AQI)
- AQI = max `avg_value` over non-NA pollutants; valid only with >= 3 pollutants
  including PM2.5 or PM10, else "insufficient data". Prominent pollutant = the max.
- Bands: 0-50 Good, 51-100 Satisfactory, 101-200 Moderate, 201-300 Poor,
  301-400 Very Poor, 401-500 Severe.
- Concentration breakpoints (only if starting from raw concentrations, e.g. OpenAQ;
  24 h averages, CO/O3 8 h; CO mg/m3, others ug/m3):

| Band | PM10 | PM2.5 | NO2 | O3 | CO | SO2 | NH3 |
|---|---|---|---|---|---|---|---|
| Good | 0-50 | 0-30 | 0-40 | 0-50 | 0-1.0 | 0-40 | 0-200 |
| Satisfactory | 51-100 | 31-60 | 41-80 | 51-100 | 1.1-2.0 | 41-80 | 201-400 |
| Moderate | 101-250 | 61-90 | 81-180 | 101-168 | 2.1-10 | 81-380 | 401-800 |
| Poor | 251-350 | 91-120 | 181-280 | 169-208 | 10-17 | 381-800 | 801-1200 |
| Very Poor | 351-430 | 121-250 | 281-400 | 209-748 | 17-34 | 801-1600 | 1200-1800 |
| Severe | 430+ | 250+ | 400+ | 748+ | 34+ | 1600+ | 1800+ |

  Sub-index: `I = I_lo + (C - C_lo) * (I_hi - I_lo) / (C_hi - C_lo)`.

## PIN code -> location (install time only)
- Nominatim: `https://nominatim.openstreetmap.org/search?postalcode=<PIN>&countrycodes=in&format=jsonv2&limit=1`
  -- worked for 110001, 492001, 759122, 496001, 583275 [TESTED]. Policy: <= 1 req/s,
  identifying User-Agent (generic product name, no personal data), cache.
- Fallback: data.gov.in "All India Pincode Directory" (resource
  5c2f62fe-5afa-4119-a499-fec9d604d5bd, `filters[pincode]=`), lat/lon per post
  office -- has typos (one Delhi office off by 0.4 deg), so take the median.
- Confirmation text: `https://api.postalpincode.in/pincode/<PIN>` (district/state, no coords).

Tested nearest stations (23:00 IST snapshot):
- 759122 Angul: Hakimapada 1.2 km (AQI 144, NO2), Talcher 12.9 km.
- 496001 Raigarh: nearest 28.2 km (20), next 35.4 km (52), third 36.2 km (insufficient data).
- 110001 New Delhi: Mandir Marg 1.9 km (104), ITO 2.2 km (91), 5 stations within 3.1 km.

## Proposed design
- Install: PIN -> geocode -> confirm district -> fetch full feed once -> show 5
  nearest stations (distance, AQI, pollutant count; flag invalid or > 50 km) ->
  pick primary + 2 backups; store station names verbatim, lat/lon, API key.
- Runtime: poll `filters[station]=<primary>` hourly at ~HH:50 + 0-5 min random
  jitter per kiosk; never more often than 15 min. Cache on disk. Show AQI, band,
  prominent pollutant, station + distance, "as of HH:00", and
  "Source: CPCB via data.gov.in". Grey out if `last_update` unchanged > 3 h.
- Fallback: primary -> backup 1 -> backup 2 -> cached value marked stale
  (backoff up to 1 h). Weekly re-rank of backups (stations come and go).

## Not verified
Personal-key rate limits; exact min/max/avg semantics; the hourly roll-over (one
snapshot seen); whether dead stations ever keep old numbers; OpenAQ CPCB latency;
WAQI commercial permission.

Sources: data.gov.in resource page and GODL (data.gov.in/Godl), CPCB AQI note and
daily bulletin (cpcb.gov.in/aqi_report.php), docs.openaq.org, aqicn.org/api,
open-meteo.com terms, operations.osmfoundation.org/policies/nominatim.
