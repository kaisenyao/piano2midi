"""Run by the user: real model, simulated playback, temporary library, no microphone or TTS."""
import argparse
import json

from piano2midi.chat import INSTRUCTIONS, TOOLS
from piano2midi.llm import make_chat
from test_playback_interactions import PlaybackTests

# Each case starts with \u8d1d\u52a0\u5c14\u6e56\u7554 paused at 22 seconds.
# 'replay' after a disconnection is ambiguous: allow resume or restart of the SAME piece.
# Explicit 'from the beginning' cases still require zero; continuation cases require 22.
CASES = [
    ('\u5443\uff0c\u597d\u50cf\u65ad\u5f00\u4e86\uff0c\u4f60\u53ef\u4ee5\u518d\u5e2e\u6211\u91cd\u65b0\u64ad\u653e\u5417\uff1f', 'replay'),
    ('\u5c0f\u660e\u540c\u5b66\u597d\u50cf\u53c8\u65ad\u5f00\u4e86\uff0c\u4f60\u53ef\u4ee5\u770b\u770b\u80fd\u5e2e\u6211\u7ee7\u7eed\u64ad\u653e\u5417\uff1f', 'resume'),
    ('\u5c0f\u8317\u540c\u5b66\uff0c\u6211\u60f3\u7ee7\u7eed\u542c\u8fd9\u9996\u6b4c\u3002', 'resume'),
    ('\u6211\u5df2\u7ecf\u8fde\u63a5\u597d\u4e86\u3002', 'pending'),
    ('\u6211\u5c1d\u8bd5\u8fde\u63a5\u4e86\uff0c\u4f60\u53ef\u4ee5\u518d\u8bd5\u8bd5\u5417\uff1f', 'resume'),
    ('\u521a\u624d\u4e2d\u65ad\u4e86\uff0c\u80fd\u4ece\u65ad\u6389\u7684\u5730\u65b9\u63a5\u7740\u5f39\u5417\uff1f', 'resume'),
    ('\u8bf7\u4ece\u5f00\u5934\u518d\u7ed9\u6211\u5f39\u4e00\u904d\u521a\u624d\u90a3\u9996\u3002', 'restart'),
    ('Could you carry on with the piece we were hearing?', 'resume'),
    ('The connection is fixed. Can you give it another try?', 'pending'),
    ('\u8fd9\u9996\u5148\u505c\u4e00\u4e0b\uff0c\u6211\u60f3\u8bf4\u53e5\u8bdd\u3002', 'pause'),
    ('\u4e0d\u7528\u91cd\u8bd5\u4e86\uff0c\u53d6\u6d88\u521a\u624d\u7684\u64ad\u653e\u5427\u3002', 'cancel'),
    ('\u5148\u522b\u7ee7\u7eed\uff0c\u6211\u8fd8\u6ca1\u8fde\u597d\u3002', 'no_action'),
    ('\u7b49\u6211\u8fde\u597d\u4e86\u518d\u7ee7\u7eed\uff0c\u73b0\u5728\u5148\u522b\u64ad\u3002', 'no_action'),
    ('\u6362\u6210\u5fb7\u5f6a\u897f\u7684\u6708\u5149\u5427\u3002', 'new_song'),
    ('\u597d\u50cf\u53c8\u65ad\u5f00\u4e86\uff0c\u80fd\u5e2e\u6211\u7ee7\u7eed\u64ad\u653e\u8fd9\u9996\u5417\uff1f', 'offline_resume'),
    ('\u597d\u50cf\u53c8\u65ad\u5f00\u4e86\uff0c\u80fd\u4ece\u5934\u91cd\u65b0\u64ad\u653e\u5417\uff1f', 'offline_restart'),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--list', action='store_true', help='list cases without calling the model')
    args = parser.parse_args()
    if args.list:
        for index, (text, expected) in enumerate(CASES, 1): print(index, expected, text)
        return 0
    print('\u771f\u5b9e\u6a21\u578b\u6d4b\u8bd5\uff1a\u53ea\u6a21\u62df\u64ad\u653e\u548c\u8fde\u63a5\uff0c\u4e0d\u64cd\u4f5c\u5b9e\u9645\u8bbe\u5907\u3001\u66f2\u5e93\u6216\u8bed\u97f3\u8f93\u51fa\u3002')
    failures = 0
    for index, (text, expected) in enumerate(CASES, 1):
        fixture = PlaybackTests()
        result = 'PASS'
        trace = []
        try:
            fixture.setUp()
            fixture.prime()
            if expected == 'pending':
                fixture.connected = False
                fixture.a.control_playback('resume')
                fixture.connected = True
            if expected.startswith('offline_'): fixture.connected = False
            original = fixture.a.run_tool
            def safe_tool(name, arguments):
                trace.append((name, arguments))
                if name not in {'control_playback', 'search_library', 'play_recording'}:
                    return json.dumps({'error': 'Only playback and library search are enabled in this test.'})
                return original(name, arguments)
            fixture.a.run_tool = safe_tool
            chat, label = make_chat(INSTRUCTIONS, TOOLS)
            answer = fixture.a.ask(chat, text)
            if fixture.player._thread: fixture.player._thread.join()
            player, pending = fixture.player, fixture.a.pending
            if expected in {'resume', 'pending', 'restart', 'new_song', 'replay'}:
                offset = 0 if expected in {'restart', 'new_song'} else 22
                title = '\u6708\u5149' if expected == 'new_song' else '\u8d1d\u52a0\u5c14\u6e56\u7554'
                offsets = {0, 22} if expected == 'replay' else {offset}
                okay = player.state == 'playing' and player._from in offsets and player.current[1] == title
            elif expected.startswith('offline_'):
                offset = 0 if expected == 'offline_restart' else 22
                okay = pending is not None and pending.from_s == offset and '\u8bbe\u5907\u548c\u94a2\u7434' in answer
            elif expected == 'cancel':
                okay = player.state == 'stopped' and pending is None
            else:
                okay = player.state == 'paused' and player.position == 22
                if expected == 'no_action':
                    okay = okay and not any(name == 'play_recording' or name == 'control_playback' and a.get('action') in {'resume','restart','retry'} for name,a in trace)
            if not okay: result = 'FAIL'
        except Exception as exc:
            result = 'ERROR (' + type(exc).__name__ + ')'
        finally:
            fixture.doCleanups()
        if result != 'PASS': failures += 1
        print(f'{index:02d} {result}: {text}')
        if result != 'PASS': print('  tool calls:', json.dumps(trace, ensure_ascii=False))
    print(f'Result: {len(CASES)-failures}/{len(CASES)} passed')
    return int(failures != 0)


if __name__ == '__main__':
    raise SystemExit(main())
