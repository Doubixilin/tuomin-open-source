#!/usr/bin/env python3
"""Measure CLUENER NER throughput and device behavior on a synthetic fixture.

The NER tier costs per request are deployment-varying: the same text is ~10x
slower on CPU than on a CUDA device, and an agentic loop re-detects the whole
conversation on every model call. This script records the numbers a deployment
decision needs — per-size seconds and characters/second, span counts so CPU and
CUDA can be compared on identical input, cold-start time, and peak VRAM.

``--thermal`` adds a three-phase sample run answering "how hot does this get",
which is a different question from throughput: an idle baseline, a realistic
agent cadence (one short inference, then ``--gap-seconds`` of idle before the
next), and a sustained back-to-back loop for the worst case. GPU
temperature/power/utilization come from ``nvidia-smi``; CPU load and
thermal-zone temperature come from ``typeperf`` on Windows and are absent
elsewhere.

The fixture is synthetic and generated here; no real material is read. Output
is safe JSON — counts, durations, and device telemetry, never input text.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Synthetic contract-shaped unit, repeated to build each measured text. Uses the
# same shape as the committed fixtures: invented names, a formatted contract id,
# amounts, an account, a contact, and a role cue NER can anchor on.
SYNTHETIC_UNIT = (
    "甲方北辰测试制造有限公司与乙方星橙合成建设有限公司于2024年3月15日签订"
    "《虚构星橙制造基地建设工程施工合同》，合同编号HT-2026-TM-0001，合同总价人民币"
    "12,345,678.90元，由张三担任项目经理，联系电话13800138000，开户行中国工商银行"
    "北京中关村支行，账号6222021234567890123。相关部门已完成初审。"
)

_GPU_QUERY = (
    "timestamp,temperature.gpu,power.draw,utilization.gpu,"
    "utilization.memory,memory.used,clocks.sm"
)


def _gpu_sample() -> dict | None:
    """Return one telemetry sample from nvidia-smi, or None when unavailable."""
    try:
        raw = subprocess.run(
            ["nvidia-smi", f"--query-gpu={_GPU_QUERY}", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if raw.returncode != 0 or not raw.stdout.strip():
        return None
    fields = [field.strip() for field in raw.stdout.strip().splitlines()[0].split(",")]
    if len(fields) != 7:
        return None

    def _number(value: str) -> float | None:
        try:
            return float(value)
        except ValueError:
            return None

    return {
        "gpu_temp_c": _number(fields[1]),
        "gpu_power_w": _number(fields[2]),
        "gpu_util_pct": _number(fields[3]),
        "gpu_mem_bw_pct": _number(fields[4]),
        "gpu_mem_used_mib": _number(fields[5]),
        "gpu_sm_clock_mhz": _number(fields[6]),
    }


def _start_cpu_sampler(destination: Path) -> subprocess.Popen | None:
    """Start a one-second typeperf capture of CPU load and thermal zones (Windows)."""
    if sys.platform != "win32":
        return None
    command = [
        "typeperf",
        r"\Processor(_Total)\% Processor Time",
        r"\Thermal Zone Information(*)\Temperature",
        "-si", "1",
        "-sc", "100000",
        "-f", "CSV",
        "-o", str(destination),
        "-y",
    ]
    try:
        return subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return None


def _to_float(value: str) -> float | None:
    """Parse one typeperf cell; the CSV uses blanks for unavailable counters."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _read_cpu_samples(path: Path) -> list[dict]:
    """Parse a typeperf CSV into per-second CPU load and hottest thermal zone."""
    if not path.exists():
        return []
    lines = [line for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
    if len(lines) < 2:
        return []
    header = [cell.strip().strip('"') for cell in lines[0].split(",")]
    load_index = next((i for i, name in enumerate(header) if name.endswith("% Processor Time")), None)
    # typeperf writes the full counter path, e.g.
    # \\HOST\Thermal Zone Information(\_TZ.THRM)\Temperature — match on the
    # counter name, never on the machine name.
    zone_indices = [i for i, name in enumerate(header) if "Thermal Zone Information" in name]
    samples: list[dict] = []
    for line in lines[1:]:
        cells = [cell.strip().strip('"') for cell in line.split(",")]
        if len(cells) < len(header):
            continue
        zone_temps = []
        for index in zone_indices:
            kelvin = _to_float(cells[index])
            # 0 and values below ambient are "sensor not present" sentinels.
            if kelvin is not None and kelvin > 273.15:
                zone_temps.append(kelvin - 273.15)
        samples.append({
            "cpu_load_pct": None if load_index is None else _to_float(cells[load_index]),
            "cpu_zone_c": max(zone_temps) if zone_temps else None,
        })
    return samples


def _run_throughput(detector, sizes: list[int], repeats: int) -> dict:
    """Time one detection per size and record the span count for cross-device comparison."""
    results = []
    for size in sizes:
        text = SYNTHETIC_UNIT * max(1, round(size / len(SYNTHETIC_UNIT)))
        best = None
        spans = None
        for _ in range(repeats):
            started = time.perf_counter()
            spans = detector.detect(text)
            elapsed = time.perf_counter() - started
            best = elapsed if best is None else min(best, elapsed)
        results.append({
            "chars": len(text),
            "spans": len(spans),
            "seconds": round(best, 3),
            "chars_per_s": round(len(text) / best),
        })
    return {"sizes": results}


def _run_thermal(detector, chars: int, gap_seconds: float, idle_seconds: float,
                 cadence_seconds: float, sustained_seconds: float) -> dict:
    """Sample GPU/CPU telemetry across idle, realistic-cadence, and sustained phases.

    Telemetry is read once per second in every phase, including the gaps between
    inferences — sampling only after each inference would alias every cadence
    reading to a burst peak and make a realistic cadence look as hot as a
    sustained load. The phases differ in whether the detector runs and how the
    loop is paced:

    - ``idle``      — no inference, one sample per second (the baseline)
    - ``cadence``   — one inference, then ``gap_seconds`` of idle before the next
    - ``sustained`` — back-to-back inference for the whole phase (worst case)

    CPU samples come from a separate ``typeperf`` process capturing one row per
    second; they are aligned to the gathered rows by position, which is exact
    only while both run at 1 Hz — read the CPU columns as approximate.
    """
    import torch

    text = SYNTHETIC_UNIT * max(1, round(chars / len(SYNTHETIC_UNIT)))
    phases = [
        ("idle", idle_seconds, 1.0, False),
        ("cadence", cadence_seconds, gap_seconds, True),
        ("sustained", sustained_seconds, 0.0, True),
    ]
    rows: list[dict] = []
    runs: dict[str, int] = {}
    peak: float | None = None
    with tempfile.TemporaryDirectory(prefix="tuomin_ner_thermal_") as tmp:
        cpu_csv = Path(tmp) / "cpu.csv"
        cpu_proc = _start_cpu_sampler(cpu_csv)
        try:
            if torch.cuda.is_available():
                torch.cuda.reset_peak_memory_stats()
            for name, duration, gap, work in phases:
                runs[name] = 0
                end = time.monotonic() + duration
                last_sample = 0.0
                last_inference = 0.0
                while time.monotonic() < end:
                    iteration = time.monotonic()
                    if work and (gap == 0.0 or iteration - last_inference >= gap):
                        detector.detect(text)
                        runs[name] += 1
                        last_inference = time.monotonic()
                    if iteration - last_sample >= 1.0:
                        sample = _gpu_sample() or {}
                        sample["phase"] = name
                        # Burst samples carry the peak; gap samples show recovery.
                        sample["after_inference"] = work and iteration - last_inference < 0.5
                        rows.append(sample)
                        last_sample = iteration
                    if gap > 0.0:
                        remaining = (iteration + 1.0) - time.monotonic()
                        if remaining > 0:
                            time.sleep(remaining)
            if torch.cuda.is_available():
                peak = round(torch.cuda.max_memory_allocated() / 1024 / 1024, 1)
        finally:
            if cpu_proc is not None:
                cpu_proc.terminate()
                try:
                    cpu_proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    cpu_proc.kill()
            cpu_samples = _read_cpu_samples(cpu_csv)

    for index, row in enumerate(rows):
        if index < len(cpu_samples):
            row.update(cpu_samples[index])

    summary = {}
    for name, duration, _, _ in phases:
        phase_rows = [row for row in rows if row.get("phase") == name]
        if not phase_rows:
            continue
        summary[name] = {
            "seconds": duration,
            "inferences": runs[name],
            "samples": len(phase_rows),
            "gpu_temp_c_max": _max_of(phase_rows, "gpu_temp_c"),
            "gpu_power_w_max": _max_of(phase_rows, "gpu_power_w"),
            "gpu_power_w_mean": _mean_of(phase_rows, "gpu_power_w"),
            "gpu_util_pct_max": _max_of(phase_rows, "gpu_util_pct"),
            "gpu_mem_bw_pct_max": _max_of(phase_rows, "gpu_mem_bw_pct"),
            "gpu_mem_used_mib_max": _max_of(phase_rows, "gpu_mem_used_mib"),
            "cpu_load_pct_max": _max_of(phase_rows, "cpu_load_pct"),
            "cpu_zone_c_max": _max_of(phase_rows, "cpu_zone_c"),
        }
        # A paced phase spends most of its samples between inferences; reporting
        # the gap separately is what separates "burst peak" from "does not cool
        # down between bursts".
        gap_rows = [row for row in phase_rows if row.get("after_inference") is False]
        if gap_rows:
            summary[name]["gap_samples"] = len(gap_rows)
            summary[name]["gap_gpu_temp_c_max"] = _max_of(gap_rows, "gpu_temp_c")
            summary[name]["gap_gpu_power_w_max"] = _max_of(gap_rows, "gpu_power_w")
    return {
        "phases": summary,
        "gap_seconds": gap_seconds,
        "thermal_chars": len(text),
        "peak_vram_mb": peak,
        "samples": rows,
    }


def _max_of(rows: list[dict], key: str) -> float | None:
    values = [row[key] for row in rows if isinstance(row.get(key), (int, float))]
    return round(max(values), 1) if values else None


def _mean_of(rows: list[dict], key: str) -> float | None:
    values = [row[key] for row in rows if isinstance(row.get(key), (int, float))]
    return round(sum(values) / len(values), 1) if values else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--device", default=None,
                        help="cpu, cuda, or a device index; sets TUOMIN_NER_DEVICE for the run")
    parser.add_argument("--sizes", default="2100,10500,29750,122500",
                        help="comma-separated text sizes in characters")
    parser.add_argument("--repeats", type=int, default=1, help="timed runs per size (best kept)")
    parser.add_argument("--thermal", action="store_true", help="also run the thermal phases")
    parser.add_argument("--skip-throughput", action="store_true",
                        help="run only the thermal phases (re-measuring a temperature curve)")
    parser.add_argument("--thermal-chars", type=int, default=29750)
    parser.add_argument("--idle-seconds", type=float, default=20.0)
    parser.add_argument("--cadence-seconds", type=float, default=150.0)
    parser.add_argument("--gap-seconds", type=float, default=10.0,
                        help="idle seconds between one inference and the next; "
                             "10 s approximates the gap inside an agent turn")
    parser.add_argument("--sustained-seconds", type=float, default=20.0)
    parser.add_argument("--out", default=None, help="write the JSON report here instead of stdout")
    args = parser.parse_args()

    if args.device:
        os.environ["TUOMIN_NER_DEVICE"] = args.device

    try:
        import torch
        from tuomin_gateway.detectors.ner import CluenerNerDetector
    except ImportError as exc:  # pragma: no cover - optional dependency
        print(json.dumps({"status": "error", "message": f"NER extra not installed: {exc}"},
                         ensure_ascii=False, indent=2))
        return 2

    if (args.device or "").lower() in ("cuda", "gpu", "auto") or (args.device or "").isdigit():
        if not torch.cuda.is_available():
            print(json.dumps({
                "status": "error",
                "message": "a CUDA device was requested but torch.cuda.is_available() is false; "
                           "install a CUDA build of torch (checks currently off)",
                "torch": torch.__version__,
            }, ensure_ascii=False, indent=2))
            return 2

    report: dict = {
        "status": "ok",
        "torch": torch.__version__,
        "cuda_available": torch.cuda.is_available(),
        "cuda_device": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
        "cuda_capability": list(torch.cuda.get_device_capability(0)) if torch.cuda.is_available() else None,
        "device_requested": args.device or os.environ.get("TUOMIN_NER_DEVICE", "(unset: cpu)"),
        "model_dir": os.environ.get("TUOMIN_NER_MODEL_DIR", "(unset: pinned HF id)"),
    }

    detector = CluenerNerDetector()
    started = time.perf_counter()
    detector.detect(SYNTHETIC_UNIT)
    report["cold_start_seconds"] = round(time.perf_counter() - started, 2)

    sizes = [int(value) for value in args.sizes.split(",") if value.strip()]
    if not args.skip_throughput:
        report["throughput"] = _run_throughput(detector, sizes, max(1, args.repeats))
    if args.thermal:
        report["thermal"] = _run_thermal(
            detector,
            chars=args.thermal_chars,
            gap_seconds=args.gap_seconds,
            idle_seconds=args.idle_seconds,
            cadence_seconds=args.cadence_seconds,
            sustained_seconds=args.sustained_seconds,
        )

    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
