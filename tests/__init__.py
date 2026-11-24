"""测试包：把工作区根加入导入路径，使 `python -m unittest discover -s tests`
在无安装步骤时也能同时找到 caseio 与 maxpath。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
