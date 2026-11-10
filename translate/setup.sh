#!/bin/bash
# Optional: installs Meta's NLLB translation model (about 3 GB) so Indian-language lyrics are
# translated on this Mac first, before falling back to Google. Needs uv (https://docs.astral.sh/uv/).
set -euo pipefail
cd "$(dirname "$0")"
command -v uv >/dev/null || { echo "uv is missing. Install it: curl -LsSf https://astral.sh/uv/install.sh | sh"; exit 1; }
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python torch transformers sentencepiece
.venv/bin/python -c "from huggingface_hub import snapshot_download; snapshot_download('facebook/nllb-200-distilled-600M', allow_patterns=['*.json', '*.model', 'pytorch_model.bin'])"
echo "NLLB is ready. Restart the server from the menu bar to start using it."
