"""Local-only SpeechBrain SepFormer WHAMR (official architecture, 8kHz, two sources).

No from_hparams/YAML execution, hub lookup or implicit model download. Only the three
reviewed tensor state dictionaries are loaded, with weights_only=True.
"""

from __future__ import annotations

from pathlib import Path


def load_separator(model_path: Path, *, device: str = "cpu", cpu_threads: int = 4):
    path = Path(model_path)
    if not all(
        (path / f"{name}.ckpt").is_file() for name in ("encoder", "decoder", "masknet")
    ):
        raise ValueError("SepFormer local weights missing")
    import torch
    from speechbrain.lobes.models.dual_path import (
        Decoder,
        Dual_Path_Model,
        Encoder,
        SBTransformerBlock,
    )
    from torchaudio.functional import resample

    torch.set_num_threads(cpu_threads)
    encoder = Encoder(kernel_size=16, out_channels=256)
    decoder = Decoder(
        in_channels=256, out_channels=1, kernel_size=16, stride=8, bias=False
    )

    def transformer():
        return SBTransformerBlock(
            num_layers=8,
            d_model=256,
            nhead=8,
            d_ffn=1024,
            dropout=0,
            use_positional_encoding=True,
            norm_before=True,
        )

    masknet = Dual_Path_Model(
        num_spks=2,
        in_channels=256,
        out_channels=256,
        num_layers=2,
        K=250,
        intra_model=transformer(),
        inter_model=transformer(),
        norm="ln",
        linear_layer_after_inter_intra=False,
        skip_around_intra=True,
    )
    for name, model in [
        ("encoder", encoder),
        ("decoder", decoder),
        ("masknet", masknet),
    ]:
        model.load_state_dict(
            torch.load(path / f"{name}.ckpt", map_location="cpu", weights_only=True)
        )
        model.to(device).eval()

    def separate(audio):
        x = torch.from_numpy(audio.copy()).unsqueeze(0).to(device)
        with torch.inference_mode():
            x = resample(x, 16000, 8000)
            encoded = encoder(x)
            masks = masknet(encoded)
            sources = [decoder(encoded * masks[i]).reshape(-1) for i in range(2)]
            sources = [resample(s, 8000, 16000) for s in sources]
            # Conv decoder may drop an ending frame. Match duration without altering original audio.
            sources = [
                torch.nn.functional.pad(s, (0, max(0, len(audio) - s.numel())))[
                    : len(audio)
                ]
                for s in sources
            ]
            if any(not torch.isfinite(s).all() for s in sources):
                raise ValueError("Non-finite separator output")
            return [s.cpu().numpy() for s in sources]

    return separate
