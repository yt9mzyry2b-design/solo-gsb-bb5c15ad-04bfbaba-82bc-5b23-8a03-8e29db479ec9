# 🚌 公交刷卡票价核验台

基于 **FastAPI + SQLite + 原生前端（HTML/CSS/JS，无需构建）** 的公交刷卡计费核验系统。
录入站点分区、票价矩阵、换乘抵扣额、每日封顶额与刷卡记录后，系统自动按卡号配对进出站、
判定换乘优惠与运营日封顶，输出每段行程的基础票价、优惠来源、实扣额与当日累计。

## 快速开始

### Docker Compose（推荐）

```bash
docker compose up -d --build
```

启动后浏览器访问： **http://localhost:8000**

数据保存在命名卷 `bus-data`（容器内 `/data/bus.db`）中，容器重建不丢失。

### 仅用 Docker

```bash
docker build -t bus-fare-check .
docker run -d -p 8000:8000 -v bus-data:/data bus-fare-check
```

### 本地直接运行

```bash
pip install -r requirements.txt
DB_PATH=./bus.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 使用步骤

界面分三个标签页，按顺序操作即可：

1. **① 参数配置**
   - 设置「换乘抵扣额」（元）与「每日封顶额」（元 / 卡 / 运营日）；
   - 维护站点分区：站点名称 → 分区号；
   - 维护票价矩阵：两个分区之间的票价（方向无关，A↔B 与 B↔A 同价，同分区也可录入）。
2. **② 刷卡记录**
   - 逐条录入（卡号、站点、时间、IN 进站 / OUT 出站），或批量粘贴：
     ```
     卡号,站点,时间,类型
     A001,中央公园,2026-09-18 08:00,IN
     A001,科技园,2026-09-18 08:35,OUT
     ```
     首行可为表头；时间格式 `YYYY-MM-DD HH:MM`（也兼容 `/` 与秒）；
     类型支持 `IN/进站`、`OUT/出站`。**任一行解析失败则整批拒绝、不写入。**
   - 可点击「载入演示数据」一键体验（会清空现有数据）。
3. **③ 核验结果**
   - 点击「运行核验 / 生成账单」。
   - 有错误时红色面板逐条列出问题，并提供「→记录#N」跳转定位（刷卡表中相应行高亮），
     **不出任何账单（无部分账单）**；
   - 核验通过后展示行程账单与「卡 / 运营日」汇总。

## 计费规则

| 规则 | 说明 |
| --- | --- |
| 进出站配对 | 同一卡号按**录入顺序（记录编号）**严格 IN、OUT 交替配对；不同卡号互不干扰 |
| 运营日 | 以**进站时刻**为准，每日 **04:00** 之前归属前一运营日（03:30 进站算前一天） |
| 基础票价 | 按进、出站站点所属分区查票价矩阵 |
| 换乘抵扣 | 同卡「前次出站 → 本次进站」间隔 **≤ 90 分钟**即享抵扣；先在基础票价上抵扣，抵扣额不超过基础票价 |
| 当日封顶 | 抵扣后按「卡 + 运营日」累计实扣，超过封顶额时本段只补差额；已封顶后再乘车实扣 0 元；新运营日重新累计 |
| 跨运营日换乘 | 间隔 ≤ 90 分钟仍可享换乘抵扣，但累计额按进站时刻所属运营日分别计算 |

## 错误校验（定位到记录编号，任一存在即不出账单）

| 错误码 | 含义 |
| --- | --- |
| `duplicate_tap` | 完全重复刷卡：同卡、同站、同时刻、同类型（进出站类型不同不算重复） |
| `time_reversed` | 时间逆序：同卡后录入的记录时刻早于前一条 |
| `missing_check_in` | 缺少进站：首刷即出站，或连续两次出站 |
| `missing_check_out` | 缺少出站：末刷为进站，或连续两次进站 |
| `unknown_station` | 刷卡站点未配置分区 |
| `missing_fare` | 票价矩阵缺少对应分区间的票价 |

## 金额处理

所有金额在后端以整数「**分**」存储和计算（SQLite 中为 `price_cents` 等字段），
API 与界面以元展示，避免浮点误差。

## 主要 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET/POST/DELETE | `/api/stations[/{id}]` | 站点分区 |
| GET/POST/DELETE | `/api/fares[/{id}]` | 票价矩阵（元） |
| GET/PUT | `/api/settings` | 换乘抵扣 / 每日封顶（元） |
| GET/POST/DELETE | `/api/swipes[/{id}]` | 刷卡记录 |
| POST | `/api/swipes/bulk` | 批量导入（整批事务性校验） |
| DELETE | `/api/swipes` | 清空刷卡记录 |
| POST | `/api/billing/run` | 运行核验，返回行程账单或错误定位 |
| POST | `/api/demo` | 载入演示数据 |

交互式文档：服务启动后访问 `http://localhost:8000/docs`。

## 项目结构

```
.
├── app/
│   ├── billing.py         # 纯计费/校验逻辑（不依赖 Web，可独立测试）
│   ├── database.py        # SQLite 数据访问层
│   ├── main.py            # FastAPI 路由与静态托管
│   └── static/index.html  # 原生单页前端
├── tests/                 # pytest 单元 + 端到端测试
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 运行测试

```bash
pip install -r requirements.txt pytest httpx
python -m pytest -q
```
