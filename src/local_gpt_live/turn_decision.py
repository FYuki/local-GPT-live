from __future__ import annotations

import re
import unicodedata
from typing import Literal


TurnDecision = Literal["backchannel", "take_turn", "indeterminate"]


_BACKCHANNELS = frozenset(
    {
        "うん",
        "うんうん",
        "はい",
        "ええ",
        "へえ",
        "へー",
        "ふうん",
        "ふーん",
        "ほう",
        "なるほど",
        "たしかに",
        "確かに",
        "そうなんだ",
        "そうなんですね",
        "そうですね",
        "そうなの",
        "そっか",
        "そうか",
        "そうそう",
        "わかる",
        "分かる",
    }
)
# 語のない短い反応は、同意とも停止指示とも断定できない。
# 質問符や後続の語があれば保留せず、通常の発話権取得へ進める。
_SHORT_REACTION = re.compile(r"(?:[あえおへほふ][っー]{1,3}|ん[っー]{0,3})")
_BACKCHANNEL_SEPARATORS = re.compile(r"[\s、。！？!?…・]+")


def classify_turn(transcript: str) -> TurnDecision:
    """完全な相槌は継続し、語のない曖昧な反応は保留し、それ以外は発話権取得とする。"""
    text = unicodedata.normalize("NFKC", transcript.strip())
    # STTの片仮名・半角表記だけを同じ照合形へ揃える。認識本文は変更しない。
    text = "".join(chr(ord(char) - 0x60) if "ァ" <= char <= "ヶ" else char for char in text)
    normalized = _BACKCHANNEL_SEPARATORS.sub("", text)
    if not normalized:
        return "indeterminate"
    if normalized in _BACKCHANNELS:
        return "backchannel"
    if "?" not in text and _SHORT_REACTION.fullmatch(normalized):
        return "indeterminate"
    return "take_turn"
