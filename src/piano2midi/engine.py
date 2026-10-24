"""Transkun piano transcription engine (loaded once, reused for every file)."""

from __future__ import annotations

import threading
from importlib import resources
from importlib.metadata import version

import pretty_midi


def pick_device(requested: str = "auto") -> str:
    import torch

    if requested != "auto":
        return requested
    if torch.cuda.is_available():
        return "cuda"
    # Apple MPS is opt-in (--device mps): CPU is reliable and fast enough on Apple Silicon.
    return "cpu"


class TranskunEngine:
    name = "transkun"

    def __init__(self, device: str = "auto", threads: int | None = None):
        import moduleconf
        import torch

        self.device = pick_device(device)
        if threads:
            torch.set_num_threads(threads)
        base = resources.files("transkun") / "pretrained"
        with resources.as_file(base / "2.0.conf") as conf_path:
            conf_manager = moduleconf.parseFromFile(str(conf_path))
        model_cls = conf_manager["Model"].module.TransKun
        self.model = model_cls(conf=conf_manager["Model"].config)
        with resources.as_file(base / "2.0.pt") as weight_path:
            checkpoint = torch.load(weight_path, map_location="cpu", weights_only=False)
        state = checkpoint.get("best_state_dict", checkpoint.get("state_dict"))
        self.model.load_state_dict(state, strict=False)
        self.model.to(self.device).eval()
        self.sample_rate = int(self.model.fs)
        self.version = f"transkun {version('transkun')} (pretrained 2.0)"
        self._lock = threading.Lock()

    def transcribe(self, audio) -> pretty_midi.PrettyMIDI:
        """audio: float32 numpy array (samples, channels) at self.sample_rate."""
        import torch
        from transkun.Data import writeMidi

        with self._lock, torch.no_grad():
            x = torch.from_numpy(audio).to(self.device)
            notes = self.model.transcribe(x, discardSecondHalf=False)
        return writeMidi(notes)
