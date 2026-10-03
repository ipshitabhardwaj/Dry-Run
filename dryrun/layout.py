"""A small layered layout so the interface can draw the state graph without a library."""
from __future__ import annotations

from collections import defaultdict, deque

from .model import Process

NODE_W, NODE_H, GAP_X, GAP_Y, PAD = 164, 56, 56, 96, 28


def layout(p: Process) -> dict:
    out = defaultdict(list)
    for t in p.transitions:
        if t.source != t.target:
            out[t.source].append(t.target)
    depth = {p.start: 0}
    q = deque([p.start])
    while q:
        s = q.popleft()
        for n in out[s]:
            if n not in depth:
                depth[n] = depth[s] + 1
                q.append(n)
    far = max(depth.values(), default=0) + 1
    for s in p.states:
        depth.setdefault(s.id, far)
    layers = defaultdict(list)
    for s in p.states:
        layers[depth[s.id]].append(s.id)

    parents = defaultdict(list)
    for t in p.transitions:
        if depth[t.source] < depth[t.target]:
            parents[t.target].append(t.source)
    slot = {}
    for d in sorted(layers):
        ids = layers[d]
        if d:
            ids.sort(key=lambda i: (sum(slot[x] for x in parents[i]) / len(parents[i])
                                    if parents[i] else 1e9))
        for k, i in enumerate(ids):
            slot[i] = k - (len(ids) - 1) / 2
    widest = max(len(v) for v in layers.values())
    width = widest * NODE_W + (widest - 1) * GAP_X + 2 * PAD
    nodes = {}
    for d, ids in layers.items():
        for i in ids:
            cx = width / 2 + slot[i] * (NODE_W + GAP_X)
            nodes[i] = {"x": round(cx - NODE_W / 2), "y": PAD + d * (NODE_H + GAP_Y),
                        "w": NODE_W, "h": NODE_H, "layer": d, "slot": layers[d].index(i)}
    height = PAD * 2 + (max(layers) + 1) * NODE_H + max(layers) * GAP_Y
    return {"nodes": nodes, "width": round(width), "height": round(height)}
