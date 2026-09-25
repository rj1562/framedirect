"""Save what's currently on /dev/fb0 as a PNG (RGB565 framebuffer)."""
import sys

import numpy as np
from PIL import Image

out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/fb_snapshot.png"
with open("/sys/class/graphics/fb0/virtual_size") as f:
    w, h = (int(v) for v in f.read().strip().split(","))
with open("/sys/class/graphics/fb0/stride") as f:
    stride = int(f.read().strip())
with open("/dev/fb0", "rb") as f:
    raw = np.frombuffer(f.read(stride * h), dtype="<u2").reshape(h, stride // 2)[:, :w]

rgb = np.empty((h, w, 3), dtype=np.uint8)
rgb[:, :, 0] = ((raw >> 11) & 0x1F) * 255 // 31
rgb[:, :, 1] = ((raw >> 5) & 0x3F) * 255 // 63
rgb[:, :, 2] = (raw & 0x1F) * 255 // 31
Image.fromarray(rgb, "RGB").save(out)
print(out)
