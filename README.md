# 联排控制权交接台 · 升降台 × 飞行吊点

升降台控制席与飞行吊点控制席进入联排后，可能同时点击同一个危险动作；席位断网后
系统既不能永久锁死，也不能让它恢复后凭旧凭据再次执行。本项目是一个**真实可联调**
的控制权交接台：

- **React + TypeScript** 控制台：每个浏览器会话都能看到动作状态（持有席位、
  剩余秒数、执行结果），并可申请 / 续期 / 释放 / 执行；联排负责人还可在
  控制台**开始 / 结束场次**，把一段现场操作归入明确的一轮；
- **FastAPI** 服务：授予不可猜测令牌，所有判定只按**服务端 UTC 时间**；
- **PostgreSQL**：单事务行锁保证同一动作同时只有一个有效租约；部分唯一索引
  保证任一时刻只有一个进行中场次。

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
   赢家、独立连接池同样串行化）、**过期边界**（`now == expires_at` 即失效、
   旧令牌迟到的续期 / 释放 / 执行全部拒绝且新租约不变、执行只写一次事件）、
   **联动执行**（两条事件共享联动标识且各计数一次、交换动作顺序结果一致、
   并发的联动与单动作执行只有一方提交、一枚旧令牌使整次操作无副作用）、
   **场次归属**（单动作与联动事件在原事务内计入当前场次、并发开始仅一个
   进行中场次、重复开始 / 空白名称 / 无进行中场次时结束均为可识别业务错误
   且不改动事件、结束后摘要冻结且后续动作不再计入）与**现场异常留痕**
   （异常挂在最近一次执行事件上、重复报告 / 确认携带当前记录返回业务错误且
   首次数据不被改写、仅另一控制台可确认（同一页面改席名仍被稳定 id 拒绝）、
   16 路并发确认仅一人写入、新事件到来后旧记录保留但卡片
   不再显示、重新申请 / 释放 / 单动作 / 联动均不覆盖旧记录）；
2. Vitest：倒计时边界、接管 / 旧令牌界面逻辑，场次全流程组件测试
   （开始 → 两类动作执行 → 结束后摘要冻结），以及异常报告 / 确认组件测试
   （提交成功后轮询展示报告人与时间、空白说明就地反馈且不发请求、同一页面
   改席名不出现确认入口、新事件切换卡片后不误显旧异常）；
3. Playwright：两个真实浏览器上下文模拟双控制席——同时争抢只有一人持有、
   失联满 30 秒后另一席立即接管、旧页面再次点击明确显示「控制权已失效」；
   另有双动作联动用例（一席持两动作一次联动成功、旧令牌联动被 409
   拒绝且另一租约不受影响）、场次用例（控制台开始场次 → 单动作 +
   联动各执行一次 → 结束场次 → 摘要固定且随后执行不再计入），以及异常
   交接用例（一席执行并报告异常、另一浏览器看到待确认记录后确认、两侧
   刷新后仍展示已确认结果与确认席位）。

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

### 联动执行（双设备协同段）

进入双设备协同段后，控制席把升降台与飞行吊点作为**一次联动动作**提交，避免
先后点击造成一台已执行而另一台失败：

- 联动**恰好**由两个动作组成：一个升降台动作（上升/下降）与一个飞行吊点动作
  （进场/退场）。同一台设备互斥的两个方向（如「上升 + 下降」）不能联动，
  持有三项动作也不会出现联动入口；紧急停止不参与联动；
- 本席分别取得这两个动作的有效控制权后，页面出现「联动执行」条，一次提交
  两枚令牌到 `POST /api/actions/execute-linked`；
- 服务端在**同一事务**内按**动作编号字典序**依次锁定动作行（固定顺序，与请求
  中的排列无关，也不会与并发的单动作执行死锁），逐个校验租约与令牌，全部有效
  才终结各租约并各写一条 `action_events`；
- 这两条事件共享一个**服务端生成的联动标识**（`link_id`）；动作快照新增
  `last_link_id` 供两张卡片展示「最近联动」，历史单动作事件的 `link_id`
  保持 `NULL`；
- 任一令牌缺失（`401`）、过期或已被接管（`409 control_lost`）时，响应的
  `detail.action_id` 指明**具体失效动作**并附带各动作最新快照；事务整体回滚
  ——不写入任何事件，也不终结另一个仍然有效的租约。页面不清除任何令牌，两张
  卡片保持真实控制状态，失效卡片提示重新取得控制权。

### 场次（一轮联排的归属与摘要）

负责人把一段现场操作归入明确场次，避免动作事件散落后无法判断本轮排练的
执行范围与结果：

- 控制台提供**场次控制入口**：填写场次名称后「开始场次」，进行中可
  「结束场次」。两者都走同一个状态转换接口
  `POST /api/sessions/transition`（body: `{"op":"start","name":…}` 或
  `{"op":"end"}`）；场次只在 **进行中 → 已结束** 之间单向流转；
- **任一时刻只有一个进行中场次**：`rehearsal_sessions` 上的部分唯一索引
  （`WHERE ended_at IS NULL`）在 SQL 层面保证并发开始只有一个赢家，败者
  收到 `409 session_active`；
- 单动作执行与双动作联动**成功写入事件时**，在**原事务**内把当前进行中场次
  的 id 写入 `action_events.session_id`（无进行中场次时保持 `NULL`，历史
  行为不变）；提交或回滚与事件本身同生共死；
- 动作轮询响应 `GET /api/actions` 新增 `session` 摘要（场次名称、状态、
  **累计事件数**、**涉及动作数**），页面持续展示本轮摘要；结束后摘要
  保持可查（`GET /api/sessions/current` 同样返回），随后执行的动作不再
  计入该场次——新一轮开始前的摘要数字就此冻结；
- 错误边界均为可识别业务错误且不改动任何事件：重复开始
  `409 session_active`、名称空白 `400 invalid_name`、没有进行中场次却
  请求结束 `409 no_active_session`；错误响应的 `detail.session` 附带当前
  场次摘要便于界面重渲染。

### 现场异常留痕（报告 → 下一班确认）

演出复盘时，控制席把**某次已执行动作的现场异常留在对应事件上**，并让下一班
确认已经看到，避免口头交接后无法追溯：

- 动作卡片在该动作**最近一次执行事件**存在后出现「报告异常」入口：选择异常
  类别（设备 / 操作 / 环境 / 其他）并填写说明后提交，生成一条 `pending`
  （待确认）记录；空白说明在组件内**就地反馈**、不发送请求；轮询快照在卡片
  上展示类别、说明、**报告人**与**报告时间**（服务端 UTC）；
- 记录保存在新表 `action_anomalies` 中，外键关联 `action_events(id)`，
  且每个事件至多一条（唯一索引从 SQL 层杜绝重复报告）；
- **另一席**在同一张卡片看到待确认记录后点击「下一班确认已看到」：服务端只
  允许 `pending → confirmed` 这一种转换（`UPDATE ... WHERE status='pending'`
  条件更新），保存**确认席位、确认控制台与确认时间**；16 路并发确认也只有
  一人写入；
- **报告控制台不能确认自己的报告**，且该判定基于**稳定的控制台身份**而非可
  编辑的席名：每个浏览器控制台首次进入时生成一个存于 `sessionStorage` 的
  `console-id`（与席名分离，报告时随 `reporter_id` 落库），确认时携带
  `confirmer_id`。因此**报告席在同一页面把本席名称改成「下一班」也无法确认**
  （`409 anomaly_self_confirm`，界面上始终只显示「等待下一班确认」、没有确认
  按钮）；反过来，两个不同控制台即使写成同一席名仍是两席、后者可以确认；
- 重复报告返回 `409 anomaly_exists`、重复确认返回 `409 anomaly_confirmed`，
  两者都在错误体 `detail.anomaly` 中**携带当前记录**并附 `detail.state`
  最新快照——失败永远不改写首次数据（后端测试直接核对行内容）；类别无效 /
  说明、席位或控制台身份空白为 `400 invalid_anomaly`，尚无执行事件时报告为
  `409 no_execution_event`，没有待确认记录却确认是 `409 anomaly_not_found`；
- 记录**只追加、不覆盖**：重新申请、释放以及单动作 / 联动执行都不会改写旧
  记录。卡片快照只跟随**最新事件**的异常——新事件到来后卡片自然切换，
  `anomaly` 变为 `null`，不会误显上一事件的旧异常（旧行仍在 PostgreSQL 中，
  可按事件追溯）；
- 与既有契约一致：报告 / 确认同样先 `SELECT ... FOR UPDATE` 锁定动作行，
  复用 FastAPI 既有错误信封（`detail.code/message/state`），租约、联动与
  场次接口字段完全保持兼容（`anomaly`、`last_event_id` 均为新增字段）。

### 时间只信服务端

- 到期判定全部使用数据库事务内的 `now()`，客户端不能用本地时钟影响结果；
- 界面倒计时用响应里的 `server_time` 锚定（抓取瞬间对齐本地时钟，消除时钟偏差），
  每秒本地重算，不产生额外请求；真正的续期 / 执行仍由服务端重新裁决。

### 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/actions` | 全部动作快照（短轮询），含 `last_link_id`、`last_event_id`、最新事件的 `anomaly` 与 `session` 场次摘要 |
| GET | `/api/actions/{id}` | 单个动作快照 |
| POST | `/api/actions/{id}/lease` | 申请（body: `{"holder":"席位名"}`），返回令牌 |
| POST | `/api/actions/{id}/renew` | 续期 30 秒（body 或 Bearer 携带令牌） |
| POST | `/api/actions/{id}/release` | 主动释放 |
| POST | `/api/actions/{id}/execute` | 执行；写且仅写一条动作事件（计入当前场次） |
| POST | `/api/actions/execute-linked` | 联动执行（body: `{"items":[{"action_id","token"},…]}`），全部成功或全部不变 |
| POST | `/api/actions/{id}/anomaly` | 为最近一次执行事件报告异常（body: `{"category","description","reporter","reporter_id"}`），生成待确认记录 |
| POST | `/api/actions/{id}/anomaly/confirm` | 另一控制台确认已看到（body: `{"confirmer":"席位名","confirmer_id":"控制台 id"}`），仅允许待确认 → 已确认 |
| POST | `/api/sessions/transition` | 场次状态转换（body: `{"op":"start","name":…}` / `{"op":"end"}`） |
| GET | `/api/sessions/current` | 当前场次摘要（进行中或最近已结束），结束后仍可查 |

失败响应：`409 lease_held`（争抢失败）、`409 control_lost`（旧/错令牌或已
过期，响应附带最新 `state` 便于界面渲染当前持有者；联动接口以
`detail.action_id` 指明具体失效动作并附 `states`）、`404`（未知动作）、
`401`（缺令牌）、`409 session_active`（重复开始场次）、`400 invalid_name`
（场次名称空白）、`409 no_active_session`（无进行中场次却请求结束）、
`400 invalid_anomaly`（异常类别无效 / 说明或席位空白）、
`409 no_execution_event`（尚无执行事件却报告异常）、
`409 anomaly_self_confirm`（报告控制台按稳定 id 尝试确认自己的报告，改名也不允许，仅另一控制台可确认）、
`409 anomaly_exists`（重复报告，`detail.anomaly` 携带当前记录）、
`409 anomaly_not_found`（当前事件没有待确认记录）、
`409 anomaly_confirmed`（重复确认，携带已确认记录与确认席位）。

## 目录结构

```
backend/            FastAPI + psycopg + PostgreSQL
  app/main.py         HTTP 路由
  app/leases.py       事务化租约逻辑（FOR UPDATE / 令牌 / 一次性事件）
  app/sessions.py     场次状态转换与摘要（开始 / 结束 / 当前场次）
  app/anomalies.py    现场异常报告与下一班确认（待确认 → 已确认，只追加）
  app/db.py           连接池、建表、状态快照
  tests/              pytest：事务竞争、过期边界、HTTP 端到端、场次归属、异常留痕
frontend/           React + Vite + TypeScript
  src/api.ts          类型化 HTTP 客户端（真实接口）
  src/lease.ts        服务端时钟锚定的倒计时（纯函数，单测覆盖边界）
  src/App.tsx         短轮询控制台（含场次控制、本轮摘要与卡片异常留痕）
  test/               Vitest 单元 + 组件测试（mock 仅存在于测试目录）
  e2e/                Playwright 双浏览器交接 + 联动 + 场次 + 异常确认用例（真实服务）
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
