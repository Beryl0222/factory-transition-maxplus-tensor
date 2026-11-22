"""离线读取合成采集资料。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np


def load_case(path: Path):
    """读出交接记录和原始张量。"""
    envelope = json.loads(path.read_text(encoding="utf-8"))
    arrays = {}
    for key, descriptor in envelope["tensors"].items():
        value = np.asarray(descriptor["values"], dtype=descriptor["dtype"])
        if tuple(descriptor["shape"]) != value.shape:
            raise ValueError(f"张量 {key} 与声明形状不一致")
        arrays[key] = value
    return envelope["records"], arrays


def main():
    parser = argparse.ArgumentParser(description="查看本地采集记录与张量尺寸")
    parser.add_argument("path", type=Path)
    settings = parser.parse_args()
    records, arrays = load_case(settings.path)
    print("记录字段", "、".join(sorted(records)))
    for key, array in sorted(arrays.items()):
        print(key, "形状", list(array.shape), "类型", array.dtype)


if __name__ == "__main__":
    main()
