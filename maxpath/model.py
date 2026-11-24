"""片段、修订、视图与查询的不可变数据模型。

交换约定（见 README）：
- ``input`` / ``output`` 是片段允许的状态身份字典（有序，身份为字符串）。
- 边记录为 ``(edge_id, 入口状态, 出口状态, 有符号整数权重)``；缺席坐标表示不可达。
- 区间左右闭合；查询按 ``order`` 中的片段名定位。
- 权重绝对值不超过 1e12，区间至多 10000 段，故任意合法路径得分满足
  ``|score| <= 1e16``，用 Python 任意精度整数保存，绝不因累加改变并列关系。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import AbstractSet, FrozenSet, Iterable, Optional, Sequence, Tuple

#: 单条边权重绝对值上界（交接说明）。
MAX_ABS_WEIGHT = 10**12
#: 单次查询区间内最多包含的段数（交接说明）。
MAX_SEGMENTS_PER_RANGE = 10_000
#: 任意合法路径累计得分的绝对值上界。
MAX_ABS_SCORE = MAX_ABS_WEIGHT * MAX_SEGMENTS_PER_RANGE


@dataclass(frozen=True)
class Edge:
    """一条可追溯源边。"""

    edge_id: str
    entry: str
    exit_: str
    weight: int

    def __post_init__(self) -> None:
        if not isinstance(self.edge_id, str) or not self.edge_id:
            raise ValueError("边 ID 必须是非空字符串")
        if not self.edge_id.isascii() or not self.edge_id.isalnum():
            raise ValueError(f"边 ID 必须是 ASCII 字母数字串: {self.edge_id!r}")
        if not isinstance(self.weight, int) or isinstance(self.weight, bool):
            raise ValueError(f"边 {self.edge_id} 权重必须是整数")
        if abs(self.weight) > MAX_ABS_WEIGHT:
            raise ValueError(
                f"边 {self.edge_id} 权重 {self.weight} 超出 |w| <= {MAX_ABS_WEIGHT}"
            )


@dataclass(frozen=True)
class SegmentSpec:
    """一个片段版本（原始版本或某次修订）。

    ``revision_id`` 是该版本自身的身份；``replaces`` 指向被替换的片段逻辑名。
    原始片段的 ``revision_id`` 即片段名本身。
    """

    name: str
    revision_id: str
    input_states: Tuple[str, ...]
    output_states: Tuple[str, ...]
    edges: Tuple[Edge, ...]
    replaces: Optional[str] = None

    @classmethod
    def create(
        cls,
        name: str,
        edges: Iterable[Tuple[str, str, str, int] | Edge],
        input_states: Sequence[str],
        output_states: Sequence[str],
        revision_id: Optional[str] = None,
        replaces: Optional[str] = None,
    ) -> "SegmentSpec":
        """从采集文件风格的元组构造片段版本并做完整合法性校验。"""
        built = tuple(
            e if isinstance(e, Edge) else Edge(e[0], e[1], e[2], int(e[3]))
            for e in edges
        )
        in_set, out_set = set(input_states), set(output_states)
        if len(in_set) != len(input_states):
            raise ValueError(f"片段 {name} 输入状态字典存在重复身份")
        if len(out_set) != len(output_states):
            raise ValueError(f"片段 {name} 输出状态字典存在重复身份")
        seen_ids: set[str] = set()
        for edge in built:
            if edge.edge_id in seen_ids:
                raise ValueError(f"片段 {name} 内边 ID 重复: {edge.edge_id}")
            seen_ids.add(edge.edge_id)
            if edge.entry not in in_set:
                raise ValueError(
                    f"片段 {name} 边 {edge.edge_id} 入口 {edge.entry!r} 不在输入字典"
                )
            if edge.exit_ not in out_set:
                raise ValueError(
                    f"片段 {name} 边 {edge.edge_id} 出口 {edge.exit_!r} 不在输出字典"
                )
        return cls(
            name=name,
            revision_id=revision_id or name,
            input_states=tuple(input_states),
            output_states=tuple(output_states),
            edges=built,
            replaces=replaces,
        )

    def with_exclusions(self, excluded: AbstractSet[str]) -> "SegmentSpec":
        """返回剔除指定源边后的同版本片段（救济性筛查的叶子层实现）。"""
        if not excluded:
            return self
        kept = tuple(e for e in self.edges if e.edge_id not in excluded)
        return replace(self, edges=kept)


@dataclass(frozen=True)
class SegmentView:
    """某个视图下、区间中一个逻辑槽位实际选用的片段版本。"""

    position: int
    name: str
    revision_id: str
    spec: SegmentSpec


@dataclass(frozen=True)
class ViewPlan:
    """一个区间一致的版本集合。

    ``segments`` 按场景顺序排列；每个逻辑片段名恰好出现一次。
    ``boundary_breaks`` 记录因相邻段交界字典不交叠而无法拼接的位置：
    元素为 ``(左位置, 右位置, 左片段, 右片段, 左输出字典, 右输入字典)``。
    断裂不阻止建视图（其余子区间仍可查询），只阻止跨断裂合成。
    """

    view_id: str
    order: Tuple[str, ...]
    segments: Tuple[SegmentView, ...]
    boundary_breaks: Tuple[
        Tuple[int, int, str, str, Tuple[str, ...], Tuple[str, ...]], ...
    ] = field(default_factory=tuple)
    description: str = ""

    def index_of(self, segment_name: str) -> int:
        try:
            return self.order.index(segment_name)
        except ValueError as exc:
            raise KeyError(f"视图 {self.view_id} 中不存在片段 {segment_name!r}") from exc

    def breaks_in(self, lo: int, hi: int) -> Tuple[
        Tuple[int, int, str, str, Tuple[str, ...], Tuple[str, ...]], ...
    ]:
        """返回落在闭合区间 ``[lo, hi]`` 内的交界断裂。"""
        return tuple(b for b in self.boundary_breaks if lo <= b[0] and b[1] <= hi)


@dataclass(frozen=True)
class QuerySpec:
    """一次区间查询请求；区间按片段名左右闭合。"""

    start_segment: str
    end_segment: str
    start_state: str
    end_state: str
    excluded: FrozenSet[str] = frozenset()

    @classmethod
    def create(
        cls,
        start_segment: str,
        end_segment: str,
        start_state: str,
        end_state: str,
        excluded: Iterable[str] = (),
    ) -> "QuerySpec":
        excluded_frozen = frozenset(excluded)
        if any(not isinstance(x, str) or not x for x in excluded_frozen):
            raise ValueError("排除边 ID 必须是非空字符串")
        return cls(
            start_segment, end_segment, start_state, end_state, excluded_frozen
        )


@dataclass(frozen=True)
class QueryResult:
    """查询结果。

    状态：
    - ``ok``             找到最优路径，附带得分、边序列与并列裁决说明；
    - ``unreachable``    区间可合法拼接，但指定端点之间没有路径；
    - ``incompatible``   区间内存在交界字典断裂，无法合成，附带断裂边界。
    """

    status: str
    view_id: str
    start_segment: str
    end_segment: str
    start_state: str
    end_state: str
    excluded: Tuple[str, ...]
    score: Optional[int] = None
    edge_ids: Optional[Tuple[str, ...]] = None
    tie_break: Optional[str] = None
    unreachable_at: Optional[Tuple[str, str]] = None
    breaks: Tuple[
        Tuple[int, int, str, str, Tuple[str, ...], Tuple[str, ...]], ...
    ] = ()

    @property
    def reachable(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict:
        """供 JSON 序列化的消费者视图（含得分、边身份与不可达位置）。"""
        data = {
            "status": self.status,
            "view_id": self.view_id,
            "range": [self.start_segment, self.end_segment],
            "start_state": self.start_state,
            "end_state": self.end_state,
            "excluded": list(self.excluded),
        }
        if self.status == "ok":
            data["score"] = self.score
            data["edge_ids"] = list(self.edge_ids or ())
            data["tie_break"] = self.tie_break
        elif self.status == "unreachable":
            data["unreachable_at"] = list(self.unreachable_at or ())
        else:
            data["breaks"] = [
                {
                    "left_position": b[0],
                    "right_position": b[1],
                    "left_segment": b[2],
                    "right_segment": b[3],
                    "left_output_states": list(b[4]),
                    "right_input_states": list(b[5]),
                }
                for b in self.breaks
            ]
        return data


def validate_closed_range(lo: int, hi: int, n_segments: int) -> None:
    """校验闭合区间 ``[lo, hi]`` 的形状与长度上界。"""
    if lo < 0 or hi < 0 or lo >= n_segments or hi >= n_segments:
        raise IndexError(f"区间位置越界: [{lo}, {hi}]，共 {n_segments} 段")
    if lo > hi:
        raise ValueError(f"区间左右颠倒: [{lo}, {hi}]")
    length = hi - lo + 1
    if length > MAX_SEGMENTS_PER_RANGE:
        raise ValueError(
            f"区间长度 {length} 超过上限 {MAX_SEGMENTS_PER_RANGE}"
        )
