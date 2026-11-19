# 算力设施能耗与容量数据

按园区和计量周期提交的原始读数，经标准化后产出按月份与设施汇总的能耗、算力和 PUE 指标。
仅使用 Python 3.11+ 标准库。

> 仓库内的 `data/readings.json` 与测试数据均为合成样例，不代表任何现实园区，也不构成能耗结论。

## 快速开始

    python -m compileall src
    python -m unittest discover -s tests

    export PYTHONPATH=src
    # 只看标准化观测：单位归一、窗口、业务状态、是否可比
    python -m facility_metrics observe data/readings.json
    # 生成可归档的报送批次
    python -m facility_metrics report data/readings.json --batch 2026-01 --policy v1 --out out/2026-01.json
    # 查看某汇总由哪些观测构成、哪些被排除及原因
    python -m facility_metrics explain out/2026-01.json --month 2026-01 --park P01 --level park
    # 追踪单个读数的修订/校准链路
    python -m facility_metrics trace out/2026-01.json 'readings.json#0'
    # 比较两个批次的口径定义与结果变化
    python -m facility_metrics compare out/2026-01.json out/2026-02.json
    python -m facility_metrics policies

也可在代码中直接调用 `facility_metrics.load_readings` / `compute_metrics` /
`build_batch`，见 `src/facility_metrics/__init__.py`。

## 输入契约

每条读数是一个 JSON 对象；必填 `park`、`window_start`、`window_end`。除样例已有字段外，
还支持：

| 字段 | 含义 |
| --- | --- |
| `facility` | 设施标识；缺省表示园区级电表 |
| `unit` | 能耗单位：`Wh/kWh/MWh/GWh/TWh`；给出 `energy` 时必填，不做单位猜测 |
| `it_energy`, `it_energy_unit` | IT 能耗及单位，PUE 的分子/分母配对来源 |
| `capacity`, `capacity_unit` | 算力容量：`FLOPS/TFLOPS/PFLOPS/EFLOPS` 等 |
| `status` | `complete`（默认）/`partial`/`downtime` |
| `kind` | `reading`（默认）/`calibration` |
| `revision`, `submitted_at`, `supersedes` | 修订版本信息 |
| `reading_id`, `note` | 业务标识与备注 |

时间戳支持 `Z`、`+00:00` 或裸时间（视为 UTC）。窗口按**左闭右开**处理：
`...T23:59:59` 或 `...T23:59` 视为含末端的日终，自动归一到次日 `00:00`；已经是
`00:00` 的结束时间视为开区间右端不再后移。

## 处理规则

1. **单位归一**：能耗统一为 kWh，算力统一为 EFLOPS；原始单位保留在观测上。未知单位
   记为 `invalid_*_unit` 排除，不做猜测。
2. **可比窗口**：只有覆盖整个自然月（或达到口径阈值 `complete_coverage`）且不跨月的
   窗口才能进入**能耗比值/合计**。部分窗口的能耗单独记在 `partial_energy_kwh`，不会被
   当成零，也不与整月合计混加。
3. **算力是水平量**：按观测时长加权（可配置为简单平均），设施间相加得到园区；部分窗口
   的算力可用但标记 `capacity_status=partial`。
4. **PUE 只在同一批观测同时给出总能耗与 IT 能耗时计算**，否则 `pue=null` 并给出
   `pue_reason`（`missing_energy` / `missing_it_energy` / `incomplete_it_energy_coverage`）。
   绝不写 0。
5. **停机保留业务含义**：`status=downtime` 的读数默认排除出合计/比值，但仍出现在观测
   与 `excluded` 中；`downtime_excluded=False` 的口径可将其计入。
6. **校准替换**：`kind=calibration` 的读数替换同园区/设施/窗口的原读数，原读数标记
   `superseded` 与 `replaced_by`。两者不会相加，历史输入仍可追溯。
7. **层级不相加**：园区级电表与设施级分表是二选一的事实来源。按口径选定一层用于汇总，
   另一层记录为 `park_level_overridden` 或 `park_meter_preferred` 排除项。
8. **拒绝不丢失**：结构不合法的记录进入 `rejections`，带原始载荷和原因。

## 有效版本与历史批次

- 同一 (园区, 设施, 窗口, 事实类型) 只取一个有效版本；提交时间晚者优先，其次取较大
  `revision`，再按文件顺序。校准优先于普通读数。停机通知属于独立事实，不会顶掉月度读数。
- `--cutoff` 复现历史批次：该时刻之后提交的校准被标记 `after_cutoff`，不进入本批次，
  因此已对外报送的结果不会因后来的补录而改变。
- 批次文件写入后不可覆盖（重写会报 `FileExistsError`），新结果使用新的 `--batch`。
  每个批次都记录了口径文档及其摘要、输入文件的 SHA-256、观测、指标、拒绝与排除。

## 口径版本

`policies` 子命令列出内置口径。口径里任何影响含义的字段（可比阈值、算力加权方式、
层级优先、停机处理等）都会写进批次；`compare` 会同时给出**定义变更**和**数值变动**，
并指出输入文件摘要是否变化。现有内置口径：

- `v1`：整月才计入能耗；算力按时长加权；设施级优先。
- `v1-park`：同上但优先使用园区级电表。
- `v2`：窗口覆盖 ≥95% 即可比；算力简单平均且要求窗口可比。

## 输出结构

- `observations[]`：标准化读数、原始单位、有效标记、替换链、排除原因。
- `metrics[]`：每个 (month, park, facility, level) 一行，含
  `energy_*`、`it_energy_*`、`pue_*`、`capacity_*`、`energy_per_eflops_kwh`、
  `partial_energy_*`、`capacity_missing_facilities` 与 `excluded[]`；
  每个数值都带 `*_observations` 指回构成它的观测。
- `rejections[]`：无法解析的原始记录、原因与原始载荷。

## 目录

    src/facility_metrics/
      models.py    读数、观测、口径的数据类型
      units.py     单位换算
      windows.py   时间窗解析与自然月切分
      loader.py    原始读数 -> 标准化观测、版本选择、拒绝记录
      metrics.py   月度/设施/园区指标
      batch.py     可归档批次与批次比较
      audit.py     汇总构成、排除原因、修订链路、口径差异
      cli.py       命令行入口
