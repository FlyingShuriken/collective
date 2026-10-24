"""Measure the pinned local CPU BGE-M3 quantization on declared synthetic matching texts."""

import argparse
import asyncio
import hashlib
import json
import platform
import resource
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from collective.adapters.embeddings import (
    MODEL,
    REVISION,
    WEIGHTS_SHA256,
    EmbeddingGateway,
    normalized_text,
    shortlist,
)
from collective.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache/embeddings"
RUNTIME_SHA256 = "a02627e81f30e6eb9f72424c5556caf08fb0d018d593698fd0b6b18e1950b4cf"


async def benchmark(*, write_vectors=False):
    binary = CACHE / "llama-b11852/llama-b11852/llama-server"
    weights = CACHE / "bge-m3-Q4_K_M.gguf"
    for path, expected in (
        (weights, WEIGHTS_SHA256),
        (CACHE / "llama-b11852-bin-macos-arm64.tar.gz", RUNTIME_SHA256),
    ):
        with path.open("rb") as handle:
            if hashlib.file_digest(handle, "sha256").hexdigest() != expected:
                raise ValueError("The pinned model/runtime checksum differs")
    cases = json.loads((ROOT / "fixtures/matching/cases.json").read_text())
    assert cases["provenance"] == "synthetic"
    texts = tuple(
        dict.fromkeys(
            normalized_text(row["text"])
            for row in cases["catalogue"] + cases["queries"]
        )
    )
    command = [
        str(binary),
        "--model",
        str(weights),
        "--embeddings",
        "--pooling",
        "cls",
        "--embd-normalize",
        "2",
        "--gpu-layers",
        "0",
        "--threads",
        "4",
        "--threads-batch",
        "4",
        "--parallel",
        "1",
        "--ctx-size",
        "4096",
        "--batch-size",
        "4096",
        "--ubatch-size",
        "4096",
        "--host",
        "127.0.0.1",
        "--port",
        "8086",
        "--alias",
        f"{MODEL}@{REVISION}",
        "--no-webui",
    ]
    settings = Settings(
        _env_file=None,
        embedding_mode="local",
        embedding_url="http://127.0.0.1:8086/v1/embeddings",
    )
    gateway = EmbeddingGateway(settings)
    vectors, samples = {}, []
    started = time.perf_counter()
    with (CACHE / "benchmark-runtime.log").open("w") as output:
        process = await asyncio.create_subprocess_exec(
            *command, stdout=output, stderr=output
        )
        try:
            async with httpx.AsyncClient(timeout=1) as client:
                while True:
                    if process.returncode is not None:
                        raise RuntimeError(
                            "Embedding runtime exited; inspect its local log"
                        )
                    try:
                        response = await client.get("http://127.0.0.1:8086/health")
                        if response.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    if time.perf_counter() - started > 45:
                        raise TimeoutError(
                            "Embedding runtime was not ready within 45 seconds"
                        )
                    await asyncio.sleep(0.1)
            startup = time.perf_counter() - started
            for text in texts:
                began = time.perf_counter()
                batch = await gateway.encode([text], REVISION)
                duration = time.perf_counter() - began
                vectors[text] = batch.vectors[0]
                samples.append(
                    {
                        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                        "utf8_bytes": len(text.encode()),
                        "seconds": duration,
                    }
                )
            warm = []
            for _ in range(5):
                began = time.perf_counter()
                await gateway.encode([texts[0]], REVISION)
                warm.append(time.perf_counter() - began)
        finally:
            if process.returncode is None:
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except TimeoutError:
                process.kill()
                await asyncio.wait_for(process.wait(), timeout=5)
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    ranks = []
    catalogue = [
        (row["key"], vectors[normalized_text(row["text"])])
        for row in cases["catalogue"]
    ]
    for query in cases["queries"]:
        ranked = shortlist(vectors[normalized_text(query["text"])], catalogue)
        ranks.append(
            {
                "query": query["key"],
                "expected": query["expected"],
                "ranked": [{"key": key, "cosine": score} for key, score in ranked],
            }
        )
    report = {
        "measured_at": datetime.now(UTC).isoformat(),
        "provenance": "local_cpu_inference",
        "system": platform.system(),
        "machine": platform.machine(),
        "os_release": platform.release(),
        "model_id": MODEL,
        "model_revision": REVISION,
        "weights_sha256": WEIGHTS_SHA256,
        "runtime": "llama.cpp b11852 (7894e8b92)",
        "runtime_archive_sha256": RUNTIME_SHA256,
        "pooling": "cls",
        "normalization": "l2",
        "cpu_threads": 4,
        "startup_seconds": startup,
        "filesystem_cache": "unspecified; checksum read precedes startup",
        "peak_child_rss_bytes": usage.ru_maxrss
        if sys.platform == "darwin"
        else usage.ru_maxrss * 1024,
        "child_user_seconds": usage.ru_utime,
        "child_system_seconds": usage.ru_stime,
        "samples": samples,
        "warm_repeat_seconds": warm,
        "retrieval": ranks,
        "limitations": [
            "Quantized artifact, not full-precision BAAI weights.",
            "Small declared synthetic catalogue; not a general matching quality claim.",
            "Local CPU measurements do not establish hosted memory/latency.",
            "Similarity cannot confirm attributes, packs or a mapping.",
        ],
    }
    report_path = ROOT / "docs/validation/bge-m3-local.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    if write_vectors:
        target = ROOT / "fixtures/matching/vectors.json"
        raw = (
            json.dumps(vectors, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode()
        target.write_bytes(raw)
        manifest = {
            "version": "1",
            "provenance": "recorded_synthetic_local_quantized_inference",
            "source_model": "BAAI/bge-m3",
            "model_id": MODEL,
            "model_revision": REVISION,
            "weights_sha256": WEIGHTS_SHA256,
            "pooling": "cls",
            "dimensions": 1024,
            "vectors_path": target.name,
            "vectors_sha256": hashlib.sha256(raw).hexdigest(),
        }
        (target.parent / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n"
        )
    return {
        "report": str(report_path),
        "startup_seconds": startup,
        "peak_rss_bytes": report["peak_child_rss_bytes"],
        "texts": len(texts),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-vectors", action="store_true")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(benchmark(write_vectors=args.write_vectors))))
