# cyprof

<p align="center">
  <strong>✈️ Linux 持续 CPU Profiling 守护进程 —— 生产环境的「飞行记录仪」</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.10%2B-blue" alt="Python">
  <img src="https://img.shields.io/badge/platform-Linux%20x86__64-orange" alt="Platform">
  <img src="https://img.shields.io/badge/coverage-95%25-brightgreen" alt="Coverage">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
</p>

---

## 项目背景

在生产环境中，CPU 性能问题往往具有**偶发性**和**不可复现性**——

- 凌晨 3 点 CPU 突然飙到 100%，运维被告警叫醒，登上机器时问题已经消失
- 某个请求偶尔延迟抖动 10 倍，压测却一切正常
- 怀疑是某次发版引入的性能退化，但距离上次火焰图分析已经过去两周，没有历史对比基线

传统的 CPU 诊断方式（`perf top`、`perf record` + 手动跑脚本）是**反应式**的：出问题 → 人操作 → 收集数据。但很多时候，**问题发生时人不在场，人在场时问题不复现**。

cyprof 的设计灵感来源于**飞机黑匣子**：7×24 不间断记录，低开销，自动轮转，**出事后回放事发时段的 CPU 栈数据**，直接生成火焰图。不需要提前知道什么时候会出问题，数据已经在那里了。

---

## 系统架构

```
┌──────────────────────────────────────────────────────────────────┐
│                          systemd                                 │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │                    cyprofiled (daemon)                      │  │
│  │                                                             │  │
│  │   ┌───────────┐    ┌───────────┐    ┌───────────┐          │  │
│  │   │ watermark │───▶│  rotate   │───▶│  collect  │          │  │
│  │   │  check    │    │ (before)  │    │perf+zstd  │          │  │
│  │   └───────────┘    └───────────┘    └─────┬─────┘          │  │
│  │       ▲                                    │                │  │
│  │       │              ┌───────────┐         │                │  │
│  │       └──────────────│  rotate   │◀────────┘                │  │
│  │       (halt/backoff) │ (after)   │                           │  │
│  │                      └───────────┘                           │  │
│  │                           │                                  │  │
│  │                           ▼                                  │  │
│  │                      ┌───────────┐                           │  │
│  │                      │  index    │                           │  │
│  │                      │ (SQLite)  │                           │  │
│  │                      └───────────┘                           │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                   │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │                   storage layer                             │  │
│  │                                                             │  │
│  │   /var/lib/cyprof/                                          │  │
│  │   ├── data/                    ◀── 2GB loopback ext4        │  │
│  │   │   ├── 20260617_143000_11hz.perf.data.zst   (250 KB)    │  │
│  │   │   ├── 20260617_143100_11hz.perf.data.zst   (250 KB)    │  │
│  │   │   ├── 20260617_143200_99hz.perf.data.zst   (2.2 MB)    │  │
│  │   │   └── ...                                              │  │
│  │   ├── metadata.db              ◀── SQLite WAL mode          │  │
│  │   └── health.json              ◀── daemon heartbeat         │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                   │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │                   cyprof (CLI)                              │  │
│  │                                                             │  │
│  │   query    list    info                                     │  │
│  │   ──time   ──from ──to                                      │  │
│  │     │          │                                             │  │
│  │     ▼          ▼                                             │  │
│  │  ┌──────────────────────────────┐                            │  │
│  │  │       FlameGraph pipeline    │                            │  │
│  │  │  perf script                 │                            │  │
│  │  │    → stackcollapse-perf.pl   │                            │  │
│  │  │      → flamegraph.pl         │                            │  │
│  │  │        → flamegraph.svg      │                            │  │
│  │  └──────────────────────────────┘                            │  │
│  └────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────┘
```

---

## 设计思路

### 核心原则

1. **零侵入** — perf 是内核子系统，无 LD_PRELOAD，无 JVM agent，无代码注入
2. **持续运行** — 不是按需启动，而是 7×24 后台采集，永远有历史
3. **低开销** — 常态 11Hz 采样 + 10 秒窗口 + 60 秒间隔，CPU 开销 < 0.05%
4. **安全第一** — 独立分区兜底，多层磁盘防护，systemd cgroup 资源限制
5. **简单可靠** — Python + SQLite + perf，零外部服务依赖

### 数据流

```
perf record (-a, -F 11, -g dwarf, sleep 10)
    │
    │  stdout pipe
    ▼
zstd -3 -T1
    │
    │  atomic rename (.tmp → .zst)
    ▼
/var/lib/cyprof/data/20260617_143000_11hz.perf.data.zst
    │
    │  INSERT metadata
    ▼
/var/lib/cyprof/metadata.db   ←── SQLite WAL, synchronous=NORMAL
    │
    │  ring-buffer rotate
    ▼
delete oldest files when: total > 500MB OR age > 24h
```

---

## 为什么选择 perf？

在 Linux 上做 CPU profiling，有几种主流方案：

| 方案 | 原理 | 开销 | 栈回溯 | 生产就绪 |
|------|------|:--:|:--:|:--:|
| **perf_events** | PMU 硬件中断 + 内核栈回溯 | ~0.05% | DWARF / LBR / FP | ✅ |
| eBPF (BCC/pyperf) | BPF 程序挂载到 PMU | ~0.01% | 需 BTF 支持 | ⚠️ 需 5.x 内核 |
| gperftools | LD_PRELOAD + SIGPROF | ~1-5% | libunwind | ⚠️ 需注入 |
| async-profiler | perf_events + JVM | ~0.5% | JVM frames | ✅ 仅 Java |

**选择 perf 的理由：**

- **内核原生** — `CONFIG_PERF_EVENTS` 从 Linux 2.6.31 起就存在，任何现代发行版都内置
- **无需运行时注入** — 不会像 gperftools 那样修改 LD_PRELOAD，不会像 async-profiler 那样需要 JVM 配合
- **多语言透明** — C/C++/Rust/Go 栈自动解析，Java/Node.js/Python 可通过 perf-map-agent 辅助
- **工业验证** — Netflix、Facebook、Google 的生产火焰图工具链均基于 perf
- **eBPF 是未来** — BCC/pyperf 有更低的理论开销，但依赖 Linux 5.x + BTF，RHEL 7 / CentOS 7 等存量系统无法运行。cyprof 的 MVP 先用 perf 覆盖最大范围的系统，后续版本增加 eBPF 后端作为可选升级

---

## 为什么使用持续采样？

### 按需采集 vs 持续采集

| 模式 | 覆盖窗口 | 问题响应 | 数据积累 |
|------|:--:|:--:|:--:|
| 按需采集（出问题 → `perf record`） | 手工触发的这一刻 | 人必须在线 | 无历史 |
| 持续采集（cyprof） | 7×24 全时段 | 事后查询即可 | 每天 360 MB，自动轮转 |

**典型场景对比：**

```
凌晨 3:07 CPU 飙升告警
──────────────────────────────────────────────────────────────

按需采集:
  03:07 告警 → 03:08 被叫醒 → 03:10 登录机器
  → 问题已经结束，perf record 只能看到正常状态
  → 死无对证

持续采集:
  03:15 被叫醒
  → cyprof query --time "03:05" --output /tmp/
  → 火焰图显示某个 GC 线程占了 80% CPU
  → 证据确凿，转发给 JVM 团队
```

### 采样频率分档

cyprof 支持运行时切换频率，平衡开销与精度：

```
常态 (7×24):     11Hz  ─── CPU 开销 ~0.005% / 核
告警自动触发:    99Hz  ─── 精确度提升 9 倍，持续 120 秒后回落
人工主动诊断:    199Hz ─── 极致细节，手动开启/关闭
```

**为什么常态用 11Hz 而不是 99Hz？** 在 64 核高 QPS 机器上，99Hz = 每秒 6336 次 NMI 中断，对延迟敏感的关键路径可能增加 0.1-0.5% 的 tail latency。11Hz 将中断密度降低 9 倍，几乎不可测量。

---

## 存储策略

### 文件格式

```
/var/lib/cyprof/data/
└── YYYYMMDD_HHMMSS_{freq}hz.perf.data.zst

示例:
  20260617_143000_11hz.perf.data.zst   ─── 2026年6月17日 14:30:00, 11Hz采样
  20260617_143200_99hz.perf.data.zst   ─── 同时可辨认为告警升级窗口
```

- **格式**: `perf record -o -` 原始输出 → `zstd -3` 压缩后落盘
- **压缩比**: perf.data 栈重复度高，实测 **5:1 ~ 8:1** 压缩比
- **管道模式**: `perf | zstd > file`，避免先写原始文件再压缩的双倍磁盘 IO

### SQLite 索引

```sql
CREATE TABLE samples (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ts        REAL    NOT NULL,   -- epoch seconds, UTC
    end_ts          REAL    NOT NULL,
    frequency_hz    INTEGER NOT NULL,   -- 11 / 49 / 99 / 199
    duration_sec    INTEGER NOT NULL,   -- 通常是 10
    file_path       TEXT    NOT NULL,    -- 相对路径
    file_size_bytes INTEGER NOT NULL,   -- 压缩后大小
    sample_count    INTEGER NOT NULL,    -- 解析自 perf stderr
    trigger_mode    TEXT    NOT NULL DEFAULT 'normal'  -- normal/alert/manual
);
CREATE INDEX idx_samples_time ON samples(start_ts, end_ts);
```

**为什么不在 SQLite 里存 perf 数据本体？**

- `perf script` 直接读文件，BLOB 存储意味着每次查询都要先 `SELECT BLOB → write tmp → perf script`，额外 IO
- SQLite WAL 在写入大 BLOB 时会膨胀
- `du -sb` 获取目录总大小 O(1)（文件系统缓存），比 `SELECT SUM(file_size_bytes)` 更快

### 轮转策略（双重限制 FIFO）

```
触发条件 (任一满足):
  ├── 总大小 > 500 MB  ─── 删最旧文件直到低于阈值
  └── 最旧文件距今 > 24 h ─── 过期淘汰

删除顺序: 先删磁盘文件 → 再删 SQLite 行
           (即使 SQLite 删除失败，磁盘空间已释放)
```

### 磁盘防爆（五层纵深防御）

```
第1层 — 应用自控:     每次采集后检查，500MB 硬上限 + 24h 时间限制
第2层 — 磁盘水位感知:
    free < 20% → WARN   日志告警
    free < 15% → CAP    强制 MAX_SIZE 降到 200MB
    free < 10% → EMERG  仅保留最近 30 分钟，停止采集
    free < 5%  → FATAL  进程主动 exit，systemd 不再拉起
第3层 — 独立分区:     2GB loopback ext4, noatime，绝对上限隔离
第4层 — systemd cgroup: IOWriteBandwidthMax=5M, CPUQuota=50%, MemoryMax=128M
第5层 — 监控告警:      health.json 文件探针 + structured JSON logging
```

---

## 查询流程

### CLI 命令

```bash
# 精确时间点查询 — 找到最接近的采样窗口
cyprof query --time "2026-06-17 14:30"

# 时间范围查询 — 合并多个窗口生成火焰图
cyprof query --from "2026-06-17 03:00" --to "03:15"

# 时间简写 — 自动补全今天日期
cyprof query --time "14:30" --output ./flamegraphs/

# 列出最近的采样记录
cyprof list

# 守护进程状态概览
cyprof info
```

### 查询引擎逻辑

```
用户输入: "14:30"
    │
    ▼
parse_time("14:30") → 今天 UTC 14:30:00
    │
    ▼
MetadataStore.query(start_ts=14:25, end_ts=14:35)
    │  (±5 min 滑动窗口)
    ▼
找到 1 条记录
    │
    ▼
返回最近的一条: 14:30:05, 11Hz, 42 samples, 250 KB
    │
    ▼
FlameGraphGenerator.generate([file], output_dir)
    │
    ▼
flamegraph.svg  ←── 标题带精确时间 & 采样参数
```

---

## 火焰图生成流程

```
┌──────────────┐
│  perf.data   │  (可能有 .zst 后缀)
│  .zst        │
└──────┬───────┘
       │
       ▼  stage 1: perf script
┌──────────────────────────────────────────┐
│ zstd -d -c file.zst | perf script -i -   │
│                                          │
│ 输出: 每行一条栈帧                         │
│ perf 12345 [000] 123456.789: cpu-clock:  │
│     ffffffff81000000 native_write_msr    │
│     ffffffff81000200 do_syscall_64       │
│     00007f0000000100 libc_start          │
└──────────────┬───────────────────────────┘
               │
               ▼  stage 2: stackcollapse-perf.pl
┌──────────────────────────────────────────┐
│ 折叠栈 — 每行: "栈;栈;栈 采样次数"          │
│                                          │
│ perf;native_write_msr;do_syscall_64 42   │
│ bash;main;printf;_IO_printf 15           │
└──────────────┬───────────────────────────┘
               │
               ▼  stage 3: flamegraph.pl
┌──────────────────────────────────────────┐
│              ████████                    │
│   ████████   ████████  ████             │
│   ████████   ████████  ████  ████       │
│   ████████   ████████  ████  ████  ████ │
│   ████████   ████████  ████  ████  ████ │
└──────────────────────────────────────────┘
              flamegraph.svg
          (可交互缩放/搜索的 SVG)
```

**关键特性：**

- **多文件合并**: 时间范围查询时，多个 `perf.data` 的 `perf script` 输出被拼接后再折叠，火焰图展示整个时间区间的聚合视图
- **透明解压**: `.zst` 文件通过 `zstd -d -c | perf script -i -` 管道直接读取，无需手工解压
- **标题自动标注**: 单点查询 → 精确时间和样本数；范围查询 → 起止时间

---

## 测试结果

```
143 passed in 6.61s

模块级覆盖率:
  collector.py    100%  ████████████████████████████████
  config.py        99%  ███████████████████████████████▌
  query.py         99%  ███████████████████████████████▌
  flamegraph.py    98%  ███████████████████████████████
  storage.py       97%  ██████████████████████████████▌
  cli.py           89%  ████████████████████████████▌
  daemon.py        88%  ████████████████████████████▏

总体覆盖率: 95%
```

| 测试文件 | 数量 | 覆盖范围 |
|----------|:--:|------|
| `test_config.py` | 10 | YAML 加载、环境变量覆盖、布尔/路径类型转换、4 种校验拒绝 |
| `test_collector.py` | 25 | 命令构建、管道模式、无压缩直写、zstd 失败、超时、空输出、异常清理 |
| `test_flamegraph.py` | 22 | 单/多文件生成、zst 解压、各级失败传播、样本计数估算、标题转义 |
| `test_storage.py` | 26 | CRUD 全量、时间范围查询、触发器统计、双限制轮转、紧急轮转、磁盘水位、删除容错 |
| `test_query.py` | 22 | 精确/范围查询、最近匹配、时间解析 6 种格式、火焰图错误处理、空结果 |
| `test_daemon.py` | 23 | tick 周期、磁盘水位阻塞/退避、信号处理、健康检查写入、可中断睡眠 |
| `test_cli.py` | 14 | query/list/info 子命令、参数校验、火焰图输出断言 |

未覆盖行均为合理豁免（systemd socket 通信、argparse SystemExit 路径、OSError fallback）。

---

## 生产部署

### 系统要求

- Linux x86_64，内核 ≥ 3.10
- `perf` (linux-tools 包)
- `zstd` (通常已预装)
- `perl` (FlameGraph 脚本依赖)
- Python 3.10+

### 安装

```bash
# 安装 Python 包
pip install .

# 部署配置文件
sudo mkdir -p /etc/cyprof
sudo cp deploy/cyprof.yaml /etc/cyprof/

# 部署 systemd 单元
sudo cp deploy/cyprofiled.service /etc/systemd/system/
sudo systemctl daemon-reload

# (推荐) 创建独立分区 — 2GB loopback, 彻底隔离
sudo dd if=/dev/zero of=/var/lib/cyprof.img bs=1M count=2048
sudo mkfs.ext4 -F /var/lib/cyprof.img
sudo mkdir -p /var/lib/cyprof/data
sudo mount -o loop,noatime /var/lib/cyprof.img /var/lib/cyprof/data
echo '/var/lib/cyprof.img /var/lib/cyprof/data ext4 loop,noatime 0 0' | sudo tee -a /etc/fstab

# 启动
sudo systemctl enable --now cyprofiled
systemctl status cyprofiled
```

### 配置

```yaml
# /etc/cyprof/cyprof.yaml
collector:
  frequency_hz: 11          # 常态采样频率 (告警时可动态升至 99)
  duration_sec: 10           # 每次窗口时长
  callgraph: true            # DWARF 栈回溯 (FP fallback)
  perf_path: perf            # perf 二进制路径
  extra_args: []             # 额外参数, 如 ["--pid", "1234"]

storage:
  data_dir: /var/lib/cyprof/data
  db_path: /var/lib/cyprof/metadata.db
  max_size_mb: 500           # 存储硬上限
  max_age_hours: 24           # 最旧保留时限
  comp_level: 3               # zstd 压缩级别 (1 最快, 19 最小)

daemon:
  sample_interval_sec: 60     # 采集间隔 (60s 即 duty cycle = 1/6)
  health_check_sec: 30        # 健康检查心跳间隔
```

支持 12 个环境变量覆盖：`CYPROF_FREQUENCY_HZ`、`CYPROF_MAX_SIZE_MB`、`CYPROF_DATA_DIR` 等。

### 日常运维

```bash
# 查看守护进程状态
cyprof info

# 查询昨天的某个时间点
cyprof query --time "2026-06-16 22:10"

# 查看最近的采样记录
cyprof list --limit 20

# 批量排查告警时段
for hour in 02 03 04; do
  cyprof query --from "2026-06-17 ${hour}:00" \
               --to   "2026-06-17 ${hour}:05" \
               --output "./flamegraphs/crash_${hour}"
done
```

### 火焰图阅读指南

```
    宽条 = 占用 CPU 时间长 → 优先优化
    高条 = 调用栈深 → 关注调用链
    颜色 = 区分函数 (暖色=用户态, 冷色=内核态)

    点击条 → 放大子节点
    搜索框 → 高亮某个函数的所有出现位置
```

---

## 后续优化

| 方向 | 方案 | 预期收益 |
|------|------|----------|
| **eBPF 后端** | BCC/pyperf 实现，CPU 开销降至 0.01% | 大规模部署更放心 |
| **HTTP API** | FastAPI REST 服务，在线浏览火焰图 | 免 SSH，Web 直查 |
| **多进程过滤** | `--pid` / `--cgroup` 精确采集 | 降低无关数据噪音 |
| **自适应频率** | 检测 CPU 使用率变化，异常时自动升频 | 平衡开销与精度 |
| **JIT 符号解析** | Java/Node.js/Python perf-map 注入 | 看到 JIT 编译的函数名 |
| **远程存储** | 可选 S3/MinIO 后端，数据异步上传 | 多机汇聚分析 |
| **离线符号化** | 采集时只存地址，查询时按二进制离线解析 | 采集开销进一步降低 |
| **Prometheus 导出** | CPU 热点 Top-N 作为指标暴露 | 融入监控告警体系 |
| **多机汇聚** | Agent-Collector 架构，中心化存储分析 | 集群级性能诊断 |

---

## License

MIT

---

<p align="center">
  <em>Built with ❤️ for SREs who hate being paged at 3 AM.</em>
</p>
