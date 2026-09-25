#!/usr/bin/env python3
"""Installer helper -- standard library only, because it runs in step 1,
before any packages are installed.

  setup_helper.py widget-info <api_base> <widget_id> <secret>
      Prints shell variable assignments describing the widget (name, layout,
      whether it has an outdoor station, its branding links). Meant for eval.
  setup_helper.py probe <link-or-path>
      Checks an image link can be downloaded and really is an image.
      Prints "OK <kind> <size>" or "FAIL <reason>".
  setup_helper.py write-config <template> <out> KEY=VALUE ...
      Writes config.json from the template. Keys: WIDGET_ID, SECRET_KEY,
      DEVICE_LABEL, BG_MODE, BG_URL, LOGO_MODE, LOGO_URL, QR_MODE, QR_URL,
      OUTDOOR.
"""
import json
import os
import shlex
import sys
import urllib.request

UA = {"User-Agent": "airvoice-kiosk-installer/1.0"}
MAX_BYTES = 20 * 1024 * 1024


def widget_info(base, widget_id, secret):
    url = f"{base.rstrip('/')}/api/widget/v1/widgets/{widget_id}/?secret_key={secret}"
    out = {"W_OK": "no", "W_NAME": "", "W_LAYOUT": "", "W_OUTDOOR": "no", "W_BG": "", "W_LOGO": "", "W_QR": ""}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
            resp = (json.load(r) or {}).get("response") or {}
    except Exception as e:
        out["W_ERROR"] = f"{type(e).__name__}: {e}"
        resp = None
    if resp:
        def link(v):
            return v if isinstance(v, str) and v.startswith("https://") else ""
        layout = resp.get("layout") or ""
        has_outdoor = layout in ("TYPE_INDOOR_OUTDOOR_CMP", "TYPE_INDOOR_OUTDOOR") or bool(resp.get("outdoors"))
        out.update(W_OK="yes", W_NAME=resp.get("name") or "", W_LAYOUT=layout,
                   W_OUTDOOR="yes" if has_outdoor else "no", W_BG=link(resp.get("background_img")),
                   W_LOGO=link(resp.get("logo")), W_QR=link(resp.get("qr_code_img")))
    for k, v in out.items():
        print(f"{k}={shlex.quote(str(v))}")
    return 0


def _kind(head):
    if head.startswith(b"\x89PNG"):
        return "PNG"
    if head.startswith(b"\xff\xd8"):
        return "JPEG"
    if head[:4] == b"GIF8":
        return "GIF"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "WEBP"
    text = head.lstrip()
    if text.startswith(b"<svg") or (text.startswith(b"<?xml") and b"<svg" in head):
        return "SVG"
    return None


def probe(src):
    try:
        if src.startswith("https://") or src.startswith("http://"):
            if src.startswith("http://"):
                print("FAIL use an https:// link")
                return 1
            with urllib.request.urlopen(urllib.request.Request(src, headers=UA), timeout=20) as r:
                data = r.read(MAX_BYTES + 1)
        else:
            if not os.path.isabs(src) or not os.path.isfile(src):
                print("FAIL not an https link or an existing file (use a full path, e.g. /home/pi/logo.png)")
                return 1
            with open(src, "rb") as f:
                data = f.read(MAX_BYTES + 1)
    except Exception as e:
        print(f"FAIL could not download it ({type(e).__name__}: {e})")
        return 1
    if len(data) > MAX_BYTES:
        print("FAIL larger than 20 MB")
        return 1
    kind = _kind(data[:512])
    if not kind:
        print("FAIL that link is not an image (it may be a web page -- use the direct image link)")
        return 1
    print(f"OK {kind} {max(1, len(data) // 1024)}KB")
    return 0


def write_config(template, out, pairs):
    v = dict(p.split("=", 1) for p in pairs)
    with open(template) as f:
        text = f.read()
    for key in ("WIDGET_ID", "SECRET_KEY", "DEVICE_LABEL"):
        text = text.replace(f"__{key}__", v.get(key, ""))
    cfg = json.loads(text)
    assets = {}
    for name, prefix in (("background", "BG"), ("logo", "LOGO"), ("qr", "QR")):
        mode = v.get(f"{prefix}_MODE", "widget")
        assets[name] = {"mode": mode, "url": v.get(f"{prefix}_URL", "")} if mode == "url" else {"mode": mode}
    cfg["assets"] = assets
    cfg["outdoor"] = v.get("OUTDOOR", "auto")
    tmp = out + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    os.replace(tmp, out)
    return 0


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) == 4 and a[0] == "widget-info":
        sys.exit(widget_info(*a[1:]))
    if len(a) == 2 and a[0] == "probe":
        sys.exit(probe(a[1]))
    if len(a) >= 3 and a[0] == "write-config":
        sys.exit(write_config(a[1], a[2], a[3:]))
    print(__doc__)
    sys.exit(2)
