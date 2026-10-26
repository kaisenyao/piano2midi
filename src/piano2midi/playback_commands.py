"""Shared playback controls for chat and voice; song selection remains with the model."""
import os
import re
import unicodedata

WAKE_WORDS = [w.strip() for w in os.environ.get("PIANO2MIDI_WAKE_WORDS", "\u5c0f\u8317\u540c\u5b66,Hey Jasmine").split(",") if w.strip()]

# Short commands handled without the model. Chinese phrases are written as escapes because the repository is
# English-only; the pinyin is in the comments.
COMMANDS = {  # simplified and traditional forms
    "pause": {"stop", "pause", "stop playing", "stop it", "stop the music", "enough", "that's enough",
              "\u505c", "\u505c\u4e00\u4e0b", "\u6682\u505c", "\u66ab\u505c", "\u505c\u6b62", "\u505c\u6b62\u64ad\u653e", "\u6682\u505c\u64ad\u653e", "\u66ab\u505c\u64ad\u653e", "\u591f\u4e86", "\u5920\u4e86", "\u522b\u5f39\u4e86", "\u5225\u5f48\u4e86", "\u5148\u505c\u4e00\u4e0b"},
    # ting, ting yixia, zanting, tingzhi, tingzhi bofang, zanting bofang, gou le, bie tan le, xian ting yixia
    "resume": {"play", "continue", "keep playing", "resume", "go on", "start playing", "continue playing",
               "\u64ad\u653e", "\u5f00\u59cb\u64ad\u653e", "\u958b\u59cb\u64ad\u653e", "\u7ee7\u7eed", "\u7e7c\u7e8c", "\u7ee7\u7eed\u5f39", "\u7e7c\u7e8c\u5f48", "\u7ee7\u7eed\u64ad\u653e", "\u7e7c\u7e8c\u64ad\u653e", "\u63a5\u7740\u5f39", "\u63a5\u8457\u5f48", "\u63a5\u7740\u64ad\u653e", "\u63a5\u8457\u64ad\u653e"},
    # bofang, kaishi bofang, jixu, jixu tan, jixu bofang, jiezhe tan, jiezhe bofang
    "retry": {"retry", "try again", "\u91cd\u8bd5", "\u518d\u8bd5\u4e00\u6b21"},
    "cancel": {"cancel", "\u53d6\u6d88", "\u53d6\u6d88\u64ad\u653e"},
    "restart": {"start over", "from the beginning", "play it again", "play it from the beginning",
                "\u4ece\u5934\u5f39", "\u5f9e\u982d\u5f48", "\u4ece\u5934\u5f00\u59cb", "\u5f9e\u982d\u958b\u59cb", "\u4ece\u5934\u64ad\u653e", "\u5f9e\u982d\u64ad\u653e", "\u91cd\u65b0\u5f39", "\u91cd\u65b0\u5f48"},
    # congtou tan, congtou kaishi, congtou bofang, chongxin tan
}


def assistant_names(words: list[str]) -> list[str]:
    """The names inside the wake words: "Hey Jasmine" -> "Jasmine"."""
    return list(dict.fromkeys(re.sub(r"(?i)^hey\s+", "", w) for w in words))


FILLERS_START = (*dict.fromkeys(w.casefold() for w in WAKE_WORDS + assistant_names(WAKE_WORDS)),
                 "hey", "\u563f", "please", "okay", "ok", "uh", "um", "so",
                 "\u8bf7", "\u5443", "\u55ef", "\u90a3\u4e2a", "\u90a3", "\u597d\u7684", "\u597d", "\u54e6", "\u554a")
# qing, e, en, nage, na, hao de, hao, o, a
FILLERS_END = ("please", "\u5427", "\u554a", "\u5440", "\u54e6", "\u5443", "\u55ef")  # ba, a, ya, o, e, en


def drop_chinese_name(t: str) -> str | None:
    """t without a Chinese wake word at its start, also when written with other characters of the same sound
    (speech recognition may write xiao ming tongxue with another ming); None if it does not start with one."""
    for name in WAKE_WORDS:
        if t.startswith(name.casefold()):
            return t[len(name):].strip()
    try:
        from pypinyin import lazy_pinyin
    except ImportError:  # pypinyin is optional for text-only installations
        return None
    for name in WAKE_WORDS:
        if re.fullmatch(r"[\u4e00-\u9fff]+", name) and lazy_pinyin(t[:len(name)]) == lazy_pinyin(name):
            return t[len(name):].strip()
    return None


def strip_fillers(text: str) -> str:
    """The request without the name, "uh", "please" and the like around it, lower-case, no punctuation."""
    t = unicodedata.normalize("NFKC", text).casefold()
    t = re.sub(r"[^\w\s']", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    changed = True
    while changed and t:
        changed = False
        rest = drop_chinese_name(t)
        if rest is not None:
            t, changed = rest, True
        for f in FILLERS_START:
            whole_word = t == f or t.startswith(f + " ") if f.isascii() else t.startswith(f)  # "so" is not "sorry"
            if whole_word:
                t, changed = t[len(f):].strip(), True
        for f in FILLERS_END:
            if t.endswith(f) and t != f:
                t, changed = t[: -len(f)].strip(), True
    return t


def match_command(text: str) -> str | None:
    """Classify current-piece controls; new selections and general questions return None."""
    t = strip_fillers(text)
    for action, phrases in COMMANDS.items():
        if t in phrases:
            return action
    return None
