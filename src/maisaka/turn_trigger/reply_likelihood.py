"""估计一批新消息进入 Planner 后会调用 reply 的概率。

权重由无必要性门控期间的 Planner 首轮记录拟合得到（scripts/explore_reply_necessity.py），
样本只包含“有新的外部消息且没有 @ 麦麦”的轮次；被 @ 的消息由强制触发直接放行，不经过本函数。
重新拟合后只需要更新本文件中的权重，以及 dynamic_gate.py 里的离线阈值表。
"""

from dataclasses import dataclass
from math import exp, isfinite, log1p
from typing import Dict, Sequence, Tuple

MAX_PENDING_COUNT = 20
"""待处理消息数进入函数前的上限，与拟合时的截断一致。"""

DEFAULT_SECONDS_SINCE_BOT_MESSAGE = 3600.0
"""上下文中没有麦麦发言时采用的间隔，与拟合时的缺省值一致。"""

MEDIA_PLACEHOLDER_PREFIXES = ("[表情包", "[图片", "[消息类型]", "[CQ:image", "[语音", "[文件]", "[卡片")
"""只有媒体占位、没有文字内容的消息前缀。"""

@dataclass(frozen=True, slots=True)
class ReplyLikelihoodWeights:
    """回复评分权重；默认值沿用离线拟合结果，不在运行时自动学习。"""

    # 拟合权重：logit = 截距 + Σ 权重 × 特征。调整系数后需重新评估门控阈值。
    intercept: float = -1.2005
    mention_bot: float = 1.5852
    at_other: float = -1.1157
    question_mark: float = 0.4807
    placeholder_only: float = -1.0281
    recent_self_ratio: float = 2.6525
    recent_message_count: float = -0.0367
    log_seconds_since_bot: float = 0.0571
    pending_count: float = 0.1012
    # 主人的普通发言温和提高关注度；按批次布尔特征计分，不随主人消息数量叠加。
    owner_verified: float = 0.4

    def __post_init__(self) -> None:
        values = (
            self.intercept, self.mention_bot, self.at_other, self.question_mark,
            self.placeholder_only, self.recent_self_ratio, self.recent_message_count,
            self.log_seconds_since_bot, self.pending_count, self.owner_verified,
        )
        if not all(isfinite(value) for value in values):
            raise ValueError("回复评分权重必须是有限数值")


DEFAULT_REPLY_WEIGHTS = ReplyLikelihoodWeights()


@dataclass(frozen=True, slots=True)
class ReplyLikelihoodInput:
    """估计 reply 概率所需的对话快照。"""

    mention_bot: bool
    """新消息中是否提到了麦麦的名字。"""

    at_other: bool
    """新消息中是否 @ 了麦麦以外的人。"""

    has_question_mark: bool
    """新消息中是否带问号。"""

    placeholder_only: bool
    """新消息是否全是表情、图片等媒体占位。"""

    recent_self_ratio: float
    """最近 5 分钟的消息里麦麦发言所占比例。"""

    recent_message_count: int
    """最近 5 分钟的消息条数。"""

    seconds_since_bot_message: float
    """距离麦麦上一次发言的秒数。"""

    pending_count: int
    """本批新的外部消息条数。"""

    owner_verified: bool = False
    """本批是否包含宿主按真实平台账号校验的主人消息。"""


def summarize_pending_texts(texts: Sequence[str], *, has_at_bot: bool) -> Tuple[bool, bool, bool]:
    """从新消息文本得到 (at_other, has_question_mark, placeholder_only)。"""

    stripped_texts = [text.strip() for text in texts if text.strip()]
    at_other = not has_at_bot and any("@" in text for text in stripped_texts)
    has_question_mark = any("?" in text or "？" in text for text in stripped_texts)
    placeholder_only = bool(stripped_texts) and all(
        text.startswith(MEDIA_PLACEHOLDER_PREFIXES) for text in stripped_texts
    )
    return at_other, has_question_mark, placeholder_only


@dataclass(frozen=True, slots=True)
class ReplyLikelihoodScore:
    """一次评分的概率、加权总分及各特征贡献，供调度和排查使用。"""

    probability: float
    logit: float
    contributions: Dict[str, float]

    @property
    def detail(self) -> str:
        return " ".join(f"{name}={value:+.3f}" for name, value in self.contributions.items())


def score_reply_likelihood(
    likelihood_input: ReplyLikelihoodInput,
    *,
    weights: ReplyLikelihoodWeights = DEFAULT_REPLY_WEIGHTS,
) -> ReplyLikelihoodScore:
    """本地计算评分；贡献值是 logit 的加减分，不是概率百分点。"""

    contributions = {
        "intercept": weights.intercept,
        "mention_bot": weights.mention_bot * float(likelihood_input.mention_bot),
        "at_other": weights.at_other * float(likelihood_input.at_other),
        "question_mark": weights.question_mark * float(likelihood_input.has_question_mark),
        "placeholder_only": weights.placeholder_only * float(likelihood_input.placeholder_only),
        "recent_self_ratio": weights.recent_self_ratio * min(1.0, max(0.0, likelihood_input.recent_self_ratio)),
        "recent_message_count": weights.recent_message_count * max(0, likelihood_input.recent_message_count),
        "log_seconds_since_bot": weights.log_seconds_since_bot * log1p(
            max(0.0, likelihood_input.seconds_since_bot_message)
        ),
        "pending_count": weights.pending_count * min(MAX_PENDING_COUNT, max(0, likelihood_input.pending_count)),
        "owner_verified": weights.owner_verified * float(likelihood_input.owner_verified),
    }
    logit = sum(contributions.values())
    if not isfinite(logit):
        raise ValueError("回复评分总分必须是有限数值")
    # 可调整权重可能产生很大的负总分，按符号计算 sigmoid，避免指数溢出。
    probability = (
        1.0 / (1.0 + exp(-logit))
        if logit >= 0.0
        else exp(logit) / (1.0 + exp(logit))
    )
    return ReplyLikelihoodScore(probability=probability, logit=logit, contributions=contributions)


def estimate_reply_probability(
    likelihood_input: ReplyLikelihoodInput,
    *,
    weights: ReplyLikelihoodWeights = DEFAULT_REPLY_WEIGHTS,
) -> float:
    """估计这批消息进入 Planner 后会调用 reply 的概率。"""

    return score_reply_likelihood(likelihood_input, weights=weights).probability
