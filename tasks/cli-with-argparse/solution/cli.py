"""统计文本文件里的整数：求和 / 取最大值 / 计数。"""
import argparse
import sys
from pathlib import Path


def read_numbers(path: Path) -> list:
    numbers = []
    for line in path.read_text(encoding="utf-8").splitlines():
        for field in line.split():
            numbers.append(int(field))
    return numbers


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cli.py", description="统计文本文件里的整数")
    parser.add_argument("file", help="每行一个整数的文本文件，空行忽略")
    parser.add_argument("--op", required=True, choices=("sum", "max", "count"),
                        help="要做的运算")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    path = Path(args.file)
    if not path.is_file():
        print(f"error: no such file: {args.file}", file=sys.stderr)
        return 1
    numbers = read_numbers(path)
    if not numbers:
        print("error: file has no numbers", file=sys.stderr)
        return 1
    operations = {"sum": sum, "max": max, "count": len}
    print(operations[args.op](numbers))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
