"""Light, conservative clean-up of a raw piano transcription.

Every rule here keeps onsets, pitches and velocities exactly as the model
predicted. Only note releases and pedal controller events are touched.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field

import pretty_midi

SUSTAIN = 64
SOFT = 67

CLUSTER_WINDOW = 0.03      # notes starting within 30 ms count as one chord
LEGATO_MAX_GAP = 0.10      # only close silences up to 100 ms
LEGATO_MIN_DUR = 0.06      # leave grace notes and staccato alone
LEGATO_MAX_DUR = 0.60      # leave long, phrase-ending notes alone
LEGATO_OVERLAP = 0.02      # finger legato: hold 20 ms into the next note
INNER_VOICE_HOLD = 0.05    # a higher key held 50 ms past this onset makes this an inner voice


@dataclass
class CleanupReport:
    overlaps_fixed: int = 0
    soft_pedal_events_dropped: int = 0
    legato_fixes: int = 0
    final_pedal_release_added: bool = False
    pedal_smoothed: bool = False
    details: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "overlaps_fixed": self.overlaps_fixed,
            "soft_pedal_events_dropped": self.soft_pedal_events_dropped,
            "legato_fixes": self.legato_fixes,
            "final_pedal_release_added": self.final_pedal_release_added,
            "pedal_smoothed": self.pedal_smoothed,
        }


def pedal_intervals(ccs: list[pretty_midi.ControlChange]) -> list[tuple[float, float]]:
    """(down, up) intervals of the sustain pedal, from CC64 events."""
    intervals, down = [], None
    for c in sorted((c for c in ccs if c.number == SUSTAIN), key=lambda c: c.time):
        if c.value >= 64 and down is None:
            down = c.time
        elif c.value < 64 and down is not None:
            intervals.append((down, c.time))
            down = None
    if down is not None:
        intervals.append((down, float("inf")))
    return intervals


def _sounding_end(note: pretty_midi.Note, intervals, starts) -> float:
    """When the note stops sounding: key release, or pedal release if held by pedal."""
    i = bisect.bisect_right(starts, note.end) - 1
    if i >= 0 and intervals[i][0] <= note.end < intervals[i][1]:
        return intervals[i][1]
    return note.end


def fix_same_pitch_overlaps(notes, report: CleanupReport) -> None:
    by_pitch: dict[int, list] = {}
    for n in notes:
        by_pitch.setdefault(n.pitch, []).append(n)
    for group in by_pitch.values():
        group.sort(key=lambda n: n.start)
        for a, b in zip(group, group[1:]):
            if b.start < a.end:
                a.end = max(a.start + 0.005, b.start - 0.001)
                report.overlaps_fixed += 1


def close_melody_gaps(notes, ccs, report: CleanupReport) -> None:
    """Opt-in: close short silences in the top line (for cantabile, legato repertoire).

    For the highest note of each chord, if it is of ordinary length and the next
    top-line note (a different pitch) starts within 100 ms of it going silent,
    hold it 20 ms into that next note.

    Off by default: it assumes legato intent. On detached playing (e.g. Glenn Gould's
    Bach, almost no pedal) it changed ~18% of the notes and would erase the articulation.
    """
    intervals = pedal_intervals(ccs)
    starts = [a for a, _ in intervals]
    ordered = sorted(notes, key=lambda n: (n.start, -n.pitch))
    clusters: list[list] = []
    for n in ordered:
        if clusters and n.start - clusters[-1][0].start <= CLUSTER_WINDOW:
            clusters[-1].append(n)
        else:
            clusters.append([n])
    for cur, nxt in zip(clusters, clusters[1:]):
        top = max(cur, key=lambda n: n.pitch)
        nxt_top = max(nxt, key=lambda n: n.pitch)
        if nxt_top.pitch < 60 or top.pitch < 60:
            continue  # bass/left-hand region, keep articulation as played
        if top.pitch in {n.pitch for n in nxt}:
            continue  # repeated note needs its release
        duration = top.end - top.start
        if not (LEGATO_MIN_DUR <= duration <= LEGATO_MAX_DUR):
            continue
        # a higher key still held well after `top` starts means `top` is an inner voice
        # (e.g. a left-hand chord under a sustained melody note); a short legato overlap is fine
        if any(o.pitch > top.pitch and o.start < top.start and o.end > top.start + INNER_VOICE_HOLD
               for o in notes):
            continue
        gap = nxt_top.start - max(_sounding_end(n, intervals, starts) for n in cur if n.pitch >= 60)
        if 0 < gap <= LEGATO_MAX_GAP:
            old = top.end
            top.end = nxt_top.start + LEGATO_OVERLAP
            report.legato_fixes += 1
            report.details.append(
                f"legato {pretty_midi.note_number_to_name(top.pitch)}@{top.start:.3f}s "
                f"end {old:.3f}->{top.end:.3f} (hole {gap * 1000:.0f} ms)"
            )


def ensure_final_pedal_release(notes, ccs, report: CleanupReport) -> None:
    pedal = sorted((c for c in ccs if c.number == SUSTAIN), key=lambda c: c.time)
    if pedal and pedal[-1].value >= 64:
        last = max([n.end for n in notes] + [pedal[-1].time])
        ccs.append(pretty_midi.ControlChange(SUSTAIN, 0, last + 0.05))
        report.final_pedal_release_added = True


def smooth_pedal(ccs, report: CleanupReport, travel: float = 0.05) -> list:
    """Optional: replace instant 0/127 jumps with a short foot-travel ramp (off by default)."""
    pedal = sorted((c for c in ccs if c.number == SUSTAIN), key=lambda c: c.time)
    others = [c for c in ccs if c.number != SUSTAIN]
    changes, state = [], False
    for c in pedal:
        down = c.value >= 64
        if down != state:
            changes.append((c.time, down))
            state = down
    out = []
    for i, (t, down) in enumerate(changes):
        lo = changes[i - 1][0] if i else 0.0
        hi = changes[i + 1][0] if i + 1 < len(changes) else t + 1.0
        h = min(travel / 2, (t - lo) / 3, (hi - t) / 3)
        for dt, frac in ((-h, 0.0), (-h / 2, 0.25), (0.0, 0.5), (h / 2, 0.75), (h, 1.0)):
            value = round(127 * frac) if down else round(127 * (1 - frac))
            out.append(pretty_midi.ControlChange(SUSTAIN, int(value), max(0.0, t + dt)))
    report.pedal_smoothed = True
    return others + out


def clean(midi: pretty_midi.PrettyMIDI, *, keep_soft_pedal: bool = False,
          legato: bool = False, pedal_ramps: bool = False) -> tuple[pretty_midi.PrettyMIDI, CleanupReport]:
    """Return a cleaned single-track piano MIDI and a report of what changed."""
    report = CleanupReport()
    notes = [pretty_midi.Note(n.velocity, n.pitch, n.start, n.end)
             for inst in midi.instruments for n in inst.notes]
    ccs = [pretty_midi.ControlChange(c.number, c.value, c.time)
           for inst in midi.instruments for c in inst.control_changes]
    for n in notes:
        n.velocity = min(127, max(1, int(n.velocity)))

    if not keep_soft_pedal:
        report.soft_pedal_events_dropped = sum(1 for c in ccs if c.number == SOFT)
        ccs = [c for c in ccs if c.number != SOFT]

    fix_same_pitch_overlaps(notes, report)
    if legato:
        close_melody_gaps(notes, ccs, report)
        fix_same_pitch_overlaps(notes, report)  # safety net, should be a no-op
    ensure_final_pedal_release(notes, ccs, report)
    if pedal_ramps:
        ccs = smooth_pedal(ccs, report)

    out = pretty_midi.PrettyMIDI(resolution=960, initial_tempo=120.0)
    piano = pretty_midi.Instrument(program=0, name="Piano")
    piano.notes = sorted(notes, key=lambda n: (n.start, n.pitch))
    piano.control_changes = sorted(ccs, key=lambda c: c.time)
    out.instruments.append(piano)
    return out, report
