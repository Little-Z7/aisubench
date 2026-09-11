"""统计 app.log 中 INFO / WARN / ERROR 三种级别各出现多少行，写出 summary.txt。

用法：在 app.log 所在目录下执行 `python3 parse_log.py`。
"""
from pathlib import Path

LEVELS = ("INFO", "WARN", "ERROR")


def count_levels(path):
    counts = {level: 0 for level in LEVELS}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[2] in counts:
            counts[fields[2]] += 1
    return counts


def main():
    counts = count_levels("app.log")
    text = "".join(f"{level}={counts[level]}\n" for level in LEVELS)
    Path("summary.txt").write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
