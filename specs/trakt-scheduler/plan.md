# Trakt 数据同步定时任务 - 技术方案

## 📋 方案概述

基于 spec.md 的需求，本方案设计一个完整的 Trakt 数据同步定时任务系统，包含 OAuth2 授权、定时调度、异步数据同步和配置管理功能。

## 🏗️ 架构设计

### 系统组件

1. **OAuth2 授权模块** - 处理 Trakt OAuth2 认证流程
2. **调度器模块** - 管理定时任务的执行
3. **Trakt 客户端** - 异步 HTTP 客户端与 Trakt API 交互
4. **数据同步服务** - 调用现有同步服务处理数据上报
5. **配置管理** - 存储和管理 Trakt 相关配置
6. **数据库模型** - 存储 OAuth token 和同步状态

### 技术选型

- **调度器**: `apscheduler` - 功能完善，支持持久化和 Cron 表达式
- **HTTP 客户端**: `httpx` - 异步支持良好，性能优秀
- **配置存储**: 现有 SQLite 数据库 + 配置文件
- **数据验证**: `pydantic` - 类型安全，与现有代码风格一致

## 📁 文件变更清单

### 新增文件

#### 数据库模型
```
app/models/trakt.py
- TraktConfig 模型: 存储用户 Trakt OAuth token 和配置
- TraktSyncHistory 模型: 记录同步历史，用于增量同步
```

#### 服务层
```
app/services/trakt/
├── __init__.py
├── auth.py           # OAuth2 认证流程
├── client.py         # Trakt API 异步客户端
├── scheduler.py      # 调度器管理
└── sync_service.py   # Trakt 数据同步服务
```

#### API 路由
```
app/api/trakt.py
- /api/trakt/auth/init: 初始化 OAuth 授权
- /api/trakt/auth/callback: OAuth 回调处理
- /api/trakt/config: 获取/更新 Trakt 配置
- /api/trakt/sync/status: 获取同步状态
- /api/trakt/sync/manual: 手动触发同步
```

#### 前端集成
```
templates/trakt/
├── auth.html        # Trakt 授权页面
└── config.html      # Trakt 配置页面

static/js/trakt/
├── auth.js          # 授权相关前端逻辑
└── config.js        # 配置管理前端逻辑
```

### 修改文件

#### 配置管理
```
app/core/config.py
- 添加 Trakt 相关配置项读取
- 添加调度器配置管理

config.ini
- 新增 [trakt] 节: 包含 client_id, client_secret 等配置
- 新增 [scheduler] 节: 定时任务配置
```

#### 依赖管理
```
pyproject.toml
- 添加依赖: httpx, apscheduler, pydantic
```

#### 主应用
```
app/__init__.py 或 app/main.py
- 初始化调度器
- 注册 Trakt 相关路由

app/core/database.py
- 添加 Trakt 模型到数据库迁移
```

## 🔧 详细设计

### 1. OAuth2 授权流程实现

```python
# app/services/trakt/auth.py
class TraktAuthService:
    async def init_oauth(self, user_id: str) -> str:
        """生成 OAuth 授权 URL"""

    async def handle_callback(self, code: str, state: str) -> bool:
        """处理 OAuth 回调，获取并保存 token"""

    async def refresh_token(self, user_id: str) -> bool:
        """刷新过期的 access_token"""
```

**数据库表设计**:
```sql
CREATE TABLE trakt_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    access_token TEXT NOT NULL,
    refresh_token TEXT,
    expires_at INTEGER,
    enabled BOOLEAN DEFAULT 1,
    sync_interval TEXT DEFAULT '0 */6 * * *',  -- 每6小时
    last_sync_time INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE trakt_sync_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    trakt_item_id TEXT NOT NULL,  -- Trakt 条目ID
    media_type TEXT NOT NULL,     -- movie, episode
    watched_at INTEGER NOT NULL,  -- 观看时间戳
    synced_at INTEGER NOT NULL,   -- 同步时间戳
    UNIQUE(user_id, trakt_item_id, watched_at)
);
```

### 2. 调度器实现

```python
# app/services/trakt/scheduler.py
class TraktScheduler:
    def __init__(self):
        self.scheduler = AsyncIOScheduler()

    def start(self):
        """启动调度器，为每个启用 Trakt 的用户创建定时任务"""

    def add_user_job(self, user_id: str, cron_expression: str):
        """为用户添加定时任务"""

    def remove_user_job(self, user_id: str):
        """移除用户的定时任务"""

    async def sync_user_data(self, user_id: str):
        """执行用户数据同步（定时任务回调）"""
```

### 3. Trakt 异步客户端

```python
# app/services/trakt/client.py
class TraktClient:
    def __init__(self, access_token: str):
        self.client = httpx.AsyncClient(
            base_url="https://api.trakt.tv",
            headers={
                "Authorization": f"Bearer {access_token}",
                "trakt-api-version": "2",
                "trakt-api-key": config.trakt_client_id
            }
        )

    async def get_watched_history(
        self,
        start_date: Optional[datetime] = None,
        limit: int = 100
    ) -> List[TraktHistoryItem]:
        """获取用户观看历史，支持增量获取"""

    async def get_ratings(self) -> List[TraktRatingItem]:
        """获取用户评分"""
```

### 4. 数据同步服务

```python
# app/services/trakt/sync_service.py
class TraktSyncService:
    def __init__(self):
        self.trakt_client_factory = TraktClient
        self.sync_service = SyncService()  # 现有同步服务

    async def sync_user_trakt_data(self, user_id: str) -> SyncResult:
        """同步用户 Trakt 数据到 Bangumi"""
        # 1. 从数据库获取用户 Trakt 配置
        # 2. 创建 Trakt 客户端
        # 3. 获取增量观看历史（基于 last_sync_time）
        # 4. 转换为 CustomItem 格式
        # 5. 调用 sync_custom_item_async() 批量同步
        # 6. 更新同步状态和记录
```

### 5. 数据转换逻辑

```python
def trakt_history_to_custom_item(
    history_item: TraktHistoryItem,
    user_name: str
) -> Optional[CustomItem]:
    """将 Trakt 观看历史转换为 CustomItem"""
    # 转换规则：
    # - Trakt movie -> Bangumi 电影
    # - Trakt episode -> Bangumi 剧集
    # - 处理剧集信息提取（season, episode）
    # - 设置 watched_at 为播放时间
```

## ⚠️ 风险评估与缓解

### 高风险

1. **OAuth2 安全风险**
   - **风险**: Token 泄露可能导致用户 Trakt 账户被窃取
   - **缓解**:
     - 使用安全的 token 存储（加密存储）
     - 实现 token 自动刷新机制
     - 添加 token 失效检测

2. **API 频率限制**
   - **风险**: Trakt API 有严格频率限制（1000次/小时）
   - **缓解**:
     - 实现请求队列和速率限制
     - 添加指数退避重试机制
     - 监控 API 使用情况

3. **数据一致性**
   - **风险**: 同步过程中断可能导致数据不一致
   - **缓解**:
     - 实现事务性操作
     - 添加同步状态检查和恢复机制
     - 记录详细的同步日志

### 中风险

1. **性能影响**
   - **风险**: 异步任务和数据库操作可能影响主应用性能
   - **缓解**:
     - 使用连接池优化数据库访问
     - 限制并发同步任务数量
     - 添加性能监控

2. **错误处理**
   - **风险**: 网络错误或 API 变更导致同步失败
   - **缓解**:
     - 实现完善的错误分类和处理
     - 添加自动重试和告警机制
     - 提供手动修复工具

### 低风险

1. **依赖兼容性**
   - **风险**: 新依赖与现有依赖冲突
   - **缓解**: 使用 uv 进行精确的依赖版本管理

2. **向后兼容**
   - **风险**: 新功能影响现有功能
   - **缓解**: 保持 API 兼容性，逐步迁移

## 📊 实施计划

### 阶段 1: 基础框架 (2-3天)
- 创建数据库模型和迁移
- 实现 OAuth2 授权流程
- 添加 Trakt 客户端基础功能

### 阶段 2: 调度器集成 (2-3天)
- 实现调度器核心逻辑
- 集成到主应用启动流程
- 添加定时任务管理

### 阶段 3: 数据同步 (3-4天)
- 实现 Trakt 数据获取和转换
- 集成现有同步服务
- 实现增量同步逻辑

### 阶段 4: 前端界面 (2-3天)
- 创建授权和配置页面
- 添加同步状态展示
- 实现手动同步功能

### 阶段 5: 测试优化 (2-3天)
- 单元测试和集成测试
- 性能测试和优化
- 文档编写和部署准备

## 🔍 关键决策点

1. **调度器选择**: 使用 `apscheduler` 而非更简单的 `schedule`，因为需要持久化支持和更复杂的调度需求
2. **异步框架**: 使用 `asyncio` + `httpx` 而非线程池，与现代异步生态更好集成
3. **数据存储**: 使用现有 SQLite 数据库而非独立存储，简化部署和维护
4. **错误处理策略**: 实现分级错误处理，区分网络错误、API 错误和业务逻辑错误

## 📈 成功指标

1. **功能完整性**
   - OAuth2 授权流程正常工作
   - 定时任务按配置准确执行
   - 数据正确同步到 Bangumi

2. **性能指标**
   - 单用户同步时间 < 30秒（100条记录）
   - 内存占用增加 < 50MB
   - 调度器启动时间 < 1秒

3. **稳定性**
   - 7x24 小时运行无崩溃
   - 网络错误自动恢复
   - Token 自动刷新成功率 > 99%

4. **用户体验**
   - 授权流程 < 3步完成
   - 配置界面直观易用
   - 同步状态实时可见

## 🛠️ 测试策略

### 单元测试
- OAuth2 流程测试
- Trakt API 客户端测试
- 数据转换逻辑测试
- 调度器功能测试

### 集成测试
- 完整授权和同步流程
- 错误场景恢复测试
- 并发用户同步测试

### 性能测试
- 大数据量同步测试
- 长时间运行稳定性测试
- 资源使用监控

## 📝 部署注意事项

1. **环境配置**
   - 需要添加 Trakt API 凭据到配置文件
   - 数据库迁移需要执行
   - 调度器需要正确初始化

2. **监控告警**
   - 添加调度器运行状态监控
   - 设置同步失败告警
   - 记录详细的运行日志

3. **备份恢复**
   - OAuth token 需要定期备份
   - 同步历史数据需要保留
   - 提供配置导出/导入功能

---

**技术负责人**: Claude Code
**预计工作量**: 11-16 人天
**优先级**: 高
**依赖**: 现有同步服务稳定运行