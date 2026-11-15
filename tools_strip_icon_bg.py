"""Remove the icon's tile background, keeping the Venom head + Facebook badge.

The art is layered: transparent margin -> blue rounded border -> navy gradient
-> subject. Each pass steps across ONE colour boundary (any pixel touching
already-clear space is taken) and then region-grows with a tight LOCAL
tolerance, which follows the navy gradient but stops dead at the head.
Exactly two passes: ring, then navy. A third would start eating the subject.
"""
from collections import deque
from PIL import Image

SRC, OUT = "icon.png", "icon_nobg.png"
LOCAL_TOL = 20          # max per-step colour jump treated as "same layer"

im = Image.open(SRC).convert("RGBA")
w, h = im.size
px = im.load()
clear = [[px[x, y][3] == 0 for x in range(w)] for y in range(h)]

def dist2(a, b):
    return (a[0]-b[0])**2 + (a[1]-b[1])**2 + (a[2]-b[2])**2

def is_bg(c):
    """The tile is the only blue-dominant thing here: navy ground and the
    lighter blue border. The head is black/green-grey and the Facebook badge
    is far brighter, so both fail this and survive."""
    r, g, b = c[0], c[1], c[2]
    return b >= r + 8 and b >= g + 8 and b <= 236

def peel():
    q = deque()
    seen = [[False]*w for _ in range(h)]
    # seed: every clear pixel on, or connected to, the frame
    for x in range(w):
        for y in (0, h-1):
            if clear[y][x] and not seen[y][x]:
                seen[y][x] = True; q.append((x, y, None))
    for y in range(h):
        for x in (0, w-1):
            if clear[y][x] and not seen[y][x]:
                seen[y][x] = True; q.append((x, y, None))
    taken = 0
    while q:
        x, y, ref = q.popleft()
        for dx, dy in ((1,0),(-1,0),(0,1),(0,-1)):
            nx, ny = x+dx, y+dy
            if not (0 <= nx < w and 0 <= ny < h) or seen[ny][nx]:
                continue
            if clear[ny][nx]:
                seen[ny][nx] = True; q.append((nx, ny, None)); continue
            c = px[nx, ny]
            # stepping out of clear space: cross one boundary unconditionally.
            # otherwise stay within this layer's local tolerance.
            if is_bg(c) and (ref is None or dist2(c, ref) <= LOCAL_TOL*LOCAL_TOL):
                seen[ny][nx] = True
                clear[ny][nx] = True
                px[nx, ny] = (c[0], c[1], c[2], 0)
                taken += 1
                q.append((nx, ny, c))
    return taken

for i in range(1):
    print(f"pass {i+1}: cleared {peel():,} px")

# Final sweep: isolated scraps of the border ring the flood could not reach.
# Nothing in the subject is blue-dominant, so this only takes tile remnants.
stray = 0
for y in range(h):
    for x in range(w):
        c = px[x, y]
        if c[3] and is_bg(c):
            px[x, y] = (c[0], c[1], c[2], 0)
            clear[y][x] = True
            stray += 1
print(f"swept {stray:,} stray tile px")

# The ring's brightest highlight sits above the blue cap. It only survives in
# the far corners, which no part of the head or badge reaches, so clear any
# blue-leaning pixel out there by distance from centre.
cx, cy, far = w / 2.0, h / 2.0, min(w, h) * 0.586
corner = 0
for y in range(h):
    dy2 = (y - cy) ** 2
    for x in range(w):
        c = px[x, y]
        if not c[3]:
            continue
        if ((x - cx) ** 2 + dy2) ** 0.5 <= far:
            continue
        if c[2] > c[0] + 6 and c[2] > c[1] + 6:
            px[x, y] = (c[0], c[1], c[2], 0)
            clear[y][x] = True
            corner += 1
print(f"cleared {corner:,} corner px")

# Soften the halo: pixels still opaque but ringed by cleared space get their
# alpha eased down, so no hard navy fringe survives around the head.
soft = 0
snapshot = [[clear[y][x] for x in range(w)] for y in range(h)]
for y in range(1, h-1):
    for x in range(1, w-1):
        if snapshot[y][x]:
            continue
        n = sum(snapshot[y+dy][x+dx] for dx, dy in ((1,0),(-1,0),(0,1),(0,-1)))
        if n >= 3:
            r, g, b, a = px[x, y]
            px[x, y] = (r, g, b, int(a * 0.35)); soft += 1
print(f"feathered {soft:,} edge px")
im.save(OUT)
print("wrote", OUT, im.size)
