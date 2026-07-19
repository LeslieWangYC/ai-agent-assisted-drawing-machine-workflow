# Drawingmachine Stage 2 Live Validation Report（Public v1.0 Candidate）

Classification: **PUBLIC CANDIDATE**
Report version: **v1.0 Live Validated (Candidate)**
Derived from: **internal Stage 2 live-validation report (PRIVATE — version, date, and hash recorded privately, not here)**
Extends: **Public v0.9 Offline Code Completion report**
Public code version: **PLANNED — NOT YET PUBLISHED**
Publication status: **PLANNED — NOT YET PUBLISHED**
Live hardware status: **ACCEPTED WITH LIMITATIONS**
Sanitization policy: **report-edition-policy.md / public-sanitization-and-release-plan.md**

## Executive Summary

This report is the public v1.0 candidate for Drawingmachine Stage 2. It extends the public v0.9 report — which certified offline code completion only — with the results of a live validation campaign carried out against a real multi-principal deployment, a real serial controller running FluidNC firmware, and a real OpenClaw agent integration. It does not restate the v0.9 architecture; it reports only what live validation added.

The validation followed a single frozen route, advanced in gated segments: (1) core software readiness on the real host, (2) direct CLI hardware control over the serial link, and (3) OpenClaw agent-contract end-to-end (E2E). Every physical machine action carried its own prospective operator approval, each stating an expected outcome, a stop condition, and a rollback.

Headline results:

- The full machine action chain — RECOVER → HOME → Z-calibration → Z-confirmation → STREAM → HOME-Z → COMPLETED — ran green on real hardware under per-action operator approvals; drawing quality was confirmed by the operator.
- The pipeline is deterministic: the G-code produced through the direct-hardware path and through the independent OpenClaw agent-contract path was verified byte-identical (SHA-256), as was the intermediate processed image.
- The OpenClaw integration is check-only against the admin-owned registry (the product never writes the admin's configuration), and the agent operated strictly as a reviewer, issuing no machine commands.
- 19 defects were found and fixed during live validation. Each fix passed red/green tests, an independent full-suite regression, an offline rebuild, a digest-verified reinstall, and live re-verification.
- Final acceptance: **ACCEPTED WITH LIMITATIONS** (single machine/site; provider production path untested — both live jobs used the reviewed direct route; all live evidence retained privately; real deployment parameters excluded from this public edition — templates/placeholders only).

## 本报告与 v0.9 的关系

v0.9（Offline Code Completion）说明了 Stage 2 重构内容与离线代码证据，并明确声明“只说明离线代码完成，不说明真实部署或硬件已经通过”。本报告在 v0.9 之上加入且仅加入现场验证结果：真实多主体部署、真实串口硬件调机、真实 OpenClaw 集成，以及现场发现并修复的缺陷账本。

依版本策略，内部完整报告是唯一权威源；本公开报告是其脱敏派生物，不是独立设计，也不声明比内部报告更多的功能或更高的验收状态。真实验收状态只来自 Live 会话证据，Fake FluidNC 或离线 demo 不能替代。

## 验证路线与方法

路线在验证开始前冻结，四段推进、段间设审批门：(1) **core software**（核心软件在真实主机就绪）；(2) **direct CLI hardware**（直连串口 FluidNC 调机，逐动作审批）；(3) **OpenClaw E2E**（注册表/受管双根、代理契约端到端）；(4) **reports**（私密 + 本公开脱敏报告）。

方法上的两个不变量：

- **逐动作前瞻审批**：每一个物理机器动作都有独立审批，明确写出**预期结果 / 停止条件 / 回滚**，并带短 TTL 挑战；任一根因未证清即停（stop-on-drift）；主机变更均保留回滚备份。
- **每个缺陷修复的固定闭环**：red/green（先红后绿）→ 独立完整测试套件回归 → 离线（`--no-isolation`，从 clean git archive）重建 wheel + 逐文件 wheel-diff → digest 核验 reinstall → 现场 live 再核验。deployment/operational 类不重建 wheel，但同样经现场再核验。

## 阶段推进顺序

阶段级推进顺序（本公开版不含具体日期时间，亦不含证据文件名）：

| 阶段 | 序 | 里程碑 |
|---|---|---|
| Phase 4 部署面 | 1 | 三主体固定 venv；data-plane 精确 traverse-only ACL（ACTUAL==EXPECTED）；打包 render 引擎放置 configs 与 systemd units；跨用户 allow/deny 全验；发现缺陷 #1、#2 |
| Phase 5 核心软件 | 2 | 客户端端点、operator 只读、automation staging/export、负向证明；缺陷 #3–#13；“CORE SOFTWARE READY” 门 DECLARED；cutover 期间的临时提权配置收官后移除 |
| Phase 6 硬件调机 | 3 | 真实硬件全动作链 RECOVER→HOME→ZCAL→ZCONFIRM→STREAM→HOME_Z→COMPLETED；缺陷 #14–#17；service `enable --now` |
| Phase 7 OpenClaw E2E | 4 | 注册表/受管双根、代理契约 E2E；缺陷 #18–#19；G-code 与 Phase 6 逐字节相同 |
| Phase 8 报告 | 5 | 私密完整报告 + 本公开脱敏报告（覆盖缺陷 #1–#19） |

## 缺陷账本（#1–#19）

缺陷分两类：**deployment/operational-only**（部署或运维侧修复，不重建 wheel：#1、#2、#3、#4、#5、#8、#9、#12）与 **code fix**（代码修复 + 重建 wheel：#6、#7、#10、#11、#13、#14、#15、#16、#17、#18、#19；对应候选版本 v2…v11）。测试套件通过计数与 hostile-matrix pin 计数为可公开的验证事实；所有摘要值、坐标与作业级数值一律省略。

### #1 — data root 与 imports/exports 中间目录 manifest 0700 阻断跨用户 staging（deployment）
- 根因：install manifest 将 data root 与 imports/exports 中间目录设为 0700，跨用户身份 staging 遍历不可达；无代码 validator 覆盖这些路径。
- 修复：部署侧改为 traverse-only ACL（目录加 connect 组 `--x` 遍历项），全部路径 ACTUAL==EXPECTED；暂不重建 wheel。
- 验证：逐路径 ACL 核验。

### #2 — client_endpoint helper 在 data-mode 把端点父目录钉死 0700（deployment，code-recorded）
- 根因：`client_endpoint` helper 以纯 mode 0700 钉住端点父目录，与 bootstrap 的跨用户 lstat 冲突，data-mode 出厂即不可跨用户使用。
- 修复：数据端点软链改由 cutover（manifest creator）创建；service 上下文 lstat/readlink 两链正常（owner automation，target mode 2770）。
- 验证：跨用户读取核验通过。

### #3 — 外层 runtime 目录 ACL 位于易失 tmpfs（operational，volatile per-boot）
- 根因：outer runtime dir 的遍历 ACL 位于 tmpfs，无打包机制维护，重启后丢失。
- 修复：列为每启运维项，开机后重施。同族：service 曾 start 未 enable，manager 重启后失效（首个 operator verify 报 SERVICE_ENDPOINT_INVALID）。
- 验证：现场复核，外层 ACL 在 manager 重启后需重建。

### #4 — 打包 user unit 的 systemd 沙箱与产品自身跨用户 validator 不兼容（deployment，blocking-resolved）
- 根因：非特权 user unit 的 mount-namespace 沙箱运行于单身份 user namespace，外来用户/组映射为 nobody/nogroup；Ubuntu 24.04 apparmor 限制该 userns，部分 `Protect*` 指令被拒。
- 修复：部署 drop-in 仅移除 mount-namespace/capability 指令（保留 seccomp 硬化与 RuntimeDirectory，打包 unit 字节不动）。上游建议：改用 system-unit（`User=`）或从打包 user unit 移除 mount-namespace 指令。
- 验证：首启 forensics 记录问题，drop-in 后服务正常起。

### #5 — O_RDONLY 祖先遍历需读权限，manifest 的 traverse-only ACL 不足（deployment，ACL 加宽）
- 根因：`_walk_directory` 与 `_walk_absolute_directory` 以 O_RDONLY 遍历，每个祖先需 read+search；manifest 自带的 `--x` 遍历项不足。
- 修复：策略链与数据链 ACL 加宽到 `r-x` 并保留部署。
- 验证：journal 显示修复前后 EACCES 位置推进，暴露内在矛盾（#6）。

### #6 — `_lstat_absolute` 以 O_RDONLY 遍历祖先，违反 traverse-only 授权（code；候选 v2）
- 根因：`_lstat_absolute`（client_endpoint.py）以 O_RDONLY 遍历规范 socket 父链（含 outer runtime dir），但 `_require_outer_traverse`（access_policy.py）要求该目录 traverse-only；helper 需 read、service 禁 read，为内在矛盾，无部署解。
- 修复：改用 O_PATH 遍历（仅需 search，身份经 `fstatat` 求得）；`_open_exact_directory` 仍保留 O_RDONLY（其 fd 后续用于 `symlinkat`/`unlinkat`/`fsync`，`fsync` 对 O_PATH 非法）。
- 验证：9060 passed；未补丁复现 PermissionError；hostile-matrix pin 480→481；打包资源 19/19 逐字节不变。

### #7 — oneshot client unit 无 RemainAfterExit，RuntimeDirectory 及端点随退出销毁（code；候选 v3）
- 根因：打包 `client-endpoint` unit 为 `Type=oneshot` + `RuntimeDirectory` 却未设 `RemainAfterExit`；oneshot 一退出，systemd 即删 runtime dir 与刚建端点，unit “成功”却一无所留。
- 修复：模板加 `RemainAfterExit=yes`；install manifest 与模板摘要 digest-pin 同步更新；systemd 资源测试钉住 `RemainAfterExit=yes`。
- 验证：9060 passed；wheel diff vs 上一候选仅 {RECORD、client_endpoint.py、manifest.json、client unit 模板}；stop→dir 删 / start→端点重建。

### #8 — manifest 无客户端侧 config-bundle 交付（deployment）
- 根因：manifest 仅把 config bundle 部署到 service home，但每个 client CLI 协议调用都需在自身配置目录内有 config.toml、machines/<profile>.toml、providers/<profile>.toml 与 service-access.toml（policy 副本须特定 owner/group 0640），无打包机制交付。
- 修复：cutover-created（同 #2 模式）——打包 render 引擎 re-render 四份资源（与 service-home 逐字节相同），两 client 各置 0640，policy 副本属主移交 service、组置为 connect 角色组。
- 验证：跨用户核验通过。

### #9 — 服务启动每次 chmod data_dir 0700，清零客户端遍历 ACL 掩码（operational；后被 #11 收编退役）
- 根因：`_create_private_directory`（runtime.py）每次 start 无条件 chmod data 目录 0700，抹掉授予 client 遍历 imports/exports 的 ACL 掩码。
- 修复（初期，易失）：每次 start 后 owner chmod 0750；经 #11 代码修复后该 reapply 循环退役、0750 持久化。
- 验证：现场核验掩码清零与修复。

### #10 — 阶段目录 default ACL 致 service 无法读取/隔离 client bundle（code；候选 v4）
- 根因：`validate_phase_directory` 强制 prepare/drop 目录 access+default ACL 恰为 {u::rwx, u:automation:rwx, g::---, m::rwx, o::---}；client bundle 继承该 default，service 既非 owner 又非 named user、connect 组解析到空 group-obj，无法读/隔离 bundle，周期性 QUARANTINE_FAILED、角色永久阻塞。盲区：`_validate_automation_acl` 在身份相等时提前返回，套件从不演练此跨用户契约。
- 修复：阶段目录契约新增第二 named-user 项授 service `rwx`；继承到 bundle 即予 service 读与 rename 权限；export 侧契约不变，operator 仍全排除。
- 验证：9062 passed；production 文件 stash 后两新回归红、复原后绿；hostile pin 481→483。

### #11 — 共享 data-root 被反复重私有化（code；候选 v5，与 #13 同候选）
- 根因：`open_trusted_directory` 经 `managed_jobs_root` 每次 artifact op 无条件 fchmod 0700，且 `ServiceRuntime.start()` 每次 start 亦 chmod data 目录 0700，反复把须被 client 遍历的 data root 重私有化。
- 修复：`open_trusted_directory` 引入 `enforce_mode`；`managed_jobs_root` 打开 data root 不 enforce；start() 对缺失 data 目录创建为私有、对既有不动；state 目录与私有 jobs 子目录保留 0700。#9 的 reapply 循环由此退役。
- 验证：9065 passed；3 新回归各 red-on-revert/green-on-fix；hostile pin 483→485。

### #12 — 打包 unit 的 set-user-ID/set-group-ID 限制与产品自身 export chmod 冲突（deployment；入 #4 drop-in 家族）
- 根因：打包 unit 的 set-user-ID/set-group-ID 限制硬化指令，使产品自己对 export 目录做的 set-group-ID chmod（2750）在 seccomp 下 EPERM。
- 修复：部署 drop-in 关闭该限制，并入 #4 drop-in 家族；正向复现（该 chmod 在 unit 外通过、unit 内失败）。
- 验证：复现记录。

### #13 — export 文件 ACL 期望 raw r-- 与继承现实 raw r-x 冲突（code；候选 v5，与 #11 同候选）
- 根因：export 文件从其目录 default ACL 继承 named-automation 为 raw `r-x`；fchmod(0o640) 只把 mask 夹到 `r--`，raw 项仍保留继承值，`validate_export_file` 精确期望 raw `r--` 于是拒绝每个真实发布产物。
- 修复：契约改为期望继承 raw 值，mask 仍把 automation 有效权限夹到只读。
- 验证：9065 passed；产品原码前精确复现。

### #14 — FluidNC 启动 settle 窗口拒绝空白 banner 行（code；候选 v6）
- 根因：打开串口即复位 ESP32（适配器驱动 open 时先拉 DTR，早于 pyserial 预置），boot banner 必随 open 出现且含空白行；`_read_startup` 把空白行喂给严格的 `_decode_line`，在 banner 分类接受 reset 证据前即以 SERIAL_LINE_INVALID 失败，prepare 在真实硬件上永不成功。
- 修复：仅在 startup settle 窗口跳过空白行（不入证据、不算失败、字节仍计入 stabilization）；会话中仍拒绝空白行。回归采用真实录得的 boot banner。
- 验证：9067 passed；wheel diff vs 上一候选仅 serial_fluidnc.py（+2 行）+ RECORD。

### #15 — RECOVER 挑战绑定陈旧 creating-epoch，跨重启恢复结构性不可能（code；候选 v7；含 migration 0005，schema 4→5）
- 根因：恢复挑战绑定到失败 execution 行上冻结的 service_epoch，而 validator 与挑战消费都要求匹配**当前** service epoch，故任何服务重启后恢复在挑战被消费前即结构性失败（MACHINE_PROTOCOL_INVALID）。同一假设在一处 Python guard 与 migration 0003 的两个 SQL 触发器内均有编码。
- 修复：`_recovery_binding` 显式取当前 authority epoch（issue/consume/replay 三处）；guard 加 RECOVER-only carve-out；触发器由新增量 migration `0005_recover_binding_current_epoch.sql` 重建（除 carve-out 外逐行相同，0003 不动，schema 4→5，无表/数据变化）。motion-action epoch 检查不变。
- 验证：9068 passed；部署要求 pre-migration DB 备份卡。

### #16 — 单次静默串口读被当作终止（code；候选 v8）
- 根因：真实 FluidNC boot 在 Grbl ready banner 前含多秒静默（网络连接尝试）；startup settle 在首个中途静默即结束、`_response` 在首个 gap 即失败，破坏 prepare、`$H` homing ack 与 planner-full stream ack。
- 修复：startup settle 仅在“无 boot 输出”或“ready banner 已到”时把静默视为 stabilized；`_response` 对单次 SERIAL_READ_TIMEOUT 重试至绝对截止（到期报 SERIAL_RESPONSE_TIMEOUT）；新增 helper `_is_ready_banner`。
- 验证：9072 passed，net-new 4。

### #17 — session snapshot 要求单条状态报告同时含 MPos+WPos+WCO（code；候选 v9）
- 根因：Grbl/FluidNC 状态报告只带 MPos xor WPos，WCO 仅周期性给出，故 snapshot builder 的“单报告三者齐全”不可满足，新鲜会话锁定 RECOVERY_REQUIRED（PREFLIGHT_REJECTED），一切 motion/stream 后置证明将同样被拒。
- 修复：`_completed_positions` helper 从已报字段派生缺失字段（WCO 回退到观测工作坐标系；WPos=MPos−WCO；MPos=WPos+WCO；已报值优先；从不用 G92/G10/TLO），重连各 snapshot builder。
- 验证：9077 passed，net-new 5。

### #18 — OpenClaw authority 检查只认可 root 与 service，与 manifest 声明的 admin-owned 注册表矛盾（code；候选 v10）
- 根因：`_default_ancestor_authority` 与 `_safe_registry_file` 仅认可 root 与 service writer，但 manifest 与文档声明注册表根为 admin-owned 0750 + service `r-x`，真实 admin-owned 注册表使 `check()` 全量判 UNSAFE；policy schema 无 admin 主体。
- 修复：policy 新增必填 `[registry_owner]` 主体（与 operator/automation 不相交，可等于 service）；两处 authority 检查放行该 owner；模板/manifest 摘要/packaging pins 同步更新（digest-pinned，值不打印）；config 经打包引擎 re-render，旧配置备份保留。
- 验证：independent full suite 9085 passed，net-new 8；还原后 2 个 admin-positive 测试失败；hostile-matrix 491 passed；DB schema 不变（openclaw 命令前须 re-render + replace）。

### #19 — OpenClaw root-alias 祖先遍历 O_RDONLY，需读 traversal-only admin 祖先（code；候选 v11；同 #6 类）
- 根因：`_descriptor_has_ancestor`（openclaw_install.py）以 O_RDONLY 打开 `..`，要求对仅授遍历的 admin 祖先有读权限，`check()` fail-closed 到全量 UNSAFE。
- 修复：一行 O_RDONLY→O_PATH（遍历足矣，仅 `fstat` 这些 fd）；新增 traversal-only 祖先回归测试；hostile-matrix pin 491→492（由独立全量回归补上）。
- 验证：9086 passed；new test 在还原 O_RDONLY 时红；schema/config/DB 均不变。

## 真机调机结果

Phase 6：真实串口硬件、控制器、机械与画笔。全动作链在真实硬件上全绿，逐动作 operator 前瞻审批（各带预期结果/停止条件/回滚，短 TTL 挑战），会话全程未重启：

| 动作 | 结果（定性） |
|---|---|
| RECOVER | 签发绑定当前 epoch 的重启序列挑战并被消费；穿越含网络连接静默的 boot settle；读到真实状态行 preflight → AWAITING_HOME_APPROVAL |
| HOME | `$H` 归位，post-home 位姿证明 → AWAITING_ZCAL_APPROVAL |
| Z-calibration | 笔至纸面接触高度，operator 目视核验 → AWAITING_ZCONFIRM_APPROVAL |
| Z-confirmation | 笔抬至 travel 高度，标定位姿证明 → AWAITING_STREAM_APPROVAL |
| STREAM | 全图流式绘制，operator 确认绘制质量，milestone STREAM_CONFIRMED → AWAITING_HOME_Z_APPROVAL |
| HOME_Z | Z 归零 → **COMPLETED（retired=true，全部指令确认、错误计数为 0、进度 100%）** |

**HV / 串口时序硬件约束（控制器固件层操作事实）**：串口会话打开并保持（HV off）→ 仅在会话保持时开 HV → 任何 close 前先关 HV。理由：HV 开启时关闭并重开串口会破坏后续 boot 的 flash 读取，表现为 `invalid header: 0xFFFFFFFF` boot loop——本机已知的 FluidNC/ESP32-class 硬件怪癖。原 pre-refactor CLI 即围绕此设计；现场采纳同一约束。

**probe-before-fix**：硬件缺陷先经审批的只读探针取原始字节、逐字节定性根因，再 red/green 修复；**HV-off 纠正**推翻“空白 flash”初判，证明是 HV 时序怪癖，回归采用真实录得的 banner。

本公开版不含任何真实机器尺寸、坐标或作业级计数（offset、bounds、行程、路径/段/行/字节均定性化或省略）。

## OpenClaw 集成 E2E 结果

Phase 7 在 Phase 6 完成后进行。

**双根 / 注册表模型**：

- **registry root** = 真实 OpenClaw 安装的 admin-owned 配置目录。产品对它是 **CHECK_ONLY**——只读校验，永不写入 admin 的配置。两个既有 CNC 代理 workspace 自“脚本时代”即存在于此。
- **managed root** = Phase 4 备置的 service-owned 受管树（service 拥有，初始空）。
- 指南文件（AGENTS.md / TOOLS.md）digest-pinned，安装后逐一按打包摘要核验。

**主机侧（按序，全验证）**：按 manifest 行施加注册表权限（admin home 仅遍历、注册表根 traverse + service `r-x`、注册表配置 service `r--`）；两个既有代理 workspace 重指到受管树（仅改 workspace 字段，保留回滚备份）。`openclaw check` → registry MATCHES；`openclaw install` → 4 份指南文件 INSTALLED（0640 service:automation-read）；recheck → **in_sync=True、unsafe=False、全 MATCHES**。

**代理契约 E2E（agent-as-reviewer）**：

- E2E 作业为 reviewed **direct route**：Provider 生产调用与网络访问在本强制块中被明确禁止，且确实未发生。
- 受管树写入 **DENIED**（automation 无写权）。
- `workflow run --input-image` → **BLOCKED/REVIEW_REQUIRED**；processed_image export 经导出端点读取，SHA-256 核验与 Phase 6 同一 processed image 逐字节一致（值不打印）。
- 以 agent 身份写 **PROCESSED_IMAGE_REVIEW_V1 = PASS_TO_BUILD**（schema processed_image_review_v1，**8 项检查全 true**）。
- resume → **READY_TO_RUN**；gcode export 摘要核验通过；契约 `gcode check` COMPLETED，静态检查结果与 Phase 6 定性一致（计量值不打印）。
- 机器挑战为 **REQUEST-only**——**零机器指令**；E2E 作业留在 READY_TO_RUN（有效产物，从未 stream）。

**确定性（跨相）**：Phase 6 直连硬件与 Phase 7 代理契约两条完全不同的路径产出的 G-code 经 **SHA-256 核验逐字节相同**（摘要值不打印），中间 processed image 亦跨相一致，管道确定性得证。

## 部署与运维模型（公开层面）

**多主体 + principle of least authority（仅角色，不含身份/主机/数值）**：

- **service**：权威后台进程，**唯一 SQLite writer**，**唯一串口打开者**（in-service coordinator）。
- **automation**：input staging、受控 artifact export、代理侧读取。
- **operator**：只读 + 逐动作审批；全程排除于 automation 阶段目录。
- **registry owner / admin**：拥有 OpenClaw 注册表；产品对其只读校验。
- worker 不触 SQLite 或硬件；跨用户身份访问一律 traverse-only ACL。

**固定 venv 布局**：每主体固定 per-principal venv（标准 Ubuntu Python 3.12）；三 wheel（产品 + Pillow + pyserial）一并 hash-pinned 安装（`pip install --no-index --no-deps`，对三-wheel manifest 逐一核验），console script exit 0。

**digest-bound install manifest**：安装清单摘要绑定；每次重建 wheel 均做逐文件 wheel-diff，确认“打包资源不变则无需 re-render”，把部署面变更压到最小。

**offline wheel builds**：`--no-isolation` 从 clean git archive 可复现构建（run1==run2）；不打印摘要值，determinism/pinning 以“已核验一致”陈述。

**hostile-matrix packaging gate（计数可公开）**：installed hostile-matrix 随修复由 480 递增至 492；每候选独立完整套件回归，最终候选 9086 passed。

**证据纪律**：私有证据库文件 owner-only、O_EXCL、每个发布文件 O_NOFOLLOW 重开重哈希相等、parent fsync；保留至程序结束，未经前瞻审批不删/不改/不移。

**DB schema**：首建于 schema 4；#15 的增量 migration 0005 使 schema 4→5（仅重建触发器，无表/数据变化），此后保持 5；migration 前强制 DB 备份。

## 经验模式

现场缺陷收敛为若干反复出现的类，均为可公开的工程教训：

- **从未接触真实硬件（never-touched-real-hardware）**：#14/#15/#16/#17 全部只被真实 FluidNC 暴露——open 即复位、多秒网络静默、MPos xor WPos、跨重启 epoch；mock 与同用户身份套件无从复现。
- **O_RDONLY vs O_PATH（同类两次）**：#6（`_lstat_absolute`）与 #19（`_descriptor_has_ancestor`）根因同构——祖先遍历本只需 traverse，却用 O_RDONLY 索要 read，撞 traverse-only 授权而 fail-closed；#5 为同族前身。
- **单一状态报告字段假设**：#17（要求单报告三坐标齐全）为典型；#16（单次静默即终止）、#14（单个空白行即失败）同属“把真实串口流的稀疏/间断当异常”。
- **docs/manifest 与 code 授权不一致**：#18（manifest 声明 admin-owned 注册表，code 只认 root/service）；#1（manifest 0700 vs 跨用户遍历需求）；#8（manifest 无 client 侧 config bundle 交付机制）。
- **同用户身份套件盲区（same-user-identity test blindness）**：#10（`_validate_automation_acl` 在身份相等时提前返回）、#11、#13 等跨用户 ACL 契约对同身份套件不可见——本程序最反复的教训。
- **systemd 沙箱 vs 自身跨用户校验**：#4/#12——非特权 user unit 的单身份 user namespace 与 set-user-ID/set-group-ID 限制，与产品自身跨用户语义冲突（Ubuntu 24.04 apparmor 加剧）。

## 限制与结论

**最终结论：ACCEPTED WITH LIMITATIONS。**

全动作链在真实硬件上全绿、管道确定性经 SHA-256 得证、19 项缺陷现场发现并修复。验收带以下明确限制：

1. **单机 / 单站点验证**：仅在单一真实机器/站点验证，未做多机/多站点泛化。
2. **现场证据私有保留**：全部 live 证据保留于私有证据库，未公开发布。
3. **真实部署参数排除**：真实部署参数仅以模板 / 占位符呈现；真实 endpoint、串口、机器 profile、注册表、workspace、主机与身份标识均不在公开版。
4. **私有账本存在少量记录留存缺口**：私有账本中存在少量记录性缺口（不逐项列举）。
5. **Provider 生产链路未测试**：两次现场作业（直连硬件与代理契约 E2E）均走 reviewed direct route；真实图像生成 Provider 的生产调用与网络访问按验证规程在强制块中被明确禁止，故未经现场验证，Provider 仅以 Fake Provider 在离线套件中验证。此为有界的可选限制，不豁免且不影响任何强制门的验收。
6. **不超出内部报告**：本公开版不声明任何超出内部报告的主张；公开功能范围可能小于内部部署版，差异以功能类别在 Public release notes 说明。Public demo 与 Fake FluidNC 不能替代真实硬件验收。

Public v1.0 的正式形成取决于 sanitized 仓库独立审核、发布映射与哈希记录（Public repository 尚未发布，见顶部元数据）。
