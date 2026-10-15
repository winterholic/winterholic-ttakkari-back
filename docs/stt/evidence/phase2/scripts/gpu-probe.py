"""Read-only GPU access diagnostic; does not load any STT weights."""
import ctypes
import json
import subprocess
import sys

import torch

out = {'torch': torch.__version__, 'mps_built': torch.backends.mps.is_built(),
       'mps_available': torch.backends.mps.is_available(), 'cuda_available': torch.cuda.is_available()}
try:
    value = torch.ones(2, device='mps')
    torch.mps.synchronize()
    out['mps_tensor'] = 'ok'
except Exception as exc:
    out['mps_tensor_error'] = type(exc).__name__ + ': ' + str(exc)[:250]
metal = ctypes.CDLL('/System/Library/Frameworks/Metal.framework/Metal')
metal.MTLCreateSystemDefaultDevice.restype = ctypes.c_void_p
out['metal_default_device_found'] = bool(metal.MTLCreateSystemDefaultDevice())
mlx = subprocess.run([sys.executable, '-c', 'import mlx.core as mx; print(mx.default_device())'],
                     capture_output=True, text=True, timeout=60)
out['mlx_exit_code'] = mlx.returncode
out['mlx_message'] = (mlx.stderr or mlx.stdout)[-600:]
print(json.dumps(out, indent=2))
