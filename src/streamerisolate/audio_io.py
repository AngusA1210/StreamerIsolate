"""Device discovery and I/O helpers built on sounddevice."""

from __future__ import annotations

from dataclasses import dataclass

import sounddevice as sd


@dataclass
class DeviceInfo:
    index: int
    name: str
    max_input_channels: int
    max_output_channels: int
    default_samplerate: float


def list_devices() -> list[DeviceInfo]:
    devices = sd.query_devices()
    return [
        DeviceInfo(
            index=i,
            name=d["name"],
            max_input_channels=d["max_input_channels"],
            max_output_channels=d["max_output_channels"],
            default_samplerate=d["default_samplerate"],
        )
        for i, d in enumerate(devices)
    ]


def print_devices() -> None:
    for d in list_devices():
        kind = []
        if d.max_input_channels > 0:
            kind.append(f"in={d.max_input_channels}")
        if d.max_output_channels > 0:
            kind.append(f"out={d.max_output_channels}")
        print(f"[{d.index:2d}] {d.name}  ({', '.join(kind)}, {d.default_samplerate:.0f} Hz)")


def resolve_device(identifier: str | int) -> int:
    """Resolve a device by index or case-insensitive substring match on its name."""
    if isinstance(identifier, int) or str(identifier).isdigit():
        return int(identifier)

    needle = str(identifier).lower()
    matches = [d for d in list_devices() if needle in d.name.lower()]
    if not matches:
        raise ValueError(f"No audio device matching '{identifier}'")
    if len(matches) > 1:
        names = ", ".join(f"[{m.index}] {m.name}" for m in matches)
        raise ValueError(f"Ambiguous device '{identifier}', matches: {names}")
    return matches[0].index
