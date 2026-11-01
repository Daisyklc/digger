#!/usr/bin/env python3
"""Download and convert the anime-whisper ASR model to CTranslate2 format.

faster-whisper runs CTranslate2 models, so the Hugging Face checkpoint must be
converted once. Run this on the cloud host before starting the server:

    python drama_asr/scripts/prepare_model.py \
        --model litagin/anime-whisper \
        --output models/anime-whisper-ct2 \
        --quantization float16

The resulting directory is what you pass as ASR_MODEL.
"""

from __future__ import annotations

import argparse
import subprocess
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert a Whisper model to CTranslate2")
    parser.add_argument("--model", default="litagin/anime-whisper", help="HF model id or path")
    parser.add_argument("--output", default="models/anime-whisper-ct2", help="output directory")
    parser.add_argument(
        "--quantization",
        default="float16",
        choices=["float16", "int8_float16", "int8", "float32"],
        help="float16 fits a 16 GB GPU; int8_float16 saves more memory",
    )
    args = parser.parse_args()

    cmd = [
        "ct2-transformers-converter",
        "--model",
        args.model,
        "--output_dir",
        args.output,
        "--quantization",
        args.quantization,
        "--copy_files",
        "tokenizer.json",
        "preprocessor_config.json",
        "--force",
    ]
    print("Running:", " ".join(cmd), flush=True)
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError:
        print(
            "ct2-transformers-converter not found. Install deps first:\n"
            "    pip install -r drama_asr/requirements-server.txt",
            file=sys.stderr,
        )
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"conversion failed: {exc}", file=sys.stderr)
        return exc.returncode
    print(f"Done. Set ASR_MODEL={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
