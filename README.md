# 可追查路径的分块 Max-Plus 批矩阵乘

片段矩阵来自合成状态解码器。区间基于按场景排列的片段顺序。 数据为本地合成。

- `data/capture.json` 保存采集原始内容。records 是交换记录，tensors 字段分开描述名称、类型、形状和值。整数索引从零开始，未指定闭合的区间均为左闭右开。
- `data/capacity.json` 保存制造端的容量、规模或分片条件。大尺寸是接口需考虑的使用规模。工作区计入新分配的解包、转换、归约、版本暂存及交接缓冲。调用方已有只读输入和已消费的输出不计入工作区，额外物化副本计入。

## 交换约定

`input` 和 `output` 是每片段允许的状态字典，`edge` 记录源边 ID、入口 ID、出口 ID 和权重，缺席坐标不可达。同一端点可有多条可追溯边，边 ID 为 ASCII 字母数字串，普通字典序比较。查询区间左右闭合。相邻两段必须至少有一个共享边界身份，否则区间无法合成；存在共享身份但指定端点没有路径时属于合法不可达。
各段权重为不超过绝对值 10^12 的整数，区间最多 10000 段。发布保留本次主计划的当前和上一个有效快照。运行资料申请的筛查可以排除任意列出的少量合法边 ID。

## 读取资料

使用 Python 3.11。首次创建本地环境时安装锁定依赖。只读查看显示原始字段和张量尺寸。

```sh
python -m pip install -r requirements.txt
python caseio.py data/capture.json
python -m unittest discover -s tests -v
```

现有检查核对采集格式和所附数据的结构。

## maxpath 包：批量 Max-Plus 合成与可追查区间查询

`maxpath/` 在上述交换约定上交付完整能力，全部行为由 `tests/` 下 21 项
（含 40 组随机暴力对照）测试锁定：

| 模块 | 职责 |
| --- | --- |
| `model.py` | 片段/边/视图/查询的不可变模型；权重 ±1e12、区间 ≤10000 段、得分 ±1e16 边界校验 |
| `store.py` | 内容寻址节点存储（`MemoryStore` / `SqliteStore`）；单元格只存 O(树高) 嵌套字典序键与 O(1) 指针，不物化完整路径 |
| `engine.py` | 分层段树、max-plus 合成、嵌套 `lex_key` 同分裁决、筛查影子子树、区间分解、按需路径重建与裁决解释 |
| `publication.py` | 出版凭据、当前+上一个快照保留期、凭据重放与标记-清除回收 |
| `case.py` | capture.json 适配与修订套用 |
| `cli.py` | 命令行入口 |

关键语义：

- **缺席 vs 负数**：不可达坐标没有单元格，不使用哨兵；负权重是合法条目，正常参与取大。
- **同分裁决**：得分严格优先；同分时按全段边 ID 序列 ASCII 字典序。每个单元格保存嵌套
  `lex_key`（叶子 `[edge_id]`，内部 `[左键, 右键]`），其逐项比较与展开序列的字典序严格一致，
  因此结果与分组大小、并行合并树形状无关（有左/右结合树形状的对照测试）。
- **可追查路径**：单元格只保留分裂状态与左右子单元格指针，查询时沿指针链重建完整边 ID 序列，
  并可在解释中指出第一处分歧边。
- **救济性筛查**：排除边只在叶子层过滤；仅跨度内命中排除边的影子子树重算，其余节点按
  SHA-256 内容 ID 原样复用。
- **修订与断裂**：修订改得分或改交界字典都产生新版本；相邻段字典无交集时登记断裂，
  跨断裂查询返回 `incompatible` 并标出边界位置、涉及片段与两侧字典，断裂两侧子区间仍可查询。
- **快照保留**：每个主计划保留最近两个有效快照；旧凭据在保留期内重放当时版本集合，
  超出保留期吊销；GC 只回收无任何存活快照或在线视图引用的节点。
- **工作区预算**：满规模（512 段、80 状态、16 扇出）实测单次合成 Python 暂存峰值约 3 MiB，
  低于 `capacity.json` 的 8 MiB；全量节点流式落 SQLite（约 2.2 GiB），进程内存保持常量级，
  全区间查询约 0.05 s。

命令行：

```sh
# 基线视图
python -I maxpath/cli.py data/capture.json
# 套用得分修订
python -I maxpath/cli.py data/capture.json --revisions B2
# 套用含交界字典变更的修订并出版快照（断裂会被明确标出）
python -I maxpath/cli.py data/capture.json --revisions B2,C2 \
    --store /tmp/maxpath.sqlite --state /tmp/pubs.json --publish
```

库用法：

```python
from maxpath import MaxPlusEngine, MemoryStore, QuerySpec
from maxpath.case import build_versions, load_capture, query_from_capture

order, base, revisions, raw_queries = load_capture("data/capture.json")
engine = MaxPlusEngine(MemoryStore())
engine.build_view("base", order, base)
result = engine.query("base", query_from_capture(raw_queries[0]))
result.to_dict()          # 得分、edge_ids、tie_break、不可达位置或断裂边界
```

