# Trakt 数据同步定时任务修复计划

## 📋 方案概述

基于 spec.md 的需求，本方案规划修复 Trakt 数据同步功能中的类型检查错误、代码风格错误和测试失败问题。

## 🏗️ 修复策略

### 1. 类型检查修复策略
- 修复 `TraktAuthService.handle_callback` 方法签名以匹配测试用例
- 修复所有 `any` 类型使用问题
- 修复返回类型不匹配问题
- 修复参数类型不匹配问题

### 2. 代码风格修复策略
- 修复未使用的变量问题
- 修复过时的 API 使用（如 `dict()` 替换为 `model_dump()`）
- 修复类型注解问题

### 3. 测试修复策略
- 使测试用例与实际业务逻辑 API 签名匹配
- 修复模拟对象的类型问题
- 修复测试中的语法错误

## 📁 文件修复清单

### 修复类型检查错误
```
app/services/trakt/auth.py
- 修复 handle_callback 方法签名类型错误
- 修复 init_oauth 参数类型错误

app/services/trakt/client.py
- 修复类型注解问题
- 修复返回类型问题

app/core/config.py
- 修复 _get_config_paths 返回类型

app/utils/bangumi_data.py
- 修复类型注解和参数默认值问题

app/utils/docker_helper.py
- 修复 any 类型使用问题
- 修复字典赋值类型错误

tests/conftest.py
- 修复数据库管理器模拟类型问题
- 修复属性分配类型错误
```

### 修复代码风格问题
```
tests/trakt/test_auth.py
- 修复未使用的变量

tests/trakt/test_sync_service.py
- 修复未使用的变量

tests/integration/test_complete_flow.py
- 修复未使用的变量
```

### 修复测试用例
```
tests/trakt/test_auth.py
- 修复 handle_callback 调用方式

tests/trakt/test_client.py
- 修复 mock 对象调用方式

tests/trakt/test_scheduler.py
- 修复方法调用方式

tests/trakt/test_sync_service.py
- 修复方法调用方式
```

## 🔧 详细修复步骤

### 1. 修复类型检查错误

#### 修复 auth.py 中的 handle_callback 方法
- 保持现有方法签名，修改测试用例以匹配
- 或者提供一个兼容的适配器方法

#### 修复类型注解问题
- 将 `dict[str, any]` 替换为适当的类型
- 修复参数默认值类型问题
- 修复返回类型不匹配问题

### 2. 修复代码风格问题
- 移除未使用的变量
- 更新过时的 API 使用

### 3. 修复测试用例
- 调整测试用例以匹配实际 API 签名
- 修复模拟对象的使用方式

## ⚠️ 风险与考虑

### 技术风险
1. **API 签名变更**: 需要谨慎处理，确保不影响现有功能
2. **测试覆盖**: 确保修复不会降低测试覆盖率

### 兼容性考虑
1. **向后兼容**: 修复不应改变业务逻辑行为
2. **接口稳定性**: 保持公共接口稳定

## ✅ 验收标准

1. ty 检查无错误
2. ruff 检查无错误
3. 所有测试用例通过
4. 业务逻辑保持不变

## 📝 实施顺序

1. 首先修复类型注解问题
2. 然后修复代码风格问题
3. 最后修复测试用例
4. 验证所有修复

---

**备注**: 修复过程中需严格遵循项目代码规范，确保类型安全和代码质量。