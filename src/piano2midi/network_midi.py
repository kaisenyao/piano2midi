"""Read-only checks of the selected macOS network MIDI session before playback."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys

_SWIFT = r'''
import Foundation
import CoreMIDI

let target = CommandLine.arguments[1]
let selectedPort = CommandLine.arguments[2]
var client = MIDIClientRef()
guard MIDIClientCreate("piano2midi connection check" as CFString, nil, nil, &client) == noErr else {
    print("UNKNOWN"); exit(0)
}
defer { MIDIClientDispose(client) }
func stringProperty(_ object: MIDIObjectRef, _ key: String) -> String? {
    var value: Unmanaged<CFString>?
    guard MIDIObjectGetStringProperty(object, key as CFString, &value) == noErr else { return nil }
    return value?.takeRetainedValue() as String?
}
var selected: MIDIEndpointRef = 0
for index in 0..<MIDIGetNumberOfDestinations() {
    let endpoint = MIDIGetDestination(index)
    if stringProperty(endpoint, "displayName") == selectedPort { selected = endpoint; break }
}
guard selected != 0 else { print("MISSING"); exit(1) }
var entity = MIDIEntityRef()
MIDIEndpointGetEntity(selected, &entity)
// Read-only compatibility check for the current Apple RTP driver. Never write its vendor properties.
var properties: Unmanaged<CFDictionary>?
if MIDIObjectGetDictionaryProperty(entity, "apple.midirtp.session" as CFString, &properties) == noErr,
   let config = properties?.takeRetainedValue() as? [String: Any],
   let peers = config["peers"] as? [[String: Any]] {
    if peers.contains(where: { ($0["name"] as? String) == target && ($0["state"] as? String) == "running" }) {
        print("READY"); exit(0)
    }
    print("MISSING"); exit(1)
}
// Unsupported driver data means unknown status, not a confirmed disconnection.
print("UNKNOWN")
'''


def check_network_midi(port_name: str) -> bool:
    """Check only the selected network route; unknown status must not block working output."""
    if sys.platform != "darwin":
        return True
    target = os.environ.get("PIANO2MIDI_NETWORK_PEER", "").strip()
    if not target:  # explicitly disable for machines using a different MIDI route
        return True
    network_port = os.environ.get("PIANO2MIDI_NETWORK_PORT", "piano2midi")
    if port_name.casefold() != network_port.casefold():
        return True
    from .library import CACHE_DIR

    try:
        cache = CACHE_DIR / "network-midi"
        cache.mkdir(parents=True, exist_ok=True)
        tag = hashlib.sha256(_SWIFT.encode()).hexdigest()[:16]
        binary = cache / ("check-" + tag)
        if not binary.is_file():
            compiler = shutil.which("swiftc")
            if not compiler:
                print("连接检查暂不可用；将尝试使用已选 MIDI 输出。", flush=True)
                return True
            source = cache / ("check-" + tag + ".swift")
            source.write_text(_SWIFT)
            part = binary.with_suffix(".part")
            result = subprocess.run([compiler, str(source), "-o", str(part)], capture_output=True, text=True, timeout=60)
            if result.returncode:
                print("连接检查暂不可用；将尝试使用已选 MIDI 输出。", flush=True)
                return True
            part.replace(binary)
        result = subprocess.run([str(binary.resolve()), target, port_name], capture_output=True, text=True, timeout=10)
        status = result.stdout.strip()
        if result.returncode == 0 and status == "READY":
            return True
        if result.returncode != 0 and status == "MISSING":
            print("设备尚未连接，请检查 MIDI 网络设置。", flush=True)
            return False
        # An unsupported inspection API is not evidence of a disconnected device.
        print("无法确认网络会话状态；将尝试使用已选 MIDI 输出。", flush=True)
        return True
    except (OSError, subprocess.TimeoutExpired):
        print("连接检查暂不可用；将尝试使用已选 MIDI 输出。", flush=True)
        return True
