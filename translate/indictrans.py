"""Translates song lines to English with AI4Bharat's IndicTrans2, on this Mac.

Same input and output as nllb.py, so the server can use either. Reads JSON from stdin:
    {"groups": [{"lang": "tam_Taml", "lines": ["...", "..."]}, ...]}
and prints:
    {"lines": {"original line": "English", ...}}
Nothing is sent anywhere; the model runs on the Apple GPU when it can.
"""
import json
import sys

import torch
from IndicTransToolkit.processor import IndicProcessor
from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

MODEL = "ai4bharat/indictrans2-indic-en-dist-200M"
BATCH = 16


def main():
    request = json.load(sys.stdin)
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    tokenizer = AutoTokenizer.from_pretrained(MODEL, trust_remote_code=True)
    model = AutoModelForSeq2SeqLM.from_pretrained(MODEL, trust_remote_code=True).to(device).eval()
    processor = IndicProcessor(inference=True)
    result = {}
    for group in request["groups"]:
        lines = group["lines"]
        for start in range(0, len(lines), BATCH):
            batch = lines[start:start + BATCH]
            prepared = processor.preprocess_batch(batch, src_lang=group["lang"], tgt_lang="eng_Latn")
            inputs = tokenizer(prepared, padding="longest", truncation=True, max_length=256, return_tensors="pt").to(device)
            with torch.no_grad():
                # The model's own code doesn't work with the newer generation cache, so it stays off.
                out = model.generate(**inputs, num_beams=4, max_length=128, use_cache=False)
            texts = tokenizer.batch_decode(out, skip_special_tokens=True, clean_up_tokenization_spaces=True)
            for line, text in zip(batch, processor.postprocess_batch(texts, lang="eng_Latn")):
                result[line] = text.strip()
    json.dump({"lines": result}, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
