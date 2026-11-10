"""Translates song lines to English with Meta's NLLB model, on this Mac.

Run by server.py inside translate/.venv. Reads JSON from stdin:
    {"groups": [{"lang": "tam_Taml", "lines": ["...", "..."]}, ...]}
and prints:
    {"lines": {"original line": "English", ...}}
Nothing is sent anywhere; the model runs on the Apple GPU when it can.
"""
import json
import sys

import torch
from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

MODEL = "facebook/nllb-200-distilled-600M"
BATCH = 16


def main():
    request = json.load(sys.stdin)
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    model = AutoModelForSeq2SeqLM.from_pretrained(MODEL).to(device).eval()
    english_id = None
    result = {}
    for group in request["groups"]:
        tokenizer = AutoTokenizer.from_pretrained(MODEL, src_lang=group["lang"])
        english_id = english_id or tokenizer.convert_tokens_to_ids("eng_Latn")
        lines = group["lines"]
        for start in range(0, len(lines), BATCH):
            batch = lines[start:start + BATCH]
            inputs = tokenizer(batch, return_tensors="pt", padding=True, truncation=True, max_length=128).to(device)
            with torch.no_grad():
                out = model.generate(**inputs, forced_bos_token_id=english_id, max_new_tokens=96, num_beams=4)
            for line, text in zip(batch, tokenizer.batch_decode(out, skip_special_tokens=True)):
                result[line] = text.strip()
    json.dump({"lines": result}, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
