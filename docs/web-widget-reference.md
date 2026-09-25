# Web widget reference (reverse-engineered 2026-09-24)

Source: `https://myindoor.global/kiosk-view/assets/` bundles (index-C7szFn9R.js,
Widget-BafabAo1.js, ComparisonWidget-3CLg9ZVm.js, OutdoorWidget-DCZHll7R.js,
BreathEasy-CaB1Qfwl.js, CSS). Re-check if the bundle hashes change.
All px values at 1920x1080 (1vw = 19.2px, 1vh = 10.8px). Font: Inter variable.

## Layout routing
- `TYPE_INDOOR_OUTDOOR_CMP` -> ComparisonWidget (the indoor + outdoor layout).
- `TYPE_INDOOR_OUTDOOR` or timeseries with `response.outdoor` -> OutdoorWidget
  (a special design for one specific workspace).
- Everything else (incl. `TYPE_INDOOR`) -> Widget (indoor-only).
- `is_svg` -> SVG view; 401/404 -> no-permission page.

## TYPE_INDOOR geometry
- Container padding 49.5px top/bottom, 72px left/right; header top, content bottom.
- Header x=119.04 (2.45vw padding). Title 57.6px/700, lh .99, ls -3.84px,
  11.23px margin below. Date + time 38.4px bold, 8px gaps; the "|" is the date
  div's `border-right: 3px solid #fff`.
- Grade block 52% width, `border-right: 1px solid #fff` = vertical divider.
  "Air quality is" 76.8px bold, lh 1.2, ls -5.3px, 48.6px below.
  Status word 288px bold, lh .77, ls -1.152px.
- Description 48% width, 96px left padding, 76.8px bold, lh .94, vertically centred.
  Quality row 99.84px bottom padding.
- Metric bar: radius 38.4px, padding 22.46px / 72px, `rgba(0,0,0,.30)` + blur(2px),
  items 57.6px apart. Label 26.88px/400 lh 1.4; number 57.6px bold lh 1, 19.2px
  left padding; tick 7x38.4px radius 5px, centred on the number; unit 23.04px
  `#e6ecf2`, 19.2px left padding, bottom-aligned. Bar spans ~y 890-1030.
- Branding: box 76.8x270 at bottom 54px/right 0; SVG 38.4px wide at ~x1843-1882,
  y783-998. Hidden when `disable_branding`.
- Logo (if set): fixed 384x162 box, top 49.5px, right 19.2px, scale-down only.
  `qr_code_img` is ignored in this layout.

## Background
- No `background_img`: base colour + six animated blurred circles (static values below).
  | | good | fair | bad |
  |---|---|---|---|
  | base | `#01aa64` | `#ffa33b` | `linear-gradient(70deg,#e100ff,#7f00ff)` |
  | brightest circle | `#47bb00` | `#fd744c` | `#ee4f46` |
  Element 2 (lighter circle top-right): 691px, top -16vh, left 42vw, blur(12px),
  opacity .7, `linear-gradient(102deg,#bec617,#97ba1e)` when good.
- With `background_img`: no circles; photo `cover` centred under 50% black;
  quality + metrics sit in one panel `rgba(0,0,0,.35)` radius 38.4px, margin-right
  38.4px, padding 54/57.6/22.46/72; metric bar loses its own fill.

## Grade (status word + message)
- Words: `good` / `fair` / `bad`.
  - good: "Enjoy the fresh air! Regular ventilation keeps it this way."
  - fair: "Consider opening windows and using an air purifier to improve air quality."
  - bad: "Ventilate immediately and avoid prolonged indoor exposure."
- Driving species = first key of `meta.extra.aqi_config.widget.indoor`
  (e.g. IAQI on some widgets, PM25 on others).
- Thresholds: fair = thresholds[0], poor = thresholds[1]; from
  `config.aq_settings.thresholds` when `is_aqi_500` is false, from a hard-coded
  table when true (IAQI 51/101, PM25 31/61).
- On the rounded value: null or < fair -> good; < poor -> fair; else bad (strict <).
- calcZones / predicate / extended_thresholds are never read.

## Metric bar content
- Species = keys of timeseries `data.indexes` (not `species_list`); shown only if
  `rooms` non-empty.
- Order: IAQI, PM25, PM10, PM1, PM03, CO2, TVOC, TEMPERATURE, NOISE, LIGHT, HUMIDITY, rest.
- Labels: AQI, PM2.5, PM10, CO₂, Total VOC, Temperature, Rel. Humidity.
  Units: μg/m³ (U+03BC), ppm, ppb, °C, %, "points" (IAQI).
- Numbers: `Number(v.toFixed(0))` = round half away from zero (NOT Python round),
  en-locale grouping ("1,250"); missing -> "-".
- Tick colour: aq_settings thresholds + interval_grades -> grade colour; >= last
  threshold -> last grade, else first index with value < threshold; species with
  no thresholds -> `#B8BFCC`.

## Polling / freshness (web page)
- Fetches config + timeseries (30s timeout), then setTimeout to the end of the
  current minute -> refetch every minute on the minute. `interval` field unused.
- `meta.extra.date` unused. Staleness only from consecutive failed fetches:
  >=16 -> "Last update: over 15 minutes ago" (34.56px/500 under the date);
  >=61 -> "...over 1 hour ago".
- 1s crossfade on each update.

## Rest time
- `is_rest_time` true, or all values null/empty -> use `weekly_averages.indoor.indexes`;
  heading "Air quality over \nthe last week is" at 96px, description + divider hidden.
  Empty weekly data -> bar hidden, grade "good".

## CMP layout with no outdoor
ComparisonWidget still renders: outdoor panel shows label "outdoor", no metrics,
status "Good" (treats value as 0).
