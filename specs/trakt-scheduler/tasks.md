# Trakt 数据同步定时任务 - 实施任务清单

## 📋 任务状态说明
- ✅ 已完成
- 🟡 进行中
- ⏳ 待开始
- 🔴 阻塞中

## 🏗️ 阶段 1: 基础框架 (数据库和模型)

### 1.1 数据库模型设计
- ✅ **T1.1.1**: 创建 TraktConfig 模型 (`app/models/trakt.py`)
  - 字段: user_id, access_token, refresh_token, expires_at, enabled, sync_interval, last_sync_time
  - 约束: user_id 唯一索引
  - 方法: is_token_expired(), refresh_if_needed()

- ✅ **T1.1.2**: 创建 TraktSyncHistory 模型 (`app/models/trakt.py`)
  - 字段: user_id, trakt_item_id, media_type, watched_at, synced_at
  - 约束: (user_id, trakt_item_id, watched_at) 联合唯一索引

- ✅ **T1.1.3**: 数据库迁移脚本
  - 创建 Alembic 迁移文件
  - 测试模型创建和基本 CRUD 操作

### 1.2 配置管理扩展
- ✅ **T1.2.1**: 更新 `config.ini` 添加 Trakt 配置节
  - 添加 `[trakt]` 节: client_id, client_secret, redirect_uri
  - 添加 `[scheduler]` 节: 默认同步间隔、启用状态

- ✅ **T1.2.2**: 更新 `app/core/config.py`
  - 添加 Trakt 配置读取方法
  - 添加调度器配置读取方法
  - 确保向后兼容性

### 1.3 依赖管理
- ✅ **T1.3.1**: 更新 `pyproject.toml` 添加新依赖
  - `httpx >= 0.25.0`
  - `apscheduler >= 3.10.0`
  - `pydantic >= 2.0.0`

- ✅ **T1.3.2**: 运行 `uv sync` 安装依赖
  - 验证依赖安装成功
  - 检查依赖兼容性

## 🔐 阶段 2: OAuth2 授权流程

### 2.1 Trakt OAuth2 服务
- ✅ **T2.1.1**: 创建 `app/services/trakt/auth.py`
  - TraktAuthService 类
  - init_oauth(): 生成授权 URL
  - handle_callback(): 处理回调，获取 token
  - refresh_token(): 刷新过期 token

- ✅ **T2.1.2**: OAuth2 工具函数
  - 生成 state 参数和验证
  - Token 加密存储和解密
  - 过期时间计算和检查

### 2.2 API 路由
- ✅ **T2.2.1**: 创建 `app/api/trakt.py`
  - `/api/trakt/auth/init`: 初始化 OAuth 授权
  - `/api/trakt/auth/callback`: OAuth 回调处理
  - 添加路由到主应用路由注册

- ✅ **T2.2.2**: 请求/响应模型定义
  - TraktAuthRequest/TraktAuthResponse
  - TraktCallbackRequest/TraktCallbackResponse
  - TraktConfigResponse, TraktManualSyncResponse 等
  - 错误响应处理

### 2.3 前端授权界面
- ✅ **T2.3.1**: 授权界面集成
  - 授权流程集成在 `templates/trakt/config.html` 中
  - 使用模态框实现授权步骤
  - 授权状态显示和错误信息展示

- ✅ **T2.3.2**: 授权流程JavaScript
  - 处理授权按钮点击 (`static/js/trakt/config.js`)
  - 打开 OAuth 授权窗口和轮询授权状态
  - 授权状态自动更新和重试机制

## ⏰ 阶段 3: 调度器实现

### 3.1 调度器核心
- ✅ **T3.1.1**: 创建 `app/services/trakt/scheduler.py`
  - TraktScheduler 类
  - 基于 AsyncIOScheduler 实现
  - 用户任务管理和调度

- ✅ **T3.1.2**: 调度器配置管理
  - 从数据库读取用户调度配置
  - Cron 表达式解析和验证
  - 任务启停控制

### 3.2 任务管理
- ✅ **T3.2.1**: 用户任务注册和移除
  - add_user_job(): 添加用户定时任务
  - remove_user_job(): 移除用户任务
  - update_user_job(): 更新任务配置

- ✅ **T3.2.2**: 调度器生命周期管理
  - start(): 启动调度器
  - stop(): 停止调度器
  - pause/resume(): 暂停和恢复

### 3.3 集成到主应用
- ✅ **T3.3.1**: 在应用启动时初始化调度器
  - 修改 `app/__init__.py` 或主启动文件
  - 注册调度器关闭钩子

- ✅ **T3.3.2**: 调度器状态监控
  - 运行状态检查接口
  - 任务执行统计
  - 错误日志记录

## 🌐 阶段 4: Trakt API 客户端

### 4.1 异步 HTTP 客户端
- ✅ **T4.1.1**: 创建 `app/services/trakt/client.py`
  - TraktClient 类，基于 httpx.AsyncClient
  - 请求头管理和认证
  - 错误处理和重试机制

- ✅ **T4.1.2**: 请求速率限制
  - 实现令牌桶算法
  - 请求队列管理
  - 并发请求控制

### 4.2 API 端点封装
- ✅ **T4.2.1**: 用户数据获取方法
  - get_watched_history(): 获取观看历史（支持分页）
  - get_ratings(): 获取评分（占位实现）
  - get_collection(): 获取收藏（占位实现）
  - get_user_profile(): 获取用户信息
  - 剧集/电影信息获取方法

- ✅ **T4.2.2**: 增量同步支持
  - 基于 start_date 参数的增量获取
  - get_all_watched_history(): 自动分页获取所有历史
  - 数据去重（基于 trakt_item_id 和 watched_at）
  - 请求速率限制和重试机制

### 4.3 数据模型定义
- ✅ **T4.3.1**: Trakt 数据模型 (`app/services/trakt/models.py`)
  - TraktHistoryItem: 观看历史项
  - TraktRatingItem: 评分项
  - TraktCollectionItem: 收藏项
  - TraktSyncResult, TraktSyncStats: 同步结果统计

- ✅ **T4.3.2**: 数据验证和转换
  - Pydantic 模型定义和验证
  - API 响应到模型的自动转换
  - 数据清洗和异常处理

## 🔄 阶段 5: 数据同步服务

### 5.1 同步服务核心
- ✅ **T5.1.1**: 创建 `app/services/trakt/sync_service.py`
  - TraktSyncService 类
  - 依赖注入: TraktClient, SyncService
  - 同步流程控制: sync_user_trakt_data()

- ✅ **T5.1.2**: 增量同步逻辑
  - 基于 last_sync_time 获取增量数据
  - _should_sync_item(): 避免重复同步
  - 同步进度跟踪和结果统计

### 5.2 数据转换
- ✅ **T5.2.1**: 创建数据转换函数
  - _convert_trakt_history_to_custom_item(): 历史记录转换
  - 剧集信息提取和匹配（show, episode 数据解析）
  - 标题、季数、集数提取

- ✅ **T5.2.2**: 媒体类型映射
  - 目前只处理 Trakt episode -> Bangumi 剧集
  - 电影类型暂时忽略（TODO）
  - 特殊类型处理未实现（TODO）

### 5.3 批量同步优化
- ✅ **T5.3.1**: 并发同步控制
  - 异步任务执行: start_user_sync_task()
  - 任务队列管理: _active_syncs 字典
  - 小延迟避免请求过快

- ✅ **T5.3.2**: 同步结果处理
  - 成功/失败统计: TraktSyncResult 模型
  - 详细错误日志和结果存储
  - 同步报告生成和任务ID管理

## 🖥️ 阶段 6: 前端配置界面

### 6.1 配置管理页面
- ✅ **T6.1.1**: 创建 `templates/trakt/config.html`
  - Trakt 连接状态显示（卡片布局）
  - 同步配置表单（启用/禁用、Cron表达式）
  - 同步历史记录表格（分页支持）

- ✅ **T6.1.2**: 创建 `static/js/trakt/config.js`
  - 配置表单验证和提交
  - 实时状态更新和加载状态显示
  - 手动同步触发（普通/全量）

### 6.2 API 接口扩展
- ✅ **T6.2.1**: 添加配置相关 API (`app/api/trakt.py`)
  - `/api/trakt/config` (GET/PUT): 获取/更新配置
  - `/api/trakt/sync/status` (GET): 获取同步状态
  - `/api/trakt/sync/manual` (POST): 手动触发同步
  - `/api/trakt/disconnect` (DELETE): 断开连接

- ✅ **T6.2.2**: 同步状态查询和前端兼容接口
  - 添加 `/api/sync/history` 端点用于前端兼容
  - 同步历史分页查询
  - 状态实时更新和错误处理

### 6.3 用户界面集成
- ✅ **T6.3.1**: 添加到主导航菜单
  - 在 `app/api/pages.py` 添加 `/trakt/config` 路由
  - 状态指示器和通知系统集成
  - 授权模态框和错误处理

- ✅ **T6.3.2**: 响应式设计适配
  - Bootstrap 5 响应式布局
  - 移动端适配（卡片式布局）
  - 加载状态和错误状态显示

## 🧪 阶段 7: 测试和优化 (进行中 🟡)

### 7.1 单元测试
- 🟡 **T7.1.1**: OAuth2 流程测试（部分完成）
  - 授权 URL 生成功能实现
  - 回调处理功能实现
  - Token 刷新功能实现
  - 单元测试待编写

- 🟡 **T7.1.2**: Trakt 客户端测试（部分完成）
  - API 请求功能实现
  - 错误处理机制实现
  - 速率限制功能实现
  - 单元测试待编写

### 7.2 集成测试
- 🟡 **T7.2.1**: 完整同步流程测试（部分完成）
  - 端到端同步功能实现
  - 数据库操作功能实现
  - 错误场景处理机制实现
  - 集成测试待编写

- 🟡 **T7.2.2**: 调度器测试（部分完成）
  - 定时任务触发功能实现
  - 并发任务管理功能实现
  - 调度器重启恢复功能实现
  - 集成测试待编写

### 7.3 性能测试
- ⏳ **T7.3.1**: 大数据量同步测试（待完成）
  - 1000+ 条记录同步测试
  - 内存使用监控
  - 执行时间优化

- ⏳ **T7.3.2**: 长时间运行测试（待完成）
  - 24小时连续运行测试
  - 资源泄漏检查
  - 稳定性验证

### 7.4 代码质量
- 🟡 **T7.4.1**: 运行代码检查（进行中）
  - 已运行类型检查发现92个问题
  - 已修复Trakt模块相关的类型错误
  - 需要运行 `just lint` 和 `just types` 全面检查

- 🟡 **T7.4.2**: 文档编写（部分完成）
  - API 文档：已有注释文档
  - 用户使用指南：需编写
  - 部署说明：需更新

## 🚀 阶段 8: 部署和监控（待开始 ⏳）

### 8.1 部署准备
- ⏳ **T8.1.1**: 环境配置检查
  - 配置文件模板
  - 环境变量设置
  - 数据库迁移脚本

- ⏳ **T8.1.2**: 依赖打包
  - 更新 requirements.txt
  - Docker 镜像更新
  - 安装脚本更新

### 8.2 监控告警
- ⏳ **T8.2.1**: 运行状态监控
  - 调度器运行状态
  - 同步成功率统计
  - 错误率监控

- ⏳ **T8.2.2**: 告警配置
  - 同步失败告警
  - Token 过期告警
  - 性能下降告警

### 8.3 备份恢复
- ⏳ **T8.3.1**: 数据备份策略
  - OAuth token 备份
  - 同步历史备份
  - 配置备份

- ⏳ **T8.3.2**: 恢复流程
  - Token 丢失恢复
  - 数据库损坏恢复
  - 配置误操作恢复

## 📊 任务优先级和依赖关系

### 高优先级 (必须先完成)
1. T1.1.1, T1.1.2 - 数据库模型 (依赖: 无)
2. T1.3.1, T1.3.2 - 依赖安装 (依赖: 无)
3. T2.1.1, T2.2.1 - OAuth 基础 (依赖: T1.1.1)

### 中优先级 (核心功能)
1. T3.1.1, T3.3.1 - 调度器集成 (依赖: T1.3.2)
2. T4.1.1, T4.2.1 - Trakt 客户端 (依赖: T1.3.2)
3. T5.1.1, T5.2.1 - 同步服务 (依赖: T4.1.1, 现有 SyncService)

### 低优先级 (增强功能)
1. T6.1.1, T6.2.1 - 前端界面 (依赖: T2.2.1, T3.1.1)
2. T7.x.x - 测试套件 (依赖: 各模块完成)
3. T8.x.x - 部署监控 (依赖: 所有功能完成)

## 🔧 实施注意事项

1. **原子性**: 每个任务应尽可能独立完成，便于跟踪和回滚
2. **测试驱动**: 每完成一个任务，运行相关测试验证
3. **代码审查**: 每个模块完成后运行 `just check` 确保代码质量
4. **文档更新**: 代码变更时同步更新相关文档
5. **向后兼容**: 确保现有功能不受影响

## 📝 任务完成标准

每个任务完成后需要:
1. ✅ 代码实现完成并通过编译
2. ✅ 相关测试通过 (如果有)
3. ✅ 运行 `just check` 无错误
4. ✅ 更新本文件中的任务状态
5. ✅ 提交代码变更 (小步提交)

---

**最后更新**: 2026-01-22
**负责人**: Claude Code
**预计开始时间**: 立即
**预计总工时**: 11-16 人天