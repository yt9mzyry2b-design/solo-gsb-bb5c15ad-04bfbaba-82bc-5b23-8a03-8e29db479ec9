# 公交刷卡票价核验台（FastAPI + SQLite + 原生前端）

录入站点分区、票价矩阵、换乘抵扣额、每日封顶额及刷卡记录，系统按卡号配对进出站、
按运营日与换乘规则计算票价，并在数据异常时给出**可定位**的错误且**不生成部分账单**。

- 后端：FastAPI（Python 3.11）
- 数据库：SQLite（标准库 `sqlite3`，金额以**整数分**存储）
- 前端：原生 HTML / CSS / JavaScript，无框架、无构建步骤
- 部署：Dockerfile + docker-compose，数据持久化到命名卷

## 快速开始

```bash
docker compose up -d --build
# 启动后访问
open http://localhost:8000
```

停止与清理：

```bash
docker compose down          # 停止（保留数据卷）
docker compose down -v       # 停止并删除数据
```

本地裸机运行（不使用 Docker）：

```bash
pip install -r requirements.txt
DB_PATH=./bus.db uvicorn app.main:app --host 0.0.0.0 --port 8000
# http://localhost:8000 ，交互式 API 文档在 /docs
```

## 业务规则

1. **进出站配对**：刷卡记录按卡号分组、时间升序排列，必须从进站开始且进出成对。
   - 一条进站没有后续出站 → `MISSING_EXIT`（缺失出站）
   - 起始是出站，或出站前没有进站 → `MISSING_ENTRY`（缺失进站）
2. **运营日归属**：行程按**进站时刻**归属运营日，默认以 **04:00** 为分界。
   例如 `2026-09-19 03:20` 的进站属于 `2026-09-18` 运营日。分界时刻可在页面配置。
3. **换乘抵扣**：同一卡号「前次出站 → 下次进站」间隔 **≤ 90 分钟**（可配置）时，
   先抵扣换乘优惠额（默认 1 元，跨运营日的 90 分钟内换乘同样适用）。
   - 抵扣额不超过该程基础票价，最小为 0。
4. **当日封顶**：抵扣之后再按「卡号 + 运营日」应用封顶（默认 10 元/日）。
   - 实扣 = max(0, min(基础票价 − 换乘抵扣, 封顶额 − 当日已累计))
   - 累计额按实扣逐程累加；封顶后后续行程实扣 0。
5. **原子核验**：只要存在任何记录级错误（缺失出站/进站、时间逆序、重复刷卡、
   未知站点、票价矩阵缺项），整批核验失败，**不写入任何账单**（无部分账单）。
   旧账单同时清空，避免过期数据被误读。
6. **重复刷卡 / 时间逆序**：
   - 同卡两条记录时刻完全相同 → `DUPLICATE_SWIPE`（重复刷卡）
   - 同卡出站早于进站、或排序后存在逆序 → `TIME_REVERSED`（时间逆序）
   - 错误信息同时给出卡号、时刻、站点与**原始文本行号**。

账单字段：卡号、卡内序号、运营日、进站/出站时刻、进出站点与分区、
**基础票价、优惠额、优惠来源说明、实扣额、当日累计额（/封顶额）**。

## 录入格式

页面上每个区块都是「每行一条」的纯文本，分隔符支持逗号、制表符、分号、空白与全角逗号；
空行与 `#` 开头的注释行会被忽略。

| 区块 | 每行格式 | 示例 |
|---|---|---|
| 站点分区 | `站点名, 分区号` | `科技园, 1` |
| 票价矩阵 | `起始分区, 到达分区, 票价` | `1, 2, 3.00`（按方向精确匹配） |
| 刷卡记录 | `卡号, 时刻, 站点, 进/出` | `A001, 2026-09-18 08:00:00, 科技园, 进` |

- 刷卡方向：`IN` / `进` / `进站` 或 `OUT` / `出` / `出站`。
- 时刻格式：`YYYY-MM-DD HH:MM[:SS]`、`YYYY/MM/DD HH:MM`、`YYYY-MM-DDTHH:MM:SS`、
  或月日格式 `MM-DD HH:MM`（自动补当前年份）。
- 站点名与分区、矩阵项均不可重复定义；刷卡记录中的重复刷卡是业务校验，
  在「运行核验」时报告。

### 内置演示数据

点击页面右上角 **「载入演示数据」**，会写入 3 个站点、3×3 票价矩阵与两张卡的记录：

- 卡 A 在同一运营日内 4 次出行：演示换乘抵扣（间隔 ≤90 分钟）与当日封顶（累计到 10 元）；
- 卡 B 在 `03:20` 进站：演示 04:00 运营日归属。

可故意制造以下错误观察定位效果（核验后整批不出账单）：

```text
# 删除某卡最后一条出站        -> MISSING_EXIT
# 让同一卡两条记录时刻相同      -> DUPLICATE_SWIPE
# 让出站时刻早于进站时刻        -> TIME_REVERSED
# 录入未定义的站点名           -> UNKNOWN_STATION
# 删除矩阵中的某个分区对       -> MISSING_FARE
```

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/config` | 读取规则参数 |
| PUT | `/api/config` | 更新参数（使旧账单失效） |
| GET/PUT | `/api/stations` | 读取 / 整批替换站点分区，PUT body：`{"text": "..."}` |
| GET/PUT | `/api/fares` | 读取 / 整批替换票价矩阵 |
| GET/PUT | `/api/records` | 读取 / 整批替换刷卡记录 |
| POST | `/api/verify` | 执行核验；成功写入账单，失败返回 422 与错误数组 |
| GET | `/api/bills` | 读取最近一次成功核验的账单 |
| GET | `/api/health` | 健康检查 |

核验失败响应示例（HTTP 422）：

```json
{
  "ok": false,
  "bill_count": 0,
  "errors": [
    {
      "code": "MISSING_EXIT",
      "message": "卡号 A001 进站后缺失出站（2026-09-18 08:00:00 @ 科技园）",
      "location": { "line": 1, "card": "A001", "time": "2026-09-18 08:00:00", "station": "科技园" }
    }
  ]
}
```

错误码一览：`RECORD_BAD_FIELDS` / `RECORD_BAD_TIME` / `RECORD_BAD_ACTION` /
`STATION_BAD_FIELDS` / `STATION_DUPLICATE` / `FARE_BAD_FIELDS` / `FARE_DUPLICATE` /
`UNKNOWN_STATION` / `MISSING_FARE` / `DUPLICATE_SWIPE` / `TIME_REVERSED` /
`MISSING_ENTRY` / `MISSING_EXIT` / `CONFIG_BAD`。

## 目录结构

```text
.
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── README.md
└── app
    ├── __init__.py
    ├── main.py          # FastAPI 路由与静态页挂载
    ├── schemas.py       # 请求模型
    ├── core.py          # 解析 / 校验 / 配对 / 计价（纯标准库）
    ├── db.py            # SQLite 建表、整批替换、账单读写
    └── static
        ├── index.html
        ├── styles.css
        └── app.js
```

## 实现说明

- **金额精度**：业务层以元计算并保留两位小数；落库统一转整数分，避免浮点误差。
- **事务原子性**：整批替换基础数据、核验写账单均在单事务内完成；核验失败时清空
  `trip_bill`，因此任何时刻读到的账单要么是上一次完整成功的结果，要么为空。
- **数据位置**：容器内 `DB_PATH=/data/bus.db`，compose 用命名卷 `bus-data` 持久化；
  可用环境变量 `DB_PATH` 覆盖路径。
- **无外部前端依赖**：页面由 FastAPI 直接以静态文件提供，无需 npm/CDN，
  离线网络下也可使用。
