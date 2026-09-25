#!/usr/bin/env python3
"""Client branding assets -- background photo, logo, QR code.

Each asset's source is chosen at install time (config.json "assets"):
  {"mode": "widget"}              follow the widget's own settings on
                                  myindoor.global (changing it in the
                                  dashboard reaches the kiosk on the next
                                  config poll)
  {"mode": "url", "url": "..."}   a fixed https link, or a file on the device
  {"mode": "none"}                not shown
A config with no "assets" block behaves as "widget" for all three. Downloads
are cached on disk so the kiosk keeps its branding through reboots and
offline periods; an asset whose source becomes "nothing" is removed from the
cache so a previous client's branding never lingers.

    python3 client_assets.py --sync             # sync now from config.json
    python3 client_assets.py --check <link>     # validate one image link
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request

CACHE_DIR = os.environ.get("KIOSK_ASSET_CACHE", "/var/lib/kiosk-renderer/assets")
MANIFEST_PATH = os.path.join(CACHE_DIR, "manifest.json")
MAX_BYTES = 20 * 1024 * 1024
TIMEOUT_SEC = 20
SVG_RASTER_WIDTH = 1600  # large enough to scale down cleanly to a 4K widget

# widget config field -> cached file stem
FIELDS = {"background_img": "background", "logo": "logo", "qr_code_img": "qr"}
# config.json "assets" key -> widget config field
ASSET_KEYS = {"background": "background_img", "logo": "logo", "qr": "qr_code_img"}


def widget_urls(config):
    """{field: https url or None} from the widget config API response; {}
    if the config hasn't been fetched yet (so nothing is decided)."""
    resp = (config or {}).get("response")
    if not isinstance(resp, dict):
        return {}
    urls = {}
    for field in FIELDS:
        url = resp.get(field)
        urls[field] = url if isinstance(url, str) and url.startswith("https://") else None
    return urls


def desired_sources(widget_config, kiosk_cfg):
    """{field: source or None} for every field whose wanted source is known.
    source is an https URL or an absolute local path; None means "show
    nothing". A "widget"-mode field is left out until the widget config has
    been fetched, so a kiosk that boots offline keeps its cached branding."""
    choices = (kiosk_cfg or {}).get("assets") or {}
    from_widget = widget_urls(widget_config)
    wanted = {}
    for key, field in ASSET_KEYS.items():
        choice = choices.get(key) or {"mode": "widget"}
        mode = choice.get("mode", "widget")
        if mode == "none":
            wanted[field] = None
        elif mode == "url" and choice.get("url"):
            wanted[field] = choice["url"]
        elif field in from_widget:
            wanted[field] = from_widget[field]
    return wanted


def load_manifest():
    try:
        with open(MANIFEST_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def cached_path(field):
    """Local file for a synced asset, or None if there isn't one."""
    entry = load_manifest().get(field) or {}
    path = os.path.join(CACHE_DIR, entry.get("file", ""))
    return path if entry.get("file") and os.path.exists(path) else None


def needs_sync(wanted, manifest=None):
    manifest = load_manifest() if manifest is None else manifest
    for field, src in wanted.items():
        entry = manifest.get(field) or {}
        if src is None:
            if entry:
                return True
        elif entry.get("url") != src or not os.path.exists(os.path.join(CACHE_DIR, entry.get("file", ""))):
            return True
    return False


def _fetch(src, dest):
    """Copy an https URL or a local file to dest, size-capped."""
    if not src.startswith("https://"):
        if not os.path.isfile(src):
            raise FileNotFoundError(src)
        if os.path.getsize(src) > MAX_BYTES:
            raise ValueError(f"larger than {MAX_BYTES} bytes")
        shutil.copyfile(src, dest)
        return
    req = urllib.request.Request(src, headers={"User-Agent": "airvoice-kiosk-renderer/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp, open(dest, "wb") as out:
        total = 0
        while True:
            chunk = resp.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_BYTES:
                raise ValueError(f"larger than {MAX_BYTES} bytes")
            out.write(chunk)


def _is_svg(path, src):
    if src.lower().split("?")[0].endswith(".svg"):
        return True
    with open(path, "rb") as f:
        head = f.read(512).lstrip()
    return head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in head)


def _normalize(src_path, stem, tmpdir, is_svg):
    """Convert a download into a validated local image; returns (filename, (w, h))."""
    from PIL import Image

    if is_svg:
        png = os.path.join(tmpdir, stem + ".svg.png")
        subprocess.run(["rsvg-convert", "-w", str(SVG_RASTER_WIDTH), "-o", png, src_path],
                       check=True, timeout=60, capture_output=True)
        src_path = png
    with Image.open(src_path) as img:
        img.load()
        size = img.size
        if stem == "background":
            name = stem + ".jpg"
            img.convert("RGB").save(os.path.join(tmpdir, name), "JPEG", quality=92)
        else:
            name = stem + ".png"
            img.convert("RGBA").save(os.path.join(tmpdir, name), "PNG")
    return name, size


def fetch_image(src, stem, tmpdir):
    raw = os.path.join(tmpdir, "download")
    _fetch(src, raw)
    return _normalize(raw, stem, tmpdir, _is_svg(raw, src))


def _write_manifest(manifest):
    tmp = MANIFEST_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=1)
    os.replace(tmp, MANIFEST_PATH)


def sync(wanted, log=print):
    """Bring the cache in line with `wanted` ({field: source or None}). A
    failed download leaves the previously cached file in place. Returns True
    if anything changed."""
    if not wanted:
        return False
    os.makedirs(CACHE_DIR, exist_ok=True)
    manifest = load_manifest()
    changed = False
    for field, src in wanted.items():
        stem = FIELDS[field]
        entry = manifest.get(field) or {}
        if src is None:
            if entry:
                try:
                    os.remove(os.path.join(CACHE_DIR, entry.get("file", "")))
                except OSError:
                    pass
                del manifest[field]
                changed = True
                log(f"asset {field}: removed (none configured)")
            continue
        if entry.get("url") == src and os.path.exists(os.path.join(CACHE_DIR, entry.get("file", ""))):
            continue
        with tempfile.TemporaryDirectory(dir=CACHE_DIR) as tmpdir:
            try:
                name, _ = fetch_image(src, stem, tmpdir)
                os.replace(os.path.join(tmpdir, name), os.path.join(CACHE_DIR, name))
            except Exception as e:
                log(f"asset {field}: FAIL {type(e).__name__}: {e} src={src}")
                continue
        old = entry.get("file")
        if old and old != name:
            try:
                os.remove(os.path.join(CACHE_DIR, old))
            except OSError:
                pass
        manifest[field] = {"url": src, "file": name}
        changed = True
        log(f"asset {field}: OK {name} src={src}")
    if changed:
        _write_manifest(manifest)
    return changed


def check(src):
    """Installer helper: can this link be used? Prints 'OK <w>x<h>' or 'FAIL <why>'."""
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            _, (w, h) = fetch_image(src, "check", tmpdir)
        except FileNotFoundError as e:
            print(f"FAIL not found: {e}")
            return 1
        except subprocess.CalledProcessError:
            print("FAIL the SVG could not be converted")
            return 1
        except Exception as e:
            print(f"FAIL {type(e).__name__}: {e}")
            return 1
    print(f"OK {w}x{h}")
    return 0


def _sync_now():
    import api_client

    cfg = api_client.load_config()
    base = cfg["api_base"].rstrip("/")
    config = api_client._fetch(f"{base}/api/widget/v1/widgets/{cfg['widget_id']}/?secret_key={cfg['secret_key']}")
    if config is None:
        print("could not fetch widget config (see the API log)", file=sys.stderr)
    wanted = desired_sources(config, cfg)
    sync(wanted)
    missing = [FIELDS[f] for f, src in wanted.items() if src and not cached_path(f)]
    if missing:
        print("not cached: " + ", ".join(missing), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if args == ["--sync"]:
        sys.exit(_sync_now())
    if len(args) == 2 and args[0] == "--check":
        sys.exit(check(args[1]))
    print(__doc__.strip().split("\n\n")[-1])
    sys.exit(2)
