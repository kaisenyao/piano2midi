"""Shared playback controls for chat and voice; song selection remains with the model."""
import os
import re
import unicodedata

WAKE_WORDS = [w.strip() for w in os.environ.get("PIANO2MIDI_WAKE_WORDS", "小茗同学,Hey Jasmine").split(",") if w.strip()]

# Short commands handled without the model, in English and in simplified and traditional Chinese.
COMMANDS = {
    "pause": {"stop", "pause", "stop playing", "stop it", "stop the music", "enough", "that's enough",
              "停", "停一下", "暂停", "暫停", "停止", "停止播放", "暂停播放", "暫停播放", "够了", "夠了", "别弹了", "別彈了", "先停一下"},
    "resume": {"play", "continue", "keep playing", "resume", "go on", "start playing", "continue playing",
               "播放", "开始播放", "開始播放", "继续", "繼續", "继续弹", "繼續彈", "继续播放", "繼續播放", "接着弹", "接著彈", "接着播放", "接著播放"},
    "retry": {"retry", "try again", "重试", "再试一次"},
    "cancel": {"cancel", "取消", "取消播放"},
    "restart": {"start over", "from the beginning", "play it again", "play it from the beginning",
                "从头弹", "從頭彈", "从头开始", "從頭開始", "从头播放", "從頭播放", "重新弹", "重新彈"},
}


def is_chinese(text: str) -> bool:
    return re.search(r"[\u3400-\u9fff]", text) is not None


def assistant_names(words: list[str]) -> list[str]:
    """The names inside the wake words: "Hey Jasmine" -> "Jasmine"."""
    return list(dict.fromkeys(re.sub(r"(?i)^hey\s+", "", w) for w in words))


FILLERS_START = (*dict.fromkeys(w.casefold() for w in WAKE_WORDS + assistant_names(WAKE_WORDS)),
                 "hey", "嘿", "please", "okay", "ok", "uh", "um", "so",
                 "请", "呃", "嗯", "那个", "那", "好的", "好", "哦", "啊")
FILLERS_END = ("please", "吧", "啊", "呀", "哦", "呃", "嗯")


def spell_names(text: str) -> str:
    """text with her Chinese names written as they are configured. Speech recognition hears only the sound and
    may write 小茗同学 as 小明同学; the same goes for anything typed with other characters of the same sound."""
    try:
        from pypinyin import lazy_pinyin
    except ImportError:  # pypinyin is optional for text-only installations
        return text
    for name in WAKE_WORDS:
        if re.fullmatch(r"[\u4e00-\u9fff]+", name):
            sound, n, i = lazy_pinyin(name), len(name), 0
            while i + n <= len(text):
                if text[i:i + n] != name and lazy_pinyin(text[i:i + n]) == sound:
                    text = text[:i] + name + text[i + n:]
                i += 1
    return text


def strip_fillers(text: str) -> str:
    """The request without the name, "uh", "please" and the like around it, lower-case, no punctuation."""
    t = spell_names(unicodedata.normalize("NFKC", text)).casefold()
    t = re.sub(r"[^\w\s']", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    changed = True
    while changed and t:
        changed = False
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
