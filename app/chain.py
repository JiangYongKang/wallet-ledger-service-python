"""本地模拟链：用确定性的区块事件流代替真实节点。

- push_block：在当前链顶追加一个区块，返回新高度。
- advance：不产生事件，仅空挖出块（用于推动确认数）。
- reorg_to：回退到指定高度，移除该高度及以上的全部区块；
  *已最终化*（埋深 >= finality_depth）的区块不允许重组，由调用方判定。
- events_since：按游标顺序读取事件（确定序：block_height 升序，
  块内按 push 顺序，同笔交易按事件出现顺序），支持分页与重试。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Block:
    height: int
    events: list[dict[str, Any]]


class MockChain:
    def __init__(self) -> None:
        self._blocks: list[Block] = []

    @property
    def blocks(self) -> list[Block]:
        return list(self._blocks)

    def push_block(self, events: list[dict[str, Any]]) -> int:
        height = len(self._blocks) + 1
        # 链是区块高度的权威来源：上链时以真实高度为准，忽略载荷里自报的高度，
        # 避免"事件进了高区块却声明低高度"造成确认数判定错误。
        normalized = []
        for ev in events:
            ev = dict(ev)
            ev["block_height"] = height
            normalized.append(ev)
        self._blocks.append(Block(height=height, events=normalized))
        return height

    def advance(self, blocks: int = 1) -> int:
        for _ in range(max(0, blocks)):
            self.push_block([])
        return self.height()

    def reorg_to(self, height: int) -> list[str]:
        """回退到 height（保留 1..height-1，移除 height 及以上）。
        返回被移除的事件 event_id 列表（保持确定序）。"""
        if height < 1 or height > len(self._blocks) + 1:
            raise ValueError(f"invalid reorg target height: {height}")
        removed: list[str] = []
        kept: list[Block] = []
        for blk in self._blocks:
            if blk.height >= height:
                for ev in blk.events:
                    eid = ev.get("event_id")
                    if eid is not None:
                        removed.append(eid)
            else:
                kept.append(blk)
        self._blocks = kept
        return removed

    def height(self) -> int:
        return len(self._blocks)

    def events_since(
        self, cursor: str | None, limit: int
    ) -> tuple[list[dict[str, Any]], str | None]:
        """顺序事件流。游标格式 "h:idx"（height:块内下标，0 起），
        语义为"下一条待读事件的位置"。
        返回 (事件列表, 下次游标)；无更多事件时游标为 None。"""
        all_events: list[tuple[int, int, dict[str, Any]]] = []
        for blk in self._blocks:
            for idx, ev in enumerate(blk.events):
                all_events.append((blk.height, idx, ev))
        start = 0
        if cursor is not None:
            try:
                ch, ci = (int(x) for x in cursor.split(":", 1))
            except (ValueError, AttributeError):
                raise ValueError(f"invalid cursor: {cursor!r}")
            start = 0
            for i, (h, idx, _ev) in enumerate(all_events):
                if h == ch and idx == ci:
                    start = i
                    break
            else:
                # 游标指向的位置已不存在（例如重组后）：从头重读，由摄入层按 event_id 去重
                start = 0
        page = all_events[start:start + limit]
        next_cursor: str | None = None
        end_index = start + len(page)
        if end_index < len(all_events):
            next_h, next_idx, _ = all_events[end_index]
            next_cursor = f"{next_h}:{next_idx}"
        return [ev for _h, _i, ev in page], next_cursor

    def reset(self) -> None:
        self._blocks.clear()
