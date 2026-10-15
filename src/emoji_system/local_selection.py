"""只选择表情对象的本地评分，不替代发送概率或群冷却。"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import json
import re
import sqlite3
import time

from src.decision_replay.recorder import record

DATA_ROOT = Path(__file__).resolve().parents[2] / "data"
METADATA_PATH = DATA_ROOT / "emoji_selection_metadata.json"
BUNDLED_METADATA_PATH = DATA_ROOT.parent / "resources" / "qq-agent" / "emoji" / "metadata.json"
HISTORY_PATH = DATA_ROOT / "emoji_selection_history.db"


def load_metadata() -> Dict[str, Any]:
    """读取随仓库分发的评分，并按哈希用本地自定义条目覆盖。"""
    metadata: Dict[str, Any] = {}
    for path in (BUNDLED_METADATA_PATH, METADATA_PATH):
        if not path.exists():
            continue
        values = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(values, dict):
            raise ValueError("表情评分元数据必须为按哈希索引的对象")
        for file_hash, entry in values.items():
            if not isinstance(entry, dict):
                raise ValueError("表情评分条目必须为对象")
            metadata[file_hash] = {**metadata.get(file_hash, {}), **entry}
    return metadata


def _connect() -> sqlite3.Connection:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(HISTORY_PATH, timeout=2)
    connection.execute("CREATE TABLE IF NOT EXISTS sends (session_id TEXT, hash TEXT, sent_at REAL)")
    connection.execute("CREATE INDEX IF NOT EXISTS sends_scope ON sends(session_id, hash, sent_at)")
    return connection


def record_sent(session_id: str, file_hash: str, now: Optional[float] = None) -> None:
    now = time.time() if now is None else now
    connection = _connect()
    try:
        connection.execute("DELETE FROM sends WHERE sent_at <= ?", (now - 3600,))
        connection.execute("INSERT INTO sends VALUES (?, ?, ?)", (session_id, file_hash, now))
        connection.commit()
    finally:
        connection.close()


def penalties(sent_at: Sequence[float], now: float) -> Tuple[float, float]:
    recent = sorted((stamp for stamp in sent_at if 0 <= now - stamp < 3600), reverse=True)
    decay = [1 - (now - stamp) / 3600 for stamp in recent]
    return (14 * decay[0] if decay else 0, min(6.0, 2 * sum(decay[1:])))


def _matches(terms: Sequence[str], context: str) -> float:
    terms = list(dict.fromkeys(term.strip().casefold() for term in terms if term.strip()))
    return sum(term in context.casefold() for term in terms) / len(terms) if terms else 0.0


def score_candidate(
    *, tag: str, emotions: Sequence[str], description: str, metadata: Dict[str, Any],
    context: str, sent_at: Sequence[float], now: float,
) -> Dict[str, float]:
    keywords = metadata.get("keywords", [])
    if not isinstance(keywords, list) or not all(isinstance(term, str) for term in keywords):
        raise ValueError("表情 keywords 必须为字符串列表")
    use_when = metadata.get("useWhen", [])
    if isinstance(use_when, str):
        use_when = [term for term in re.split(r"[，,；;、\n]", use_when) if term]
    if not isinstance(use_when, list) or not all(isinstance(term, str) for term in use_when):
        raise ValueError("表情 useWhen 必须为字符串或字符串列表")
    quality = float(metadata.get("quality", 0))
    if not 0 <= quality <= 1:
        raise ValueError("表情 quality 必须在 0 到 1 之间")
    temporal, frequency = penalties(sent_at, now)
    scores = {
        "emotion": 55.0 if tag in emotions else 0.0,
        "context": 25 * _matches(keywords, context),
        "useWhen": 15 * _matches(use_when, context),
        "quality": (2.5 if emotions and description and keywords and use_when else 0) + 2.5 * quality,
        "time_penalty": temporal, "frequency_penalty": frequency,
    }
    scores["total"] = max(0.0, sum(scores[key] for key in ("emotion", "context", "useWhen", "quality"))
                          - temporal - frequency)
    return scores


def select_candidate(session_id: str, tag: str, candidates: List[Any], context: str) -> Optional[Any]:
    if len(candidates) == 1:
        record('emoji', 'emoji.selection', 'selected', 'singleton_bypasses_scoring', session_id=session_id,
               detail={'selected_hash':candidates[0].file_hash,'candidate_count':1,'threshold_applicable':False})
        return candidates[0]
    if not candidates:
        return None
    from .emoji_manager import _get_emoji_emotions
    from src.common.logger import get_logger

    metadata = load_metadata()
    now = time.time()
    connection = _connect()
    try:
        history = connection.execute("SELECT hash, sent_at FROM sends WHERE session_id=? AND sent_at>?",
                                     (session_id, now - 3600)).fetchall()
    finally:
        connection.close()
    ranked = []
    replay_scores = []
    for emoji in candidates:
        scores = score_candidate(tag=tag, emotions=_get_emoji_emotions(emoji), description=emoji.description,
                                 metadata=metadata.get(emoji.file_hash, {}), context=context,
                                 sent_at=[stamp for hash_, stamp in history if hash_ == emoji.file_hash], now=now)
        ranked.append((scores["total"], emoji.file_hash, emoji))
        replay_scores.append({"hash":emoji.file_hash,"scores":scores})
        get_logger("reply_emoji").info(f"表情本地评分: session_id={session_id} hash={emoji.file_hash} scores={scores}")
    best = sorted(ranked, key=lambda item: (-item[0], item[1]))[0]
    for offset in range(0, len(replay_scores), 100):
        record('emoji', 'emoji.candidate_scores', 'scored', 'local_scores', session_id=session_id,
               detail={'candidates':replay_scores[offset:offset+100], 'offset':offset,
                       'total_candidates':len(replay_scores)})
    record('emoji', 'emoji.selection', 'selected' if best[0] >= 70 else 'skipped',
           'score_passed' if best[0] >= 70 else 'score_below_threshold', session_id=session_id,
           detail={'rankings':sorted(replay_scores,key=lambda v:-v['scores']['total'])[:100],
                   'candidate_count':len(candidates),'rankings_truncated':len(candidates)>100,
                   'best_score':best[0],'threshold':70,'selected_hash':best[1] if best[0]>=70 else None})
    return best[2] if best[0] >= 70 else None
