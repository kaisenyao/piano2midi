import pretty_midi

from piano2midi.cleanup import clean


def make(notes, ccs=()):
    m = pretty_midi.PrettyMIDI()
    inst = pretty_midi.Instrument(0)
    inst.notes = [pretty_midi.Note(v, p, s, e) for p, s, e, v in notes]
    inst.control_changes = [pretty_midi.ControlChange(n, v, t) for n, v, t in ccs]
    m.instruments.append(inst)
    return m


def notes_of(m):
    return sorted(m.instruments[0].notes, key=lambda n: (n.start, n.pitch))


def test_onsets_pitches_velocities_are_never_changed():
    raw = make([(72, 0.0, 0.25, 60), (74, 0.30, 0.55, 64), (48, 0.0, 0.9, 40), (74, 0.6, 0.8, 50)],
               [(64, 127, 0.1), (64, 0, 0.2), (67, 127, 0.0), (67, 0, 1.0)])
    out, _ = clean(raw, legato=True, pedal_ramps=True)
    key = lambda n: (round(n.start, 4), n.pitch, n.velocity)
    assert sorted(map(key, raw.instruments[0].notes)) == sorted(map(key, out.instruments[0].notes))


def test_melody_gaps_are_kept_by_default():
    out, rep = clean(make([(72, 0.0, 0.25, 60), (74, 0.30, 0.55, 64)]))
    assert rep.legato_fixes == 0
    assert notes_of(out)[0].end == 0.25


def test_short_melody_hole_is_closed_with_small_overlap():
    out, rep = clean(make([(72, 0.0, 0.25, 60), (74, 0.30, 0.55, 64)]), legato=True)
    first = notes_of(out)[0]
    assert rep.legato_fixes == 1
    assert abs(first.end - 0.32) < 1e-6


def test_long_gap_rest_is_kept():
    out, rep = clean(make([(72, 0.0, 0.25, 60), (74, 0.50, 0.75, 64)]), legato=True)
    assert rep.legato_fixes == 0
    assert notes_of(out)[0].end == 0.25


def test_grace_note_and_long_note_are_left_alone():
    out, rep = clean(make([(76, 0.0, 0.04, 60), (74, 0.07, 0.30, 64),   # grace note
                           (72, 1.0, 1.9, 60), (71, 1.95, 2.3, 60)]), legato=True)   # long phrase-ending note
    assert rep.legato_fixes == 0


def test_repeated_note_keeps_its_release():
    out, rep = clean(make([(72, 0.0, 0.25, 60), (72, 0.30, 0.55, 64)]), legato=True)
    assert rep.legato_fixes == 0


def test_hole_covered_by_pedal_is_not_touched():
    out, rep = clean(make([(72, 0.0, 0.25, 60), (74, 0.30, 0.55, 64)], [(64, 127, 0.05), (64, 0, 0.6)]), legato=True)
    assert rep.legato_fixes == 0


def test_left_hand_is_not_legato_patched():
    out, rep = clean(make([(48, 0.0, 0.2, 60), (50, 0.25, 0.4, 60)]), legato=True)
    assert rep.legato_fixes == 0


def test_same_pitch_overlap_is_fixed():
    out, rep = clean(make([(60, 0.0, 0.5, 60), (60, 0.3, 0.6, 60)]), legato=False)
    a, b = notes_of(out)
    assert rep.overlaps_fixed == 1 and a.end <= b.start


def test_soft_pedal_dropped_by_default_and_kept_on_request():
    raw = make([(60, 0, 1, 60)], [(67, 127, 0.0), (67, 0, 1.0)])
    out, rep = clean(raw)
    assert rep.soft_pedal_events_dropped == 2
    assert not [c for c in out.instruments[0].control_changes if c.number == 67]
    out, _ = clean(raw, keep_soft_pedal=True)
    assert len([c for c in out.instruments[0].control_changes if c.number == 67]) == 2


def test_hanging_pedal_is_released_at_end():
    out, rep = clean(make([(60, 0, 1, 60)], [(64, 127, 0.5)]))
    last = sorted(out.instruments[0].control_changes, key=lambda c: c.time)[-1]
    assert rep.final_pedal_release_added and last.value == 0 and last.time >= 1.0


def test_pedal_ramps_keep_switch_points():
    out, _ = clean(make([(60, 0, 2, 60)], [(64, 127, 0.5), (64, 0, 1.5)]), pedal_ramps=True)
    ped = sorted((c for c in out.instruments[0].control_changes if c.number == 64), key=lambda c: c.time)
    mid = [c for c in ped if c.value == 64]
    assert [round(c.time, 3) for c in mid] == [0.5, 1.5]
