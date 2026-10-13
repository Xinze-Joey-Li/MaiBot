"""回复权重的兼容性、可调整性及数值边界验证。"""

from dataclasses import replace
from itertools import product
from math import exp, log1p
from unittest import TestCase

from src.maisaka.turn_trigger.reply_likelihood import (
    DEFAULT_REPLY_WEIGHTS,
    ReplyLikelihoodInput,
    ReplyLikelihoodWeights,
    estimate_reply_probability,
    score_reply_likelihood,
)


class ReplyLikelihoodWeightsTest(TestCase):
    def setUp(self) -> None:
        self.input = ReplyLikelihoodInput(
            mention_bot=False,
            at_other=False,
            has_question_mark=False,
            placeholder_only=False,
            recent_self_ratio=0.1,
            recent_message_count=10,
            seconds_since_bot_message=600.0,
            pending_count=2,
        )

    def test_default_weights_preserve_original_probability(self) -> None:
        """独立的原公式作为基准，覆盖特征组合和截断边界。"""
        for mention, at_other, question, media, ratio, pending in product(
            (False, True), (False, True), (False, True), (False, True), (-0.5, 0.1, 1.5), (-2, 2, 50)
        ):
            sample = replace(
                self.input, mention_bot=mention, at_other=at_other,
                has_question_mark=question, placeholder_only=media,
                recent_self_ratio=ratio, pending_count=pending,
            )
            original_logit = (
                -1.2005 + 1.5852 * float(mention) - 1.1157 * float(at_other)
                + 0.4807 * float(question) - 1.0281 * float(media)
                + 2.6525 * min(1.0, max(0.0, ratio)) - 0.0367 * 10
                + 0.0571 * log1p(600.0) + 0.1012 * min(20, max(0, pending))
            )
            self.assertAlmostEqual(estimate_reply_probability(sample), 1.0 / (1.0 + exp(-original_logit)), places=14)

    def test_custom_weight_changes_only_its_signal(self) -> None:
        sample = replace(self.input, mention_bot=True)
        custom = replace(DEFAULT_REPLY_WEIGHTS, mention_bot=3.0)
        baseline = score_reply_likelihood(sample)
        changed = score_reply_likelihood(sample, weights=custom)
        self.assertGreater(changed.probability, baseline.probability)
        self.assertAlmostEqual(changed.logit - baseline.logit, 3.0 - 1.5852)
        self.assertEqual(DEFAULT_REPLY_WEIGHTS.mention_bot, 1.5852)
        self.assertEqual(estimate_reply_probability(self.input, weights=custom), estimate_reply_probability(self.input))

    def test_score_explains_total(self) -> None:
        score = score_reply_likelihood(self.input)
        self.assertEqual(score.logit, sum(score.contributions.values()))
        self.assertIn("intercept=-1.200", score.detail)
        self.assertEqual(score.contributions["mention_bot"], 0.0)

    def test_invalid_weights_fail_explicitly(self) -> None:
        for value in (float("nan"), float("inf"), -float("inf")):
            with self.assertRaises(ValueError):
                ReplyLikelihoodWeights(intercept=value)

    def test_large_finite_weights_do_not_overflow_sigmoid(self) -> None:
        low = score_reply_likelihood(self.input, weights=replace(DEFAULT_REPLY_WEIGHTS, intercept=-1000.0))
        high = score_reply_likelihood(self.input, weights=replace(DEFAULT_REPLY_WEIGHTS, intercept=1000.0))
        self.assertEqual(low.probability, 0.0)
        self.assertEqual(high.probability, 1.0)
