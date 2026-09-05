"""
MCP 内部 API 配置端点测试（/app/api/mcp_config.py）

覆盖 BDD 场景：
1. GET /api/mcp/config → 全量配置，敏感字段已脱敏
2. GET /api/mcp/config/schema → serialize_schema() 元数据
3. POST /api/mcp/config/update → 合法段修改成功，返回变更摘要
4. POST /api/mcp/config/update → 不存在段 → 400 + 合法段名提示
5. 敏感字段写入 → 落盘为密文，响应不回显明文
6. 无凭证 → 401；写操作无 write scope → 403
"""

import base64
import json
import tempfile
import time
from unittest.mock import MagicMock, patch

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import deps

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_config_manager():
    """构造内存型 mock ConfigManager，避免读写真实 config.ini。"""
    cm = MagicMock()
    # get_all_config 返回含敏感字段的典型结构
    cm.get_all_config.return_value = {
        "auth": {
            "username": "admin",
            "webhook_key": "plain-webhook-key-123",
            "session_timeout": 3600,
            "enabled": True,
        },
        "sync": {
            "match_confidence_threshold": 0.6,
        },
        "llm": {
            "api_key": "sk-plain-key",
            "provider": "openai_compat",
        },
    }
    return cm


def _make_app(mock_config_manager, user_scope):
    """创建挂载 mcp_config router 的测试 app，注入 mock 与鉴权覆盖。"""
    from app.api import mcp_config

    app = FastAPI()
    app.include_router(mcp_config.router)

    # 覆盖 mcp_config 模块级依赖实例（确保 key 匹配）
    app.dependency_overrides[mcp_config._mcp_read_dep] = lambda: user_scope
    app.dependency_overrides[mcp_config._mcp_write_dep] = lambda: user_scope

    return app, mcp_config


@pytest.fixture
def app(mock_config_manager):
    """具备 read+write scope 的 app 实例。"""
    from app.api import mcp_config

    app, _ = _make_app(
        mock_config_manager,
        {"username": "mcp-test", "scope": ["read", "write"], "mcp": True},
    )
    with patch.object(mcp_config, "config_manager", mock_config_manager):
        yield app
    app.dependency_overrides.clear()


@pytest.fixture
def app_read_only(mock_config_manager):
    """只有 read scope 的 app 实例（用于 403 测试）。

    只覆盖 read 依赖；write 依赖保持真实 JWT 校验路径，由 mcp_deps_env
    注入公钥路径。请求需携带仅含 read scope 的 JWT，触发真实 403。
    """
    from app.api import mcp_config

    # 生成 RSA 密钥对（与 test_mcp_auth.py 同模式）
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()

    app = FastAPI()
    app.include_router(mcp_config.router)

    # 只覆盖 read dep；write dep 走真实 JWT 校验
    app.dependency_overrides[mcp_config._mcp_read_dep] = lambda: {
        "username": "mcp-readonly",
        "scope": ["read"],
        "mcp": True,
    }

    # 构造仅含 read scope 的 JWT
    def _b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    now = int(time.time())
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = _b64url(
        json.dumps(
            {
                "sub": "readonly",
                "scope": "read",
                "iss": "mcp",
                "aud": "bs",
                "iat": now,
                "exp": now + 3600,
            }
        ).encode()
    )
    signing_input = f"{header}.{payload}".encode()
    sig = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    token = f"{header}.{payload}.{_b64url(sig)}"

    # 将公钥写入临时文件并通过环境注入
    with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as f:
        f.write(
            public_key.public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )
        pem_path = f.name

    with (
        patch.object(deps, "_MCP_PUBLIC_KEY_PATH", pem_path),
        patch.object(deps, "_MCP_AUDIENCE", "bs"),
        patch.object(deps, "_MCP_ISSUER", "mcp"),
        patch.object(mcp_config, "config_manager", mock_config_manager),
    ):
        # 让 token 可通过 fixture 传递给测试
        app._test_token = token
        yield app

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Scenario 1: 读取当前配置，敏感字段已脱敏
# ---------------------------------------------------------------------------


class TestGetConfig:
    """GET /api/mcp/config → 全量配置，敏感字段掩码。"""

    @pytest.mark.asyncio
    async def test_get_config_返回成功包络(self, app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/mcp/config")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert "data" in body

    @pytest.mark.asyncio
    async def test_get_config_敏感字段已脱敏(self, app):
        """auth.webhook_key 与 llm.api_key 应被掩码，非敏感字段明文保留。"""
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/mcp/config")
        data = resp.json()["data"]
        # 敏感字段 → 掩码
        assert data["auth"]["webhook_key"] == "***"
        assert data["llm"]["api_key"] == "***"
        # 非敏感字段 → 明文
        assert data["auth"]["username"] == "admin"
        assert data["auth"]["session_timeout"] == 3600
        assert data["sync"]["match_confidence_threshold"] == 0.6

    @pytest.mark.asyncio
    async def test_get_config_调用_config_manager(self, app, mock_config_manager):
        """端点应委托 config_manager.get_all_config()。"""
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.get("/api/mcp/config")
        mock_config_manager.get_all_config.assert_called_once()


# ---------------------------------------------------------------------------
# Scenario 2: 读取配置 schema
# ---------------------------------------------------------------------------


class TestGetSchema:
    """GET /api/mcp/config/schema → serialize_schema() 元数据。"""

    @pytest.mark.asyncio
    async def test_get_schema_返回序列化结构(self, app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/mcp/config/schema")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        sch = body["data"]
        assert "sections" in sch
        assert "config_defaults" in sch
        assert isinstance(sch["sections"], list)
        assert len(sch["sections"]) > 0

    @pytest.mark.asyncio
    async def test_get_schema_包含已知段(self, app):
        """schema 应包含 auth / sync / llm 等已注册段。"""
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/mcp/config/schema")
        section_names = [s["name"] for s in resp.json()["data"]["sections"]]
        assert "auth" in section_names
        assert "sync" in section_names
        assert "llm" in section_names


# ---------------------------------------------------------------------------
# Scenario 3: 修改配置（合法段）
# ---------------------------------------------------------------------------


class TestUpdateConfig:
    """POST /api/mcp/config/update → 合法段修改成功，返回变更摘要。"""

    @pytest.mark.asyncio
    async def test_update_合法段_返回成功与摘要(self, app, mock_config_manager):
        payload = {"sync": {"match_confidence_threshold": 0.7}}
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/mcp/config/update", json=payload)
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        # 变更摘要应包含改动的 section/key
        assert "sync" in str(body)

    @pytest.mark.asyncio
    async def test_update_调用_set_config(self, app, mock_config_manager):
        payload = {"sync": {"match_confidence_threshold": 0.7}}
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post("/api/mcp/config/update", json=payload)
        mock_config_manager.set_config.assert_called_with(
            "sync", "match_confidence_threshold", 0.7
        )

    @pytest.mark.asyncio
    async def test_update_多段批量(self, app, mock_config_manager):
        payload = {
            "sync": {"match_confidence_threshold": 0.8},
            "llm": {"timeout": 120},
        }
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/mcp/config/update", json=payload)
        assert resp.status_code == 200
        assert mock_config_manager.set_config.call_count == 2

    @pytest.mark.asyncio
    async def test_update_下划线段名_自动转连字符(self, app, mock_config_manager):
        """前端可能用下划线段名（如 bangumi_data），应归一化为连字符。"""
        payload = {"bangumi_data": {"cache_ttl_days": 14}}
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/mcp/config/update", json=payload)
        assert resp.status_code == 200
        mock_config_manager.set_config.assert_called_with(
            "bangumi-data", "cache_ttl_days", 14
        )


# ---------------------------------------------------------------------------
# Scenario 4: 修改不存在段 → 400
# ---------------------------------------------------------------------------


class TestUpdateInvalidSection:
    """POST /api/mcp/config/update → 不存在段 → 400 + 合法段名提示。"""

    @pytest.mark.asyncio
    async def test_update_不存在段_返回400(self, app):
        payload = {"nonexistent_section": {"key": "value"}}
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/mcp/config/update", json=payload)
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_update_不存在段_提示合法段名(self, app):
        payload = {"foobar": {"key": "value"}}
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/mcp/config/update", json=payload)
        detail = resp.json()["detail"]
        # 错误提示中应出现至少一个合法段名（如 auth / sync / llm）
        assert "auth" in detail or "sync" in detail or "llm" in detail

    @pytest.mark.asyncio
    async def test_update_不存在段_未调用_set_config(self, app, mock_config_manager):
        payload = {"nonexistent": {"key": "value"}}
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post("/api/mcp/config/update", json=payload)
        mock_config_manager.set_config.assert_not_called()


# ---------------------------------------------------------------------------
# Scenario 5: 敏感字段写入 → 密文落盘，响应不回显明文
# ---------------------------------------------------------------------------


class TestSensitiveFieldWrite:
    """修改敏感字段 → 落盘为密文，响应不回显明文。"""

    @pytest.mark.asyncio
    async def test_update_敏感字段_不回显明文(self, app, mock_config_manager):
        """写入 llm.api_key 后，响应中该字段应为掩码。"""
        payload = {"llm": {"api_key": "my-new-secret"}}
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/mcp/config/update", json=payload)
        assert resp.status_code == 200
        body = resp.json()
        # 响应中不应包含明文
        assert "my-new-secret" not in str(body)

    @pytest.mark.asyncio
    async def test_update_敏感字段_传给_set_config(self, app, mock_config_manager):
        """敏感字段应明文传给 set_config（由 config_manager 自动加密）。"""
        payload = {"llm": {"api_key": "my-new-secret"}}
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post("/api/mcp/config/update", json=payload)
        mock_config_manager.set_config.assert_called_with(
            "llm", "api_key", "my-new-secret"
        )


# ---------------------------------------------------------------------------
# Scenario 6: 鉴权 — 无凭证 401；写操作无 write scope 403
# ---------------------------------------------------------------------------


class TestAuth:
    """无凭证 → 401；写操作无 write scope → 403。"""

    @pytest.mark.asyncio
    async def test_无凭证_读配置_返回401(self):
        from app.api import mcp_config

        app = FastAPI()
        app.include_router(mcp_config.router)
        # 不注入任何依赖覆盖 → 真实鉴权路径 → 无凭证应 401
        with patch.object(mcp_config, "config_manager", MagicMock()):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/api/mcp/config")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_无凭证_写配置_返回401(self):
        from app.api import mcp_config

        app = FastAPI()
        app.include_router(mcp_config.router)
        with patch.object(mcp_config, "config_manager", MagicMock()):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/mcp/config/update", json={"auth": {"x": "y"}}
                )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_无write_scope_写配置_返回403(self, app_read_only):
        payload = {"auth": {"session_timeout": 7200}}
        async with AsyncClient(
            transport=ASGITransport(app=app_read_only), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/mcp/config/update",
                json=payload,
                headers={"Authorization": f"Bearer {app_read_only._test_token}"},
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_无write_scope_读配置_允许(self, app_read_only):
        """读操作不需要 write scope，read 即可。"""
        async with AsyncClient(
            transport=ASGITransport(app=app_read_only), base_url="http://test"
        ) as client:
            resp = await client.get("/api/mcp/config")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Scenario 7: auth 段写入被拒绝（M4）
# ---------------------------------------------------------------------------


class TestUpdateAuthBlocked:
    """auth 段禁止通过 MCP 修改（Web 认证配置应仅通过 Web 界面修改）。"""

    @pytest.mark.asyncio
    async def test_update_auth_enabled_false_被拒(self, app):
        """写入 auth.enabled=false → 403。"""
        payload = {"auth": {"enabled": False}}
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/mcp/config/update", json=payload)
        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert "auth" in detail.lower() or "不可通过 MCP" in detail

    @pytest.mark.asyncio
    async def test_update_auth_username_被拒(self, app):
        """写入 auth.username → 403。"""
        payload = {"auth": {"username": "evil"}}
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/mcp/config/update", json=payload)
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_update_auth_webhook_key_被拒(self, app):
        """写入 auth.webhook_key → 403。"""
        payload = {"auth": {"webhook_key": "stolen-key"}}
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/mcp/config/update", json=payload)
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_update_auth_session_timeout_被拒(self, app):
        """写入 auth 段任意字段 → 403（整个 auth 段禁止）。"""
        payload = {"auth": {"session_timeout": 99999}}
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/mcp/config/update", json=payload)
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_update_non_auth_段_正常(self, app, mock_config_manager):
        """写入非 auth 段（如 sync）→ 正常成功。"""
        payload = {"sync": {"match_confidence_threshold": 0.9}}
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/mcp/config/update", json=payload)
        assert resp.status_code == 200
        mock_config_manager.set_config.assert_called_with(
            "sync", "match_confidence_threshold", 0.9
        )

    @pytest.mark.asyncio
    async def test_update_auth_未调用_set_config(self, app, mock_config_manager):
        """auth 段写入被拒时，不应调用 set_config。"""
        payload = {"auth": {"enabled": False}}
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post("/api/mcp/config/update", json=payload)
        mock_config_manager.set_config.assert_not_called()
