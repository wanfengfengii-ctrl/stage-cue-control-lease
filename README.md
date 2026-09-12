# 联排控制权交接台 · 升降台 × 飞行吊点

升降台控制席与飞行吊点控制席进入联排后，可能同时点击同一个危险动作；席位断网后
系统既不能永久锁死，也不能让它恢复后凭旧凭据再次执行。本项目是一个**真实可联调**
的控制权交接台：

- **React + TypeScript** 控制台：每个浏览器会话都能看到动作状态（持有席位、
  剩余秒数、执行结果），并可申请 / 续期 / 释放 / 执行；
- **FastAPI** 服务：授予不可猜测令牌，所有判定只按**服务端 UTC 时间**；
- **PostgreSQL**：单事务行锁保证同一动作同时只有一个有效租约。

无任何假接口：界面里的每一个按钮都走 HTTP → FastAPI → PostgreSQL。
（`frontend/test/mock-server.ts` 仅是 jsdom 组件测试的测试替身，不出现在运行栈中；
Playwright 双浏览器用例打真实服务。）

## 运行方法（Docker Compose）

前置：Docker Engine 24+ 与 Docker Compose v2。

```bash
# 构建并启动：db(PostgreSQL) + api(FastAPI) + web(nginx + React)
docker compose up -d --build

# 打开控制台
open http://localhost:8080        # macOS
# 或直接浏览 http://localhost:8080

# 直接访问 API（健康检查 / 动作快照）
curl http://localhost:8000/health
curl http://localhost:8000/api/actions
```

覆盖宿主端口（`WEB_PORT` 给浏览器界面，`API_PORT` 给直连 API）：

```bash
WEB_PORT=9090 API_PORT=9000 docker compose up -d --build
# 界面 http://localhost:9090 ，API http://localhost:9000
```

停止与清库：

```bash
docker compose down          # 保留数据卷
docker compose down -v       # 连同 PostgreSQL 数据一并删除
```

## 一次性验收服务 verify

`verify` 是一个**一次性**（run-once）服务，依次执行：

1. pytest：针对同一套 PostgreSQL 验证**数据库事务竞争**（24 路并发争抢仅 1 个
   赢家、独立连接池同样串行化）与**过期边界**（`now == expires_at` 即失效、
   旧令牌迟到的续期 / 释放 / 执行全部拒绝且新租约不变、执行只写一次事件）；
2. Playwright：两个真实浏览器上下文模拟双控制席——同时争抢只有一人持有、
   失联满 30 秒后另一席立即接管、旧页面再次点击明确显示「控制权已失效」。

```bash
docker compose --profile verify run --build --rm verify
```

退出码 0 即验收通过（结尾打印 `ALL ACCEPTANCE CHECKS PASSED`）。

## 手动联调脚本（两台浏览器）

1. 用两个浏览器窗口（或普通 + 无痕窗口，令牌存于 sessionStorage，互不相通）
   打开控制台，分别填入「升降台控制席」「飞行吊点控制席」。
2. 两席同时点击同一动作的「申请控制权」——只有一人成功，另一人按钮置灰并提示
   当前持有者；两侧界面经 1 秒短轮询收敛到同一持有者与同一倒计时。
3. 持有者可「续期 30 秒」「释放」「执行危险动作」。执行成功写入**一条**
   `action_events`，卡片上的历史执行次数 +1。
4. 持有者直接断网（关 Wi-Fi / 关网络命名空间 / 或 DevTools 离线 30 秒），
   到期瞬间（服务端 UTC 到达 `expires_at`，等于即失效）另一席即可申请并接管。
5. 旧页面恢复联网后，轮询发现持有者变更，出现红色「控制权已失效」横幅；此时点
   「旧令牌续期 / 执行 / 释放」都会被服务端以 `409 control_lost` 拒绝，新租约的
   持有者、到期时刻、事件计数完全不变。

## 实现说明

### 租约状态机

每个危险动作一行 `actions` 记录，租约是只追加的 `leases` 行：

`acquired（30s 有效）→ renewed（再 +30s，可多次）→ released | executed`

过期不是状态字段，而是**判断结果**：事务内取 PostgreSQL `now()`，仅当
`released_at IS NULL AND executed_at IS NULL AND now() < expires_at`
才算持有。**`now() == expires_at` 按失效处理**（严格小于），与需求的到期边界
一致。

### 并发：单事务 + 行锁

申请 / 续期 / 释放 / 执行都在**一个数据库事务**内完成：

```sql
SELECT id FROM actions WHERE id = $1 FOR UPDATE;   -- 行锁串行化
-- 然后：读最新租约 → 按 now() 判活 → 校验令牌 → 写入
```

两个进程同时申请同一动作时，后拿到行锁的事务必然看到前者已提交的新租约，只能
返回 `409 lease_held`。因此无需应用层加锁，多 uvicorn worker、多容器下同样成立。
pytest 用 24 线程共享连接池、16 线程各自独立连接池两种方式压测，赢家恒为 1。

### 令牌：不可猜测、只存哈希、仅当前令牌有效

- 授予时生成 256 位 `secrets.token_urlsafe(32)`，仅在授予响应中出现一次；
- 数据库只存 SHA-256 哈希（`token_hash`），拖库无法还原可用令牌；
- 续期 / 释放 / 执行都先在锁内取**最新一条**租约：令牌不匹配（包括旧令牌遇到
  接管后的新租约）一律 `409 control_lost`，且不触碰新租约任何字段。

### 执行只成功一次

`execute` 在同一事务里把租约标记 `executed_at` 并向 `action_events` 插入一条
事件；`action_events(lease_id)` 上有唯一索引，SQL 层面也杜绝重复事件。执行后
租约即终结，动作重新空闲，可被下一席申请并产生新事件。

### 时间只信服务端

- 到期判定全部使用数据库事务内的 `now()`，客户端不能用本地时钟影响结果；
- 界面倒计时用响应里的 `server_time` 锚定（抓取瞬间对齐本地时钟，消除时钟偏差），
  每秒本地重算，不产生额外请求；真正的续期 / 执行仍由服务端重新裁决。

### 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/actions` | 全部动作快照（短轮询） |
| GET | `/api/actions/{id}` | 单个动作快照 |
| POST | `/api/actions/{id}/lease` | 申请（body: `{"holder":"席位名"}`），返回令牌 |
| POST | `/api/actions/{id}/renew` | 续期 30 秒（body 或 Bearer 携带令牌） |
| POST | `/api/actions/{id}/release` | 主动释放 |
| POST | `/api/actions/{id}/execute` | 执行；写且仅写一条动作事件 |

失败响应：`409 lease_held`（争抢失败）、`409 control_lost`（旧/错令牌或已
过期，响应附带最新 `state` 便于界面渲染当前持有者）、`404`（未知动作）、
`401`（缺令牌）。

## 目录结构

```
backend/            FastAPI + psycopg + PostgreSQL
  app/main.py         HTTP 路由
  app/leases.py       事务化租约逻辑（FOR UPDATE / 令牌 / 一次性事件）
  app/db.py           连接池、建表、状态快照
  tests/              pytest：事务竞争、过期边界、HTTP 端到端
frontend/           React + Vite + TypeScript
  src/api.ts          类型化 HTTP 客户端（真实接口）
  src/lease.ts        服务端时钟锚定的倒计时（纯函数，单测覆盖边界）
  src/App.tsx         短轮询控制台
  test/               Vitest 单元 + 组件测试（mock 仅存在于测试目录）
  e2e/                Playwright 双浏览器交接用例（真实服务）
verify/             一次性验收服务的镜像构建与执行脚本
docker-compose.yml  db / api / web / verify
```

## 不用 Docker 的本地开发

```bash
# 后端
python3 -m venv .venv && . .venv/bin/activate
pip install -r backend/requirements.txt
export DATABASE_URL=postgresql://user:pass@localhost:5432/stage
uvicorn app.main:app --reload --port 8000

# 前端（vite 已配置 /api 代理到 8000）
cd frontend && npm install && npm run dev

# 测试
pytest backend -q                     # 需要 PostgreSQL，见 TEST_DATABASE_URL
cd frontend && npm run test:unit
cd frontend && BASE_URL=http://localhost:8080 npx playwright test
```
