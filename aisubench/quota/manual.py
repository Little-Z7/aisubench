from __future__ import annotations


class ManualQuotaProbe:
    """人工探针：读数来自用户肉眼看订阅页并手动输入。

    continuous = False：读数成本高、精度受页面显示粒度限制，
    标定循环只在首尾各快照一次。输入的百分比按 granularity_pct 取整，
    并提示精度有限。
    """

    continuous = False

    def __init__(self, input_fn=None, granularity_pct: float = 1.0):
        self.input_fn = input_fn or input
        self.granularity_pct = granularity_pct

    def snapshot(self) -> dict[str, float]:
        print("请输入窗口和已用百分比，格式为 5h=12,week=20；直接回车结束"
              f"（百分比按 {self.granularity_pct:g}% 粒度取整，读数精度有限）：")
        text = self.input_fn().strip()
        if not text:
            return {}
        result = {}
        step = self.granularity_pct
        for item in text.split(","):
            if "=" not in item:
                raise ValueError("额度格式必须为 窗口=百分比")
            name, value = item.split("=", 1)
            number = float(value.strip().rstrip("%"))
            if not 0 <= number <= 100:
                raise ValueError("额度百分比必须在 0 到 100 之间")
            result[name.strip()] = round(number / step) * step
        return result
