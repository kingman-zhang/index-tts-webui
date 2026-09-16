"""路由聚合：按原 server.py 的分区顺序 include（注册顺序保持一致）。"""

from __future__ import annotations

from . import system, voices, podcast, projects, presets, glossary, voice_presets, queue

all_routers = [
    system.router,
    voices.router,
    podcast.router,
    projects.router,
    presets.router,
    glossary.router,
    voice_presets.router,
    queue.router,
]
