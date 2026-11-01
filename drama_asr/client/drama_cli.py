#!/usr/bin/env python3
"""Local client for the cloud drama ASR + translation service.

Your audio stays on your machine: this uploads a file to the cloud host,
polls until processing finishes, then writes the subtitle files next to your
chosen output directory. Nothing is kept on the server afterwards.

Usage:
    python -m drama_asr.client.drama_cli \
        --server https://your-host:8000 \
        --api-key "$DRAMA_API_KEY" \
        --out-dir ./out \
        episode01.wav episode02.flac
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import requests


def _headers(api_key: str | None) -> dict:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def submit(server: str, api_key: str | None, path: str) -> str:
    url = server.rstrip("/") + "/api/jobs"
    with open(path, "rb") as fh:
        resp = requests.post(
            url,
            headers=_headers(api_key),
            files={"file": (os.path.basename(path), fh)},
            timeout=600,
        )
    resp.raise_for_status()
    return resp.json()["job_id"]


def poll(server: str, api_key: str | None, job_id: str, interval: float) -> None:
    url = f"{server.rstrip('/')}/api/jobs/{job_id}"
    last = ""
    while True:
        resp = requests.get(url, headers=_headers(api_key), timeout=60)
        resp.raise_for_status()
        data = resp.json()
        status = data["status"]
        line = f"  [{status}] stage={data['stage']} progress={data['progress']:.0%}"
        if line != last:
            print(line, flush=True)
            last = line
        if status == "done":
            return
        if status == "error":
            raise RuntimeError(data.get("error") or "job failed")
        time.sleep(interval)


def fetch_result(server: str, api_key: str | None, job_id: str) -> dict:
    url = f"{server.rstrip('/')}/api/jobs/{job_id}/result"
    resp = requests.get(url, headers=_headers(api_key), timeout=120)
    resp.raise_for_status()
    return resp.json()


def write_outputs(result: dict, out_dir: str, stem: str, formats: list[str]) -> list[str]:
    os.makedirs(out_dir, exist_ok=True)
    written: list[str] = []
    mapping = {
        "ja": ("srt_ja", f"{stem}.ja.srt"),
        "zh": ("srt_zh", f"{stem}.zh.srt"),
        "bilingual": ("srt_bilingual", f"{stem}.bilingual.srt"),
    }
    for fmt in formats:
        if fmt == "json":
            dest = os.path.join(out_dir, f"{stem}.json")
            with open(dest, "w", encoding="utf-8") as fh:
                json.dump(result, fh, ensure_ascii=False, indent=2)
            written.append(dest)
            continue
        key, name = mapping[fmt]
        content = result.get(key)
        if not content:
            print(f"  (no {fmt} output available; skipped)", flush=True)
            continue
        dest = os.path.join(out_dir, name)
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(content)
        written.append(dest)
    return written


def process_one(args, path: str) -> list[str]:
    print(f"Uploading {path} ...", flush=True)
    job_id = submit(args.server, args.api_key, path)
    print(f"  job_id={job_id}", flush=True)
    poll(args.server, args.api_key, job_id, args.poll_interval)
    result = fetch_result(args.server, args.api_key, job_id)
    stem = os.path.splitext(os.path.basename(path))[0]
    written = write_outputs(result, args.out_dir, stem, args.formats)
    for dest in written:
        print(f"  wrote {dest}", flush=True)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Drama ASR + translation client")
    parser.add_argument("inputs", nargs="+", help="local audio files to process")
    parser.add_argument("--server", required=True, help="service base URL, e.g. https://host:8000")
    parser.add_argument("--api-key", default=os.environ.get("DRAMA_API_KEY"), help="bearer token")
    parser.add_argument("--out-dir", default="./out", help="where to write subtitle files")
    parser.add_argument(
        "--formats",
        default="ja,zh,bilingual",
        help="comma-separated subset of: ja,zh,bilingual,json",
    )
    parser.add_argument("--poll-interval", type=float, default=3.0, help="seconds between polls")
    args = parser.parse_args(argv)
    args.formats = [f.strip() for f in args.formats.split(",") if f.strip()]

    failures = 0
    for path in args.inputs:
        if not os.path.isfile(path):
            print(f"skip: {path} is not a file", file=sys.stderr)
            failures += 1
            continue
        try:
            process_one(args, path)
        except Exception as exc:  # keep going with the remaining files
            print(f"error processing {path}: {exc}", file=sys.stderr)
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
