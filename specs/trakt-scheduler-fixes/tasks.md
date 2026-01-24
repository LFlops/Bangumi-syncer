# Trakt 数据同步定时任务修复 - 实施任务清单

## 📋 任务状态说明
- ✅ 已完成
- 🟡 进行中
- ⏳ 待开始
- 🔴 阻塞中

## 🏗️ 阶段 1: 修复类型检查错误

### 1.1 修复 auth.py 中的方法签名
- ⏳ **T1.1.1**: 修复 TraktAuthService.handle_callback 方法签名类型错误
- ⏳ **T1.1.2**: 修复 init_oauth 参数类型错误
- ⏳ **T1.1.3**: 修复 TraktAuthService 中缺失的方法

### 1.2 修复类型注解问题
- ⏳ **T1.2.1**: 修复 app/utils/docker_helper.py 中的 `any` 类型使用
- ⏳ **T1.2.2**: 修复 app/utils/bangumi_data.py 中的类型注解问题
- ⏳ **T1.2.3**: 修复 app/core/config.py 中的返回类型问题

### 1.3 修复返回类型和参数类型问题
- ⏳ **T1.3.1**: 修复 TraktHistoryItem 导入问题
- ⏳ **T1.3.2**: 修复各种返回类型不匹配问题

## 🔐 阶段 2: 修复代码风格问题

### 2.1 修复未使用的变量
- ⏳ **T2.1.1**: 修复 tests/trakt/test_auth.py 中未使用的变量
- ⏳ **T2.1.2**: 修复 tests/trakt/test_sync_service.py 中未使用的变量
- ⏳ **T2.1.3**: 修复 tests/integration/test_complete_flow.py 中未使用的变量

### 2.2 修复过时的 API 使用
- ⏳ **T2.2.1**: 修复 Pydantic model.dict() 为 model_dump()

## ⏰ 阶段 3: 修复测试用例

### 3.1 修复认证测试
- ⏳ **T3.1.1**: 修复 tests/trakt/test_auth.py 中的测试用例
- ⏳ **T3.1.2**: 修复 handle_callback 调用方式

### 3.2 修复客户端测试
- ⏳ **T3.2.1**: 修复 tests/trakt/test_client.py 中的 mock 调用方式

### 3.3 修复调度器测试
- ⏳ **T3.3.1**: 修复 tests/trakt/test_scheduler.py 中的方法调用

### 3.4 修复同步服务测试
- ⏳ **T3.4.1**: 修复 tests/trakt/test_sync_service.py 中的方法调用

### 3.5 修复 API 测试
- ⏳ **T3.5.1**: 修复 tests/trakt/test_api.py 中的 API 调用

## 🧪 阶段 4: 修复测试配置

### 4.1 修复 conftest.py
- ⏳ **T4.1.1**: 修复测试配置中的类型问题
- ⏳ **T4.1.2**: 修复数据库管理器模拟问题

### 4.2 修复集成测试
- ⏳ **T4.2.1**: 修复 tests/integration/test_complete_flow.py 中的问题

## 🧪 阶段 5: 验证修复

### 5.1 运行类型检查
- ⏳ **T5.1.1**: 运行 ty 检查确保无错误

### 5.2 运行代码风格检查
- ⏳ **T5.2.1**: 运行 ruff 检查确保无错误

### 5.3 运行测试套件
- ⏳ **T5.3.1**: 运行所有测试确保全部通过

---

**备注**: 所有修复应保持业务逻辑不变，仅修复类型、语法和测试问题。