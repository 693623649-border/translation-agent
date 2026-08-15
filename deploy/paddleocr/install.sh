#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd -- "${script_dir}/../.." && pwd)"
venv_dir="${PADDLEOCR_VENV_DIR:-${repo_dir}/work/venvs/paddleocr-cu118}"
python_executable="${PADDLEOCR_PYTHON:-python3}"

python_version="$("${python_executable}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ "${python_version}" != "3.12" ]]; then
  printf 'PaddleOCR requires Python 3.12; %s reports %s.\n' \
    "${python_executable}" "${python_version}" >&2
  exit 1
fi

"${python_executable}" -m venv "${venv_dir}"
"${venv_dir}/bin/python" -m pip install --upgrade pip setuptools wheel
"${venv_dir}/bin/python" -m pip install \
  "paddlepaddle-gpu==3.3.0" \
  --index-url "https://www.paddlepaddle.org.cn/packages/stable/cu118/" \
  --extra-index-url "https://pypi.org/simple"
"${venv_dir}/bin/python" -m pip install \
  --requirement "${script_dir}/requirements.lock"
"${venv_dir}/bin/python" -m pip check

"${venv_dir}/bin/python" - <<'PY'
from importlib.metadata import version

import paddle

print(f"paddle={paddle.__version__}")
print(f"paddleocr={version('paddleocr')}")
print(f"compiled_with_cuda={paddle.device.is_compiled_with_cuda()}")
print(f"visible_gpu_count={paddle.device.cuda.device_count()}")
if not paddle.device.is_compiled_with_cuda():
    raise SystemExit("The installed PaddlePaddle wheel has no CUDA support.")
if paddle.device.cuda.device_count() < 2:
    raise SystemExit("Two visible GPUs are required by pipeline.local-gpu.toml.")
PY

printf 'PaddleOCR environment ready: %s\n' "${venv_dir}"
