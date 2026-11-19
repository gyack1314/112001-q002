"""Command line interface for the facility metering pipeline.

Examples::

    python -m facility_metrics observe data/readings.json
    python -m facility_metrics report data/readings.json --policy v1
    python -m facility_metrics report data/*.json --batch jan --out out/jan.json
    python -m facility_metrics explain out/jan.json --month 2026-01 --park P01
    python -m facility_metrics trace out/jan.json readings.json#2
    python -m facility_metrics compare out/jan.json out/feb.json

The CLI prints JSON to stdout so results can be diffed and archived.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .audit import explain_cell, policy_diff, trace_observation
from .batch import Batch, build_batch, compare_batches, read_batch
from .loader import load_readings
from .models import MetricPolicy
from .windows import parse_instant

POLICIES: dict[str, MetricPolicy] = {
    "v1": MetricPolicy(
        version="v1",
        capacity_requires_comparable_window=False,
        level_precedence="facility",
        description="初始口径：整月才能计入能耗；算力按小时加权；设施级优先；校准替换原读数",
    ),
    "v1-park": MetricPolicy(
        version="v1-park",
        capacity_requires_comparable_window=False,
        level_precedence="park",
        description="同一算法但优先使用园区级电表",
    ),
    "v2": MetricPolicy(
        version="v2",
        complete_coverage=0.95,
        capacity_weighting="simple",
        capacity_requires_comparable_window=True,
        level_precedence="facility",
        description="修订口径：窗口覆盖≥95% 即可比；算力简单平均且要求窗口可比",
    ),
}


def _dump(payload: object) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def _read_json(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise SystemExit(f"{path}: expected a JSON array of records")
    return payload


def _load_batch_or_error(path: Path) -> dict:
    try:
        return read_batch(path)
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


def cmd_observe(args: argparse.Namespace) -> int:
    policy = POLICIES[args.policy]
    observations = []
    rejections = []
    for path in args.files:
        result = load_readings(
            _read_json(path),
            policy,
            source_file=path.name,
            cutoff=parse_instant(args.cutoff) if args.cutoff else None,
        )
        observations.extend(result.observations)
        rejections.extend(item.as_dict() for item in result.rejections)
    _dump(
        {
            "policy_version": policy.version,
            "observations": [obs.as_dict() for obs in observations],
            "rejections": rejections,
        }
    )
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    policy = POLICIES[args.policy]
    payloads = {path.name: path.read_bytes() for path in args.files}
    cutoff = parse_instant(args.cutoff) if args.cutoff else None
    batch: Batch = build_batch(
        payloads,
        policy,
        batch_id=args.batch,
        cutoff=cutoff,
        created_at=parse_instant(args.created_at) if args.created_at else None,
        notes=args.notes,
    )
    if args.out:
        out = Path(args.out)
        batch.write(out)
        _dump(
            {
                "batch_id": batch.batch_id,
                "out": str(out),
                "policy_version": policy.version,
                "metrics": len(batch.metrics),
                "observations": len(batch.observations),
                "rejections": len(batch.rejections),
            }
        )
    else:
        _dump(batch.as_dict())
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    payload = _load_batch_or_error(Path(args.batch))
    _dump(
        explain_cell(
            payload,
            month=args.month,
            park=args.park,
            facility=args.facility,
            level=args.level,
        )
    )
    return 0


def cmd_trace(args: argparse.Namespace) -> int:
    payload = _load_batch_or_error(Path(args.batch))
    _dump(trace_observation(payload, args.observation_id))
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    old = _load_batch_or_error(Path(args.old))
    new = _load_batch_or_error(Path(args.new))
    if args.policy_only:
        _dump(policy_diff(old, new))
    else:
        _dump(compare_batches(old, new))
    return 0


def cmd_policies(args: argparse.Namespace) -> int:
    _dump({name: policy.as_dict() for name, policy in POLICIES.items()})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="facility_metrics",
        description="园区能耗、算力与 PUE 的标准化观测与月度指标",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    observe = sub.add_parser("observe", help="只做标准化，输出观测与拒绝记录")
    observe.add_argument("files", nargs="+", type=Path)
    observe.add_argument("--policy", choices=sorted(POLICIES), default="v1")
    observe.add_argument("--cutoff", help="ISO 时间；该时刻之后提交的读数不参与本批次")
    observe.set_defaults(func=cmd_observe)

    report = sub.add_parser("report", help="生成可归档的报送批次")
    report.add_argument("files", nargs="+", type=Path)
    report.add_argument("--policy", choices=sorted(POLICIES), default="v1")
    report.add_argument("--batch", required=True, help="批次标识")
    report.add_argument("--out", help="批次 JSON 输出路径；不写则打印到标准输出")
    report.add_argument("--cutoff", help="ISO 时间；该时刻之后提交的读数不参与本批次")
    report.add_argument("--created-at", help="ISO 时间；用于复现历史批次的生成时间")
    report.add_argument("--notes", help="批次说明")
    report.set_defaults(func=cmd_report)

    explain = sub.add_parser("explain", help="说明某个汇总由哪些观测构成、哪些被排除")
    explain.add_argument("batch", type=Path)
    explain.add_argument("--month", required=True)
    explain.add_argument("--park", required=True)
    explain.add_argument("--facility", default=None)
    explain.add_argument("--level", choices=("park", "facility"), default="facility")
    explain.set_defaults(func=cmd_explain)

    trace = sub.add_parser("trace", help="追踪一个观测的修订与校准链路")
    trace.add_argument("batch", type=Path)
    trace.add_argument("observation_id")
    trace.set_defaults(func=cmd_trace)

    compare = sub.add_parser("compare", help="比较两个报送批次的定义与结果")
    compare.add_argument("old", type=Path)
    compare.add_argument("new", type=Path)
    compare.add_argument("--policy-only", action="store_true", help="只比较口径定义")
    compare.set_defaults(func=cmd_compare)

    policies = sub.add_parser("policies", help="列出内置口径")
    policies.set_defaults(func=cmd_policies)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
