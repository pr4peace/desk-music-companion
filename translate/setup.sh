#!/bin/bash
# Optional: installs translation models so Indian-language lyrics are translated on this Mac first,
# before falling back to Google. Needs uv (https://docs.astral.sh/uv/).
#   - Meta's NLLB (about 3 GB with its packages) needs no account.
#   - AI4Bharat's IndicTrans2 (about 1 GB more) is better for Tamil, Telugu and most Indian languages.
#     It needs a free Hugging Face account: accept the terms at
#     https://huggingface.co/ai4bharat/indictrans2-indic-en-dist-200M, then run
#     translate/.venv/bin/hf auth login, then run this script again.
set -euo pipefail
cd "$(dirname "$0")"
command -v uv >/dev/null || { echo "uv is missing. Install it: curl -LsSf https://astral.sh/uv/install.sh | sh"; exit 1; }
[ -x .venv/bin/python ] || uv venv --python 3.12 .venv
# IndicTrans2's toolkit and model code need transformers 4.
uv pip install --python .venv/bin/python torch "transformers>=4.45,<5" sentencepiece IndicTransToolkit
.venv/bin/python -c "from huggingface_hub import snapshot_download; snapshot_download('facebook/nllb-200-distilled-600M', allow_patterns=['*.json', '*.model', 'pytorch_model.bin'])"
if .venv/bin/python -c "from huggingface_hub import snapshot_download; snapshot_download('ai4bharat/indictrans2-indic-en-dist-200M', allow_patterns=['*.json', '*.py', 'model.SRC', 'model.TGT', 'model.safetensors'])" 2>/dev/null; then
  echo "NLLB and IndicTrans2 are ready."
else
  echo "NLLB is ready. IndicTrans2 was skipped: log in to Hugging Face and accept its terms (see the top of this file), then run this again."
fi
echo "Restart the server from the menu bar to start using them."
