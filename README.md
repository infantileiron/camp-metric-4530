# 启航 · 训练营指标口径签核

把经营指标从口头约定变为可试算、可比较、可签核冻结的版本记录。

## 真实业务场景

启航训练营经营分析组服务四个线上/线下班级，每天收到班主任的 `启航_班级日报_20261115.xlsx`。原流程由分析员在工作簿计算实到率、结课达成率、退款占比，再把数字抄入经营会纪要。痛点是分子分母的过滤范围不一致、修改公式后旧结论失去依据、数据更正与口径变化无法分清。本项目接收上游班级日事实，替代口径登记/试算/差异解释，给下游经营负责人提供冻结依据。

三个业务实体：指标定义Metric（代码+版本唯一）、班级日事实Fact（班级+日期唯一）、试算签核快照Snapshot，外加报送记录Delivery。五页是口径目录、事实校验、口径试算、签核历史和报送台账；另有登录。后台worker真实计算，签核锁定口径与意见，后续事实修改不改变历史快照。

|角色|权限|典型动作|
|---|---|---|
|admin|全部业务、审计、独占签核|冻结经营依据|
|analyst|口径/事实读写、试算、业务解释、导出、补发报送|建立净实到结课率v2并比较v1|
|viewer|业务只读及导出|下载签核快照，后端拒绝写入和任务提交|

除站内签核外，签核结果还会通过**报送台账**（第5页）整包报送本地接收端：班级分子/分母明细、总计、口径版本与checksum一起入账。

## 快速启动

```sh
cp .env.example .env
docker compose config --quiet
docker compose up -d --build
docker compose exec -T backend python -m scripts.seed_demo
```

前端 [localhost:18210](http://localhost:18210)，后端 [localhost:19210/docs](http://localhost:19210/docs)，接收端 [localhost:18211](http://localhost:18211)，PostgreSQL端口17710。账号为 `admin@demo.local` / `analyst@demo.local` / `viewer@demo.local`，密码均 `Demo123!`。`SEED_DEMO=true`启动自动seed，手动重复不会新增同自然键或覆盖人工修订。种子为四班30日共120条事实（2026-10-17至11-15）、三类口径v1和三账号。

## 一天工作剧本

1. 08:50 viewer查看四班的报名/实到/结课/退款人次。
2. 09:00 analyst核对QH-01的2026-11-15事实，满足结课≤实到≤报名、退款≤报名。
3. 09:10 在事实校验录入新的班级日事实；修订时填写记录ID及完整字段。
4. 09:20 对completion v1（completed / attended）提交2026-10-17至11-15试算，刷新得到基线。
5. 09:30 在口径目录创建completion v2，分子completed，分母attended-refunds，写明“净实到”业务解释。
6. 09:40 新建v2试算，选择v1成功快照作为基线，核对比值差异、变更字段、日期窗和事实checksum。
7. 10:00 在签核历史填写不少于8字的解释，说明是否同时存在事实变化。
8. 10:10 admin签核；该版本不可覆盖，同时生成待报送记录。接收端离线也不影响签核。
9. 10:20 viewer下载 `camp-metric-<id>.json`。
10. 10:30 从报送台账确认状态为已确认，查看回执依据与两端差异；必要时补发。

## 指标计算与安全白名单

允许字段为 enrolled/attended/completed/refunds，分别是班级日的报名/实到/结课/退款人次。表达式仅允许数字、字段、括号、+ − * /和一元正负；AST逐节点解释，禁止函数、属性访问、下标、幂运算、布尔、SQL和任意代码执行。表达式最多200字符/60个AST节点，常量绝对值≤1000000；非有限值、行级除零、极小分母造成超限结果显式失败。

按日期窗过滤事实，再按分子与分母各自的授课方式过滤；每行执行对应表达式，分子求和/分母求和，得到ratio。页面乘100展示百分比；不是日比率的简单平均。例如两班结课6/5、实到8/10，合计为11/18；v2扣除退款1/2后是11/15，绝对差11/90。分子分母过滤不同可能超过100%或为负，需人工解释，不自动截断。

外层总分母为0，value=null并显示undefined，不伪装0，可导出诊断但禁止签核。没有事实、非法行表达式使任务failed，修正后重新试算。每次冻结口径、事实、基线、日期窗及checksum。版本比较仅允许同指标代码，显示字段变化、绝对比值差和日期/事实是否一致；数据改变时不能把全部差异归因于口径。

签核需要已成功、非undefined、业务解释至少8字且口径仍与试算一致；试算后修订口径必须重算。签核后锁定口径版本和意见，事实更正不改旧快照。已有冻结版本可再次以新事实试算，但仍保留各次输入。

## 跨系统报送与本地接收端

签核与报送解耦。admin签核成功时，同一数据库事务写入 `approval=approved` 与一条 `deliveries` 待发送记录（含完整报送包），因此**签核成功必有待报送记录**；接收端未启动或断网只影响发送，不影响签核。后台worker按 `DELIVERY_INTERVAL_SECONDS` 轮询待发送/失败记录，状态流转 `pending 待发送 → sending → confirmed 已确认`（失败为 `failed`，接收端判定为最终拒收时 `rejected`，被更新版本取代为 `superseded`）。

报送包 `camp-metric-package-v1` 使用 `package_id` 与 `checksum` 自校验，内容为冻结口径、日期窗、修订号、签核人与时间、班级分子/分母明细、总计、逐条冻结事实和方法说明。修订号 = 同指标代码+同日期窗的下一个序号，**以后签核替换前次签核**。

接收端 `receiver/` 可独立本机运行，仅用Python标准库与SQLite：

```sh
python3 receiver/server.py          # 默认 127.0.0.1:18211，RECEIVER_DB 指定库文件
```

```sh
RECEIVER_URL=http://127.0.0.1:18211 RECEIVER_TOKEN=campmetric-receiver-demo-token \
  uvicorn src.main:app --reload    # 后端本地开发时把地址指向接收端
```

接收规则：

1. **先校验后入账**：校验 `package_id`/`revision`/日期窗/事实行结构、逐行字段与班级日期唯一性、包`checksum`；再用接收端自己的白名单表达式解释器按班级重算分子分母与总计，逐项与报送端总额、班级明细、事实checksum比对，任一不符即 `422` 拒收，**不入账**，原包与差异保留在回执中。
2. **幂等**：`package_id` 唯一。相同内容重复投递（回执丢失、两端重启、并发重试）返回 `duplicate / already_accounted`，不重复入账。8并发同一包只产生一条回执与一条入账。
3. **同包异值拒收**：同一 `package_id` 但内容/checksum 不同返回 `409 conflict`，不改动已入账数据，并单独记录冲突。
4. **版本单调**：同指标代码+日期窗只保留最新修订。更高修订替换旧修订（`replaced`，班级明细与总计一起更新）；乱序到达的旧修订返回 `409 stale / older_revision_ignored`，不覆盖新版；同修订号但不同包返回 `409 same_revision_different_package`。
5. **事务**：回执与入账在同一个 SQLite `BEGIN IMMEDIATE` 事务提交，进程中断不会留下"已回执未入账"。

页面（报送台账）可查每条报送的状态、尝试次数、接收端回执原文与确认依据（回执中的入账结果、接收端修订号、说明），并可**补发**（已确认记录禁止补发，防止重复入账）、下载原始报送包。**查询两端差异**调用接收端 `GET /api/accounts/...` 与本地包逐字段比对（总计、行数、班级明细、checksum、修订号），列出差异表与接收端重算的班级明细。原签核快照仍可在签核历史下载 `camp-metric-<id>.json`。

接收端接口：`POST /api/receipts`、`GET /api/receipts[/<package_id>]`、`GET /api/accounts[/<code>?date_from=&date_to=]`、`GET /health`、`GET /`（只读视图）。除 `/health` 外均需 `RECEIVER_TOKEN`。

## 工程、本地开发与范围

|层|技术|
|---|---|
|前端|原生ES modules/HTML/CSS，Nginx1.27|
|后端|Python3.11/FastAPI/SQLAlchemy/PyJWT|
|异步|Redis7队列，独立Python worker，心跳健康检查；worker兼轮询报送outbox|
|数据库|PostgreSQL16，命名数据卷；接收端用SQLite单文件库与独立数据卷|

backend等待db/redis健康，worker再等backend，frontend等backend；服务为db/redis/backend/worker/receiver/frontend共六个。数据库、Redis、接收端SQLite库及共享`/artifacts`结果卷持久化。Nginx `/api/`代理到后端根路由。清理用 `docker compose down -v --remove-orphans`。显式网络10.253.111.0/24规避当前机器默认地址池耗尽，可用NETWORK_SUBNET改。

环境变量见.env.example：COMPOSE_PROJECT_NAME、FRONTEND_PORT、BACKEND_PORT、DB_PORT、DB_PASSWORD、JWT_SECRET、SEED_DEMO、NETWORK_SUBNET、RECEIVER_PORT、RECEIVER_TOKEN、DELIVERY_INTERVAL_SECONDS、RECEIVER_TIMEOUT。`RECEIVER_URL` 由compose指向 `http://receiver:8000`；不设置时签核仍生成待报送记录，只是不发送。演示凭据/密钥部署时替换。本地开发先起db/redis，backend安装requirements，设置DATABASE_URL/REDIS_URL/JWT_SECRET后 `uvicorn src.main:app --reload`；前端保留同源Nginx或开发代理。

目录按backend/src的routes/controllers/services/models/schemas/middlewares/config/utils及frontend/src的api/stores/pages/components/hooks/utils/constants/types/router拆分。共享表格、字段、状态标签；表单hooks与HTML转义；请求ID/安全头中间件、业务审计和全局错误处理。枚举ROLES/STATES/MODES/APPROVALS/FIELDS位于后端constants/enums.py、schemas/business.py和前端constants/enums.js。ORM建初表，migrations/001_initial.sql保留实际DDL；无在线版本迁移执行器。

小型范围：逐条表单/API录入，无Excel批导、删除、分页或跨指标引用；单worker，启动恢复待处理任务。班级日事实是人次，不能解释为去重学员留存。签核本身仍在站内，不外发邮件/电子签章；签核结果只报送配置的接收端，无重试上限配置、无死信队列和人工对账导入。Google Fonts依赖网络，离线回退serif/sans-serif。测试证据在 `../../test-artifacts/gb-3111/TESTING.md`，源码无截图/日志。报送与接收端测试：`cd backend && python3 -m pytest tests -q`、`cd receiver && python3 -m pytest test_receiver.py -q`（SQLite与本地子进程，无需Docker）。MIT License见LICENSE。
