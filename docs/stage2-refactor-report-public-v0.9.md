# Drawingmachine Stage 2 Refactor Report（Public v0.9 Candidate）

Classification: **PUBLIC CANDIDATE**
Report version: **v0.9 Offline Code Completion**
Public code version: **PLANNED — NOT YET PUBLISHED**
Publication status: **PLANNED — NOT YET PUBLISHED**
Live hardware status: **NOT YET VALIDATED**

## 项目现在是什么

Drawingmachine 是一个本地优先的绘图工作流和 FluidNC 控制系统。它把图片处理、路径规划、G-code 安全检查、任务持久化、人工审批和机器执行放在一个可安装的 Python package 中，并通过统一的 `drawingmachine` CLI 使用。

Stage 2 的核心变化是从“按顺序运行多个脚本”转向“CLI + 后台 service + durable state + typed hardware state machine”。这不是为了增加概念，而是为了在请求重复、并发、进程崩溃或硬件结果不确定时避免重复副作用。

## 重构前后

Stage 1 主要由多个脚本、pipeline、库目录和配置文件组成。调用者需要知道每个步骤的输入输出，并人工判断失败后是否可以重试。

Stage 2 提供：

- 一个安装入口和统一 CLI；
- 一个拥有数据库和机器会话的本地 service；
- 严格、版本化的 JSON/socket protocol；
- SQLite job/event/audit/recovery 状态；
- 受监督 worker 和 Fake Provider；
- automatic input staging 和受控 artifact export；
- Fake FluidNC 和 typed serial boundary；
- 独立的一次性 operator approvals；
- packaged configuration、systemd templates 和 OpenClaw agent bundle；
- offline、packaging、architecture、hostile 和 E2E tests。

## 典型工作流程

1. 用户或 automation 通过 CLI 提交图片。
2. CLI 自动 staging 普通用户可读的输入，不要求手工复制到共享目录。
3. Service 根据 Unix peer identity 判断角色并持久化请求。
4. Worker 完成图片处理和路径规划，但不能访问 SQLite 或硬件。
5. Service 验证 artifact digest、revision 和安全结果。
6. Job 到达 `READY_TO_RUN` 后，机器流程仍需人工 operator 对每个动作分别批准。
7. HOME、Z calibration、Z confirmation 和 STREAM 按固定状态机执行。
8. 不确定的 controller 结果进入 recovery，不自动重试 STREAM。

## 主要模块

模块 | 职责
--- | ---
Domain | 纯状态、模型和安全规则
Application | 组织 job、staging、OpenClaw 和 machine use cases
Ports | 定义 repository、provider、worker 和 FluidNC 接口
Adapters | SQLite、filesystem、worker、provider 和 serial 实现
Service | 权威后台进程、peer authorization 和协调器
CLI | 用户与 automation 的统一命令入口
Config | 严格 TOML 和 digest-bound configuration
Resources | packaged templates、units 和 OpenClaw bundle

## 为什么有这么多并发与恢复逻辑

这里的并发设计主要保护正确性：

- 重复请求不会创建两个任务或重复运动；
- SQLite 只有一个 service writer；
- machine session 只有一个 owner；
- 文件发布到一半不会被当成完整输入；
- client timeout 不会被误判为“服务一定没执行”；
- 旧 approval 在 service/session restart 后失效；
- ambiguous stream acknowledgement 不会触发自动重试。

系统选择保留不确定性并要求人工恢复，而不是猜测硬件没有动作。

## Hardware safety shape

Public 版本计划包含 Fake FluidNC、typed status/response/gate、G-code safety 和 machine state tests。硬件接口只暴露 preflight、HOME、ZCAL、ZCONFIRM、validated STREAM、optional HOME_Z 和 close，不提供 raw jog、unlock、offset mutation 或 arbitrary command surface。

Fake FluidNC 用于证明代码的状态机和错误处理，并不代表真实机器已经验收。真实串口、控制器、机械、电气、校准和画笔结果需要独立 live validation。

## Offline verification

Stage 2 offline code checkpoint 的最终证据包括：

- 9030 个完整测试项通过；
- total coverage 97.62%；
- branch coverage 7303/7544，96.81%；
- 61 个 safety/authority owner modules 分别达到 100% branch coverage；
- G-code safety 144/144 branches；
- strict typing、lint、format 和 7 个 import contracts；
- clean wheel/sdist build、installed-package、architecture、docs 和 E2E gates；
- independent whole-branch review。

这些结果只说明离线代码完成，不说明真实部署或硬件已经通过。

## Public edition scope

计划公开并保留：

- 通用 CLI、domain/application/ports 架构；
- offline workflow 和 planning/G-code core；
- generic filesystem/SQLite/worker/provider adapters；
- Fake Provider 与 Fake FluidNC；
- safety、recovery 和 architecture tests；
- generic configuration/systemd/OpenClaw templates；
- deterministic fixtures 和 offline demo；
- reproducible build/test documentation。

计划排除或模板化：

- 个人 identity、host、home 和设备参数；
- 真实 endpoint、serial、machine profile、registry 和 workspace；
- token、日志、数据库、任务数据和生成 artifact；
- private deployment evidence；
- internal controller/review history；
- 不适合公开的第三方或个人素材。

## 如何验证 Public 版本的真实性

Public release 必须能够在 clean environment 中：

- 构建 wheel/sdist；
- 安装 package 并核对 CLI/resources；
- 运行完整允许公开的自动化测试；
- 通过 Fake Provider/Fake FluidNC end-to-end demo；
- 复现安全和 recovery hostile cases；
- 通过 secret、privacy、license 和 Git history audit。

Public 功能范围可能小于内部部署版本，差异会在 Public release notes 中明确说明。Public demo 不能替代真实硬件验收。

## Current limitations

- Public repository 尚未创建或发布。
- 真实跨用户 Linux permissions 尚未 live 验收。
- OpenClaw 实装、reload 和真实 automation chain 尚未执行。
- Provider network 尚未 live 验收。
- 真实 serial、FluidNC controller、HOME/ZCAL/ZCONFIRM/STREAM 和画笔绘制尚未执行。
- 因此当前状态是 `Offline Code Completion`，不是 `Live Validated Release`。

Public v1.0 只会在 sanitized repository 独立审核、真实验收结论和发布映射都完成后形成。
