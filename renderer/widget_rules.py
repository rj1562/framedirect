"""Data rules shared by the layouts: which readings a widget shows, how
values are formatted, and how each one is graded and coloured. Everything
comes from the widget's own config (aq_settings, is_aqi_500) so a new
client's thresholds work without code changes. Reverse-engineered from the
web widget's bundle -- see docs/web-widget-reference.md."""
import math

# Readings in the order the web widget lists them; anything else follows in
# the order the API sends it.
SPECIES_ORDER = ["IAQI", "PM25", "PM10", "PM1", "PM03", "CO2", "TVOC",
                 "TEMPERATURE", "NOISE", "LIGHT", "HUMIDITY"]
SPECIES_LABEL = {
    "IAQI": "AQI", "PM25": "PM2.5", "PM10": "PM10", "PM1": "PM1", "PM03": "PM0.3",
    "CO2": "CO₂", "TVOC": "Total VOC", "TEMPERATURE": "Temperature",
    "NOISE": "Noise", "LIGHT": "Light", "HUMIDITY": "Rel. Humidity",
    "PRESSURE": "Pressure", "CO": "CO", "NO2": "NO₂", "SO2": "SO₂", "O3": "O₃",
}
UNIT_LABEL = {
    "IAQI": "points", "MICROGRAMS_PER_CUBIC_METER": "μg/m³", "PARTS_PER_MILLION": "ppm",
    "PARTS_PER_BILLION": "ppb", "CELSIUS": "°C", "FAHRENHEIT": "°F", "PERCENT": "%",
    "SOUND_POWER_LEVEL": "dB", "LUX": "lx", "MM_HG": "mmHg",
    "PARTICLES_PER_DECILITER": "p/dL", "METERS_PER_SECOND": "m/s",
}

# Fixed thresholds the web widget uses instead of aq_settings when a widget
# is on the 0-500 national AQI scale (is_aqi_500): (fair, poor).
AQI_500_THRESHOLDS = {"IAQI": (51, 101), "PM25": (31, 61)}

GRADE_COLOR_DEFAULT = {"good": "#87AF1B", "fair": "#FFCD41", "poor": "#FD4E3A"}
NO_GRADE_COLOR = (184, 191, 204)  # #B8BFCC

# Overall indoor grade -> (status word, advice), exact web widget strings.
GRADE_TEXT = {
    "good": ("good", "Enjoy the fresh air! Regular ventilation keeps it this way."),
    "fair": ("fair", "Consider opening windows and using an air purifier to improve air quality."),
    "bad": ("bad", "Ventilate immediately and avoid prolonged indoor exposure."),
}


def hex_rgb(value):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def dig(d, path, default=None):
    cur = d
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


def latest_value(block):
    values = (block or {}).get("values") or []
    return values[-1] if values else None


def js_round(value):
    """Number(v.toFixed(0)): halves round away from zero (Python's round()
    rounds them to even)."""
    return int(math.copysign(math.floor(abs(value) + 0.5), value))


def format_value(value):
    if value is None:
        return "-"
    try:
        return f"{js_round(float(value)):,}"
    except (TypeError, ValueError):
        return "-"


def ordered_species(indexes):
    keys = list((indexes or {}).keys())
    known = [k for k in SPECIES_ORDER if k in keys]
    return known + [k for k in keys if k not in SPECIES_ORDER]


def species_label(key):
    return SPECIES_LABEL.get(key, key.title())


def unit_label(block):
    unit = (block or {}).get("unit")
    return UNIT_LABEL.get(unit, "" if unit is None else unit.lower())


class Grading:
    """Grades and colours for one widget config."""

    def __init__(self, config):
        resp = (config or {}).get("response") or {}
        self.is_aqi_500 = bool(resp.get("is_aqi_500"))
        aq = dig(resp, ("config", "aq_settings"), {}) or {}
        self.grade_colors = {g.get("name"): hex_rgb(g["color"]) for g in aq.get("grades") or []
                             if g.get("name") and g.get("color")}
        for name, color in GRADE_COLOR_DEFAULT.items():
            self.grade_colors.setdefault(name, hex_rgb(color))
        self.thresholds = {}
        for t in aq.get("thresholds") or []:
            if t.get("species") and t.get("thresholds"):
                self.thresholds[t["species"]] = (list(t["thresholds"]), list(t.get("interval_grades") or []))
        indoor = dig(config, ("meta", "extra", "aqi_config", "widget", "indoor"), {}) or {}
        self.driver = next(iter(indoor), "IAQI")

    def _limits(self, species):
        if self.is_aqi_500 and species in AQI_500_THRESHOLDS:
            fair, poor = AQI_500_THRESHOLDS[species]
            return [fair, poor], ["good", "fair", "poor"]
        return self.thresholds.get(species, (None, None))

    def species_color(self, species, value):
        """Tick colour: first threshold the value is below picks the grade;
        at or above the last one, the last grade."""
        thresholds, grades = self._limits(species)
        if value is None or not thresholds or not grades:
            return NO_GRADE_COLOR
        try:
            value = float(value)
        except (TypeError, ValueError):
            return NO_GRADE_COLOR
        grade = grades[-1]
        for i, t in enumerate(thresholds):
            if value < t:
                grade = grades[min(i, len(grades) - 1)]
                break
        return self.grade_colors.get(grade, NO_GRADE_COLOR)

    def overall(self, indexes):
        """Indoor grade: 'good' / 'fair' / 'bad' from the driving species."""
        value = latest_value((indexes or {}).get(self.driver))
        thresholds, _ = self._limits(self.driver)
        if value is None or not thresholds or len(thresholds) < 2:
            return "good"
        value = js_round(float(value))
        if value < thresholds[0]:
            return "good"
        if value < thresholds[1]:
            return "fair"
        return "bad"
