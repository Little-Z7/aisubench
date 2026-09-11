from __future__ import annotations

import unittest

from aisubench.estimate import burn_rate, estimate_pools, eta_hours


def sample(ts, pools=None, usage=None, clean=True, **extra):
    """按采样契约构造样本；缺省字段用于测试缺键的健壮性。"""
    item = {"ts": ts, "agent": "kimi", "source": "watch", "clean": clean}
    if pools is not None:
        item["pools"] = pools
    if usage is not None:
        item["usage"] = usage
    item.update(extra)
    return item


class RatioEstimateTests(unittest.TestCase):
    """手算基准：3 个有效对 Σtokens=3000、ΣΔ=6、g=1 →
    R = 500，R_low = 3000/7，R_high = 3000/5，Q = R×100 = 50000。
    """

    def _samples(self):
        return [
            sample(0, {"5h": 0}, {"input": 0}),
            sample(3600, {"5h": 1}, {"input": 500}),
            sample(7200, {"5h": 3}, {"input": 400, "output": 600}),
            sample(10800, {"5h": 6}, {"input": 1000, "cached": 500}),
        ]

    def test_ratio_point_and_interval(self):
        pools = estimate_pools(self._samples(), granularity_pct=1.0)
        window = pools["5h"]
        self.assertTrue(window["available"])
        self.assertEqual(window["n_pairs"], 3)
        self.assertEqual(window["resets"], 0)
        self.assertEqual(window["total_tokens"], 3000)
        self.assertAlmostEqual(window["tokens_per_pct"], 500.0)
        self.assertAlmostEqual(window["quota_tokens"], 50000.0)
        self.assertAlmostEqual(window["q_low"], 3000.0 / 7.0 * 100.0)
        self.assertAlmostEqual(window["q_high"], 3000.0 / 5.0 * 100.0)

    def test_ratio_uses_ratio_of_sums_not_mean_of_ratios(self):
        # 各对比率分别 500/1=500、1000/2=500、1500/3=500 恰好一致，
        # 换成不均匀增量验证确实是 ΣS/ΣΔ：S=100/2=50、S=900/6=150 → 1000/8=125。
        pools = estimate_pools([
            sample(0, {"5h": 0}, {}),
            sample(60, {"5h": 2}, {"input": 100}),
            sample(120, {"5h": 8}, {"input": 900}),
        ], granularity_pct=1.0)
        self.assertAlmostEqual(pools["5h"]["tokens_per_pct"], 1000.0 / 8.0)

    def test_upper_unavailable_when_delta_le_granularity(self):
        pools = estimate_pools([
            sample(0, {"5h": 0}, {}),
            sample(60, {"5h": 1}, {"input": 500}),
        ], granularity_pct=1.0)
        window = pools["5h"]
        self.assertTrue(window["available"])
        self.assertAlmostEqual(window["tokens_per_pct"], 500.0)
        self.assertIsNone(window["q_high"])
        self.assertAlmostEqual(window["q_low"], 500.0 / 2.0 * 100.0)

    def test_input_order_is_sorted_by_ts(self):
        ordered = self._samples()
        shuffled = [ordered[2], ordered[0], ordered[3], ordered[1]]
        self.assertAlmostEqual(
            estimate_pools(shuffled, granularity_pct=1.0)["5h"]["tokens_per_pct"],
            500.0)

    def test_empty_samples_returns_only_meta(self):
        pools = estimate_pools([])
        self.assertEqual(set(pools), {"_meta"})
        self.assertEqual(pools["_meta"]["n_samples"], 0)
        self.assertEqual(pools["_meta"]["span_hours"], 0.0)
        self.assertEqual(pools["_meta"]["external_tokens"], 0)

    def test_nonpositive_granularity_raises(self):
        with self.assertRaises(ValueError):
            estimate_pools([], granularity_pct=0)


class ResetDetectionTests(unittest.TestCase):
    def test_reset_pair_excluded_and_counted(self):
        pools = estimate_pools([
            sample(0, {"5h": 10}, {}),
            sample(3600, {"5h": 20}, {"input": 100}),
            sample(7200, {"5h": 5}, {"input": 200}),   # 周窗滚动重置
            sample(10800, {"5h": 12}, {"input": 300}),
        ], granularity_pct=1.0)
        window = pools["5h"]
        self.assertEqual(window["resets"], 1)
        self.assertEqual(window["n_pairs"], 2)
        # 重置对贡献的 200 tokens 不得计入，ΣS=100+300=400，ΣΔ=10+7=17。
        self.assertEqual(window["total_tokens"], 400)
        self.assertAlmostEqual(window["tokens_per_pct"], 400.0 / 17.0)


class CleanFilterTests(unittest.TestCase):
    def _samples(self):
        return [
            sample(0, {"5h": 0}, {"input": 0}, clean=True),
            sample(3600, {"5h": 3}, {"input": 700, "cached": 300}, clean=False),
            sample(7200, {"5h": 6}, {"input": 500}, clean=True),
        ]

    def test_meta_external_tokens_counts_unclean_samples(self):
        meta = estimate_pools(self._samples())["_meta"]
        self.assertEqual(meta["n_samples"], 3)
        self.assertAlmostEqual(meta["span_hours"], 2.0)
        self.assertEqual(meta["external_tokens"], 1000)

    def test_default_uses_all_pairs(self):
        window = estimate_pools(self._samples())["5h"]
        self.assertEqual(window["n_pairs"], 2)
        self.assertEqual(window["total_tokens"], 1500)

    def test_clean_only_drops_pairs_touching_unclean_sample(self):
        window = estimate_pools(self._samples(), clean_only=True)["5h"]
        self.assertEqual(window["n_pairs"], 0)
        self.assertEqual(window["resets"], 0)
        self.assertFalse(window["available"])
        self.assertIsNone(window["tokens_per_pct"])
        self.assertIsNone(window["quota_tokens"])


class UnavailablePoolTests(unittest.TestCase):
    def test_zero_delta_with_tokens_is_unavailable_not_fake_zero(self):
        pools = estimate_pools([
            sample(0, {"5h": 5}, {"input": 0}),
            sample(3600, {"5h": 5}, {"input": 100}),
        ])
        window = pools["5h"]
        self.assertFalse(window["available"])
        self.assertIsNone(window["tokens_per_pct"])
        self.assertIsNone(window["quota_tokens"])
        self.assertIsNone(window["q_low"])
        self.assertIsNone(window["q_high"])
        self.assertEqual(window["n_pairs"], 0)
        self.assertEqual(window["total_tokens"], 0)

    def test_pool_present_in_single_sample_is_unavailable(self):
        window = estimate_pools([
            sample(0, {"5h": 1}, {"input": 10}),
            sample(60, {"5h": 2}, {"input": 10}),
            sample(120, {"week": 1}, {"input": 10}),
        ])["week"]
        self.assertFalse(window["available"])
        self.assertEqual(window["n_pairs"], 0)


class MissingFieldTests(unittest.TestCase):
    def test_missing_pools_keys_do_not_crash(self):
        pools = estimate_pools([
            sample(0, {"5h": 0, "week": 0}, {"input": 0}),
            sample(60, {"5h": 2}, {"input": 600}),          # week 缺键
            sample(120, None, {}),                          # pools / usage 全缺
        ])
        self.assertEqual(set(pools), {"5h", "week", "_meta"})
        self.assertEqual(pools["5h"]["n_pairs"], 1)
        self.assertAlmostEqual(pools["5h"]["tokens_per_pct"], 300.0)
        self.assertFalse(pools["week"]["available"])

    def test_missing_usage_keys_count_as_zero(self):
        window = estimate_pools([
            sample(0, {"5h": 0}, {}),
            sample(60, {"5h": 3}, {"output": 900}),   # 缺 input/cached 键
        ])["5h"]
        self.assertEqual(window["total_tokens"], 900)
        self.assertAlmostEqual(window["tokens_per_pct"], 300.0)

    def test_missing_ts_sorts_last_without_crash(self):
        pools = estimate_pools([
            sample(3600, {"5h": 6}, {"input": 100}),
            sample(None, {"5h": 6}, {"input": 100}),
            sample(0, {"5h": 0}, {}),
        ])
        self.assertEqual(pools["_meta"]["n_samples"], 3)
        # 缺 ts 的样本稳定排到最后，其与上一对的 Δ=0 被忽略。
        self.assertEqual(pools["5h"]["n_pairs"], 1)
        self.assertEqual(pools["5h"]["resets"], 0)
        self.assertEqual(pools["5h"]["total_tokens"], 100)


class BurnRateTests(unittest.TestCase):
    def test_rate_over_window(self):
        samples = [
            sample(0, {"5h": 0}, {"input": 100}),
            sample(3600, {"5h": 1}, {"input": 200}),
            sample(7200, {"5h": 2}, {"input": 300}),
        ]
        # 窗口右端取最新 ts=7200，1 小时窗口只含 3600/7200 两点：500 tokens / 1h。
        self.assertAlmostEqual(burn_rate(samples, "5h", 1.0), 500.0)

    def test_fewer_than_two_samples_returns_none(self):
        self.assertIsNone(burn_rate([sample(0, {"5h": 0}, {"input": 1})], "5h", 1.0))
        self.assertIsNone(burn_rate([], "5h", 1.0))

    def test_zero_span_returns_none(self):
        samples = [sample(0, {"5h": 0}, {"input": 1}),
                   sample(0, {"5h": 1}, {"input": 1})]
        self.assertIsNone(burn_rate(samples, "5h", 1.0))

    def test_unknown_pool_or_nonpositive_window_returns_none(self):
        samples = [sample(0, {"5h": 0}, {"input": 1}),
                   sample(3600, {"5h": 1}, {"input": 1})]
        self.assertIsNone(burn_rate(samples, "week", 1.0))
        self.assertIsNone(burn_rate(samples, "5h", 0))
        self.assertIsNone(burn_rate(samples, "5h", -1))


class EtaTests(unittest.TestCase):
    def test_eta_value(self):
        # (100 − 50) × 500 ÷ 100 = 250 小时。
        self.assertAlmostEqual(eta_hours(50, 500, 100), 250.0)

    def test_none_propagation(self):
        self.assertIsNone(eta_hours(None, 500, 100))
        self.assertIsNone(eta_hours(50, None, 100))
        self.assertIsNone(eta_hours(50, 500, None))

    def test_zero_and_negative_inputs_return_none(self):
        self.assertIsNone(eta_hours(0, 500, 100))     # 契约：任一输入为 0 即不可用
        self.assertIsNone(eta_hours(50, 0, 100))
        self.assertIsNone(eta_hours(50, 500, 0))
        self.assertIsNone(eta_hours(50, -1, 100))
        self.assertIsNone(eta_hours(50, 500, -100))

    def test_exhausted_pool_returns_none(self):
        self.assertIsNone(eta_hours(100, 500, 100))
        self.assertIsNone(eta_hours(120, 500, 100))


if __name__ == "__main__":
    unittest.main()
