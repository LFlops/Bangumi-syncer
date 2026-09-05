"""
MCP 内部 API 日志端点（/api/mcp/logs）测试
"""

from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import mcp_logs


def _override_mcp_auth(app):
    """为路由器中所有 get_mcp_client() 生成的 _dependency 覆盖假用户。"""
    for route in mcp_logs.router.routes:
        if hasattr(route, "dependant"):
            for dep in route.dependant.dependencies:
                if dep.call.__name__ == "_dependency":

                    async def mock_mcp_client():
                        return {
                            "username": "test_mcp",
                            "scope": ["read"],
                            "mcp": True,
                        }

                    app.dependency_overrides[dep.call] = mock_mcp_client


@pytest.fixture
def app_with_mcp_auth():
    """创建带有 MCP 认证覆盖的测试应用"""
    app = FastAPI()
    app.include_router(mcp_logs.router)
    _override_mcp_auth(app)
    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def app_no_auth():
    """创建不带认证覆盖的测试应用（用于测试 401 场景）"""
    app = FastAPI()
    app.include_router(mcp_logs.router)
    yield app


# 与项目日志格式一致的时间戳行
SAMPLE_LOG = (
    "[2026/09/03 10:00:00.000] [INFO] [run:sync_1_100] 开始同步\n"
    "[2026/09/03 12:00:00.000] [ERROR] [run:sync_1_100] 数据库连接失败\n"
    "[2026/09/03 14:00:00.000] [WARNING] [run:sync_1_100] 重试第 1 次\n"
    "[2026/09/03 16:00:00.000] [INFO] [run:sync_1_100] 同步结束: status=success\n"
)


# ---------------------------------------------------------------------------
# Scenario 1: 查询日志（level 过滤）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_mcp_logs_level_filter_only_error(app_with_mcp_auth):
    """GET /api/mcp/logs?level=ERROR → 仅返回 ERROR 级日志"""
    with patch(
        "app.api.mcp_logs.resolved_dev_log_file_path",
        return_value=Path("/fake/app.log"),
    ):
        with patch("app.api.mcp_logs.os.path.exists", return_value=True):
            with patch("app.api.mcp_logs.os.stat") as mock_stat:
                mock_stat.return_value = MagicMock(
                    st_size=len(SAMPLE_LOG), st_mtime=1234567890.0
                )
                with patch("builtins.open", mock_open(read_data=SAMPLE_LOG)):
                    async with AsyncClient(
                        transport=ASGITransport(app=app_with_mcp_auth),
                        base_url="http://test",
                    ) as client:
                        response = await client.get("/api/mcp/logs?level=ERROR")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert "content" in data["data"]
    assert "stats" in data["data"]
    assert "ERROR" in data["data"]["content"]
    assert "INFO" not in data["data"]["content"]
    assert "WARNING" not in data["data"]["content"]


@pytest.mark.asyncio
async def test_get_mcp_logs_default_limit_50(app_with_mcp_auth):
    """GET /api/mcp/logs 无参数 → 返回 success 包络，content 存在"""
    with patch(
        "app.api.mcp_logs.resolved_dev_log_file_path",
        return_value=Path("/fake/app.log"),
    ):
        with patch("app.api.mcp_logs.os.path.exists", return_value=True):
            with patch("app.api.mcp_logs.os.stat") as mock_stat:
                mock_stat.return_value = MagicMock(st_size=100, st_mtime=1234567890.0)
                with patch("builtins.open", mock_open(read_data=SAMPLE_LOG)):
                    async with AsyncClient(
                        transport=ASGITransport(app=app_with_mcp_auth),
                        base_url="http://test",
                    ) as client:
                        response = await client.get("/api/mcp/logs")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert "content" in data["data"]


# ---------------------------------------------------------------------------
# Scenario 2: 时间范围过滤
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_mcp_logs_time_range_filter(app_with_mcp_auth):
    """since/until 仅返回时间窗口内的日志行"""
    with patch(
        "app.api.mcp_logs.resolved_dev_log_file_path",
        return_value=Path("/fake/app.log"),
    ):
        with patch("app.api.mcp_logs.os.path.exists", return_value=True):
            with patch("app.api.mcp_logs.os.stat") as mock_stat:
                mock_stat.return_value = MagicMock(
                    st_size=len(SAMPLE_LOG), st_mtime=1234567890.0
                )
                with patch("builtins.open", mock_open(read_data=SAMPLE_LOG)):
                    async with AsyncClient(
                        transport=ASGITransport(app=app_with_mcp_auth),
                        base_url="http://test",
                    ) as client:
                        response = await client.get(
                            "/api/mcp/logs",
                            params={
                                "since": "2026-09-03T11:00:00",
                                "until": "2026-09-03T13:00:00",
                            },
                        )

    assert response.status_code == 200
    data = response.json()
    content = data["data"]["content"]
    # 应只包含 12:00 的 ERROR 行
    assert "数据库连接失败" in content
    assert "开始同步" not in content
    assert "重试第 1 次" not in content
    assert "同步结束" not in content


@pytest.mark.asyncio
async def test_get_mcp_logs_since_only(app_with_mcp_auth):
    """仅 since → 返回该时间点之后的日志"""
    with patch(
        "app.api.mcp_logs.resolved_dev_log_file_path",
        return_value=Path("/fake/app.log"),
    ):
        with patch("app.api.mcp_logs.os.path.exists", return_value=True):
            with patch("app.api.mcp_logs.os.stat") as mock_stat:
                mock_stat.return_value = MagicMock(
                    st_size=len(SAMPLE_LOG), st_mtime=1234567890.0
                )
                with patch("builtins.open", mock_open(read_data=SAMPLE_LOG)):
                    async with AsyncClient(
                        transport=ASGITransport(app=app_with_mcp_auth),
                        base_url="http://test",
                    ) as client:
                        response = await client.get(
                            "/api/mcp/logs",
                            params={"since": "2026-09-03T13:00:00"},
                        )

    assert response.status_code == 200
    data = response.json()
    content = data["data"]["content"]
    assert "重试第 1 次" in content
    assert "同步结束" in content
    assert "开始同步" not in content
    assert "数据库连接失败" not in content


# ---------------------------------------------------------------------------
# Scenario 3: 非法级别 → 400，不抛 500
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_mcp_logs_invalid_level_returns_400(app_with_mcp_auth):
    """level=DEBUG2 → 400，不抛 500"""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_mcp_auth), base_url="http://test"
    ) as client:
        response = await client.get("/api/mcp/logs?level=DEBUG2")

    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Scenario 4: 非法时间格式 → 400
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_mcp_logs_invalid_since_returns_400(app_with_mcp_auth):
    """since=abc → 明确 400 错误"""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_mcp_auth), base_url="http://test"
    ) as client:
        response = await client.get("/api/mcp/logs?since=abc")

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_get_mcp_logs_invalid_until_returns_400(app_with_mcp_auth):
    """until=not-a-date → 明确 400 错误"""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_mcp_auth), base_url="http://test"
    ) as client:
        response = await client.get("/api/mcp/logs?until=not-a-date")

    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Scenario 5: 无凭证 → 401
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_mcp_logs_no_credentials_returns_401(app_no_auth):
    """无 Authorization 头 → 401"""
    async with AsyncClient(
        transport=ASGITransport(app=app_no_auth), base_url="http://test"
    ) as client:
        response = await client.get("/api/mcp/logs")

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Scenario 6: 日志文件不存在/未配置 → 返回空 data 而非 500
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_mcp_logs_file_not_configured(app_with_mcp_auth):
    """日志文件未配置（resolved_dev_log_file_path 返回 None）→ 空 content"""
    with patch(
        "app.api.mcp_logs.resolved_dev_log_file_path",
        return_value=None,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app_with_mcp_auth), base_url="http://test"
        ) as client:
            response = await client.get("/api/mcp/logs")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert data["data"]["content"] == ""
    assert data["data"]["stats"]["size"] == 0


@pytest.mark.asyncio
async def test_get_mcp_logs_file_not_found(app_with_mcp_auth):
    """日志文件不存在 → 空 content 而非 500"""
    with patch(
        "app.api.mcp_logs.resolved_dev_log_file_path",
        return_value=Path("/nonexistent/log.txt"),
    ):
        with patch("app.api.mcp_logs.os.path.exists", return_value=False):
            async with AsyncClient(
                transport=ASGITransport(app=app_with_mcp_auth),
                base_url="http://test",
            ) as client:
                response = await client.get("/api/mcp/logs")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert data["data"]["content"] == ""
    assert data["data"]["stats"]["size"] == 0


# ---------------------------------------------------------------------------
# 路由元数据
# ---------------------------------------------------------------------------


def test_mcp_logs_router_prefix():
    assert mcp_logs.router.prefix == "/api/mcp"


def test_mcp_logs_router_tags():
    assert "mcp" in mcp_logs.router.tags


# ---------------------------------------------------------------------------
# Scenario 7: 异常回显不泄露路径（M6）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_mcp_logs_since_带Z后缀_正常解析(app_with_mcp_auth):
    """since=2026-09-03T10:00:00Z 应正常解析，不返回 400。"""
    with patch(
        "app.api.mcp_logs.resolved_dev_log_file_path",
        return_value=Path("/fake/app.log"),
    ):
        with patch("app.api.mcp_logs.os.path.exists", return_value=True):
            with patch("app.api.mcp_logs.os.stat") as mock_stat:
                mock_stat.return_value = MagicMock(st_size=100, st_mtime=1234567890.0)
                with patch("builtins.open", mock_open(read_data="")):
                    async with AsyncClient(
                        transport=ASGITransport(app=app_with_mcp_auth),
                        base_url="http://test",
                    ) as client:
                        response = await client.get(
                            "/api/mcp/logs",
                            params={"since": "2026-09-03T10:00:00Z"},
                        )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_get_mcp_logs_since_带偏移量_正常解析(app_with_mcp_auth):
    """since=2026-09-03T10:00:00+08:00 应正常解析，不返回 400。"""
    with patch(
        "app.api.mcp_logs.resolved_dev_log_file_path",
        return_value=Path("/fake/app.log"),
    ):
        with patch("app.api.mcp_logs.os.path.exists", return_value=True):
            with patch("app.api.mcp_logs.os.stat") as mock_stat:
                mock_stat.return_value = MagicMock(st_size=100, st_mtime=1234567890.0)
                with patch("builtins.open", mock_open(read_data="")):
                    async with AsyncClient(
                        transport=ASGITransport(app=app_with_mcp_auth),
                        base_url="http://test",
                    ) as client:
                        response = await client.get(
                            "/api/mcp/logs",
                            params={"since": "2026-09-03T10:00:00+08:00"},
                        )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_get_mcp_logs_until_带Z后缀_正常解析(app_with_mcp_auth):
    """until=2026-09-03T10:00:00Z 应正常解析，不返回 400。"""
    with patch(
        "app.api.mcp_logs.resolved_dev_log_file_path",
        return_value=Path("/fake/app.log"),
    ):
        with patch("app.api.mcp_logs.os.path.exists", return_value=True):
            with patch("app.api.mcp_logs.os.stat") as mock_stat:
                mock_stat.return_value = MagicMock(st_size=100, st_mtime=1234567890.0)
                with patch("builtins.open", mock_open(read_data="")):
                    async with AsyncClient(
                        transport=ASGITransport(app=app_with_mcp_auth),
                        base_url="http://test",
                    ) as client:
                        response = await client.get(
                            "/api/mcp/logs",
                            params={"until": "2026-09-03T10:00:00Z"},
                        )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_get_mcp_logs_since_大于_until_返回400(app_with_mcp_auth):
    """since 晚于 until → 明确 400 错误，而非静默返回空。"""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_mcp_auth), base_url="http://test"
    ) as client:
        response = await client.get(
            "/api/mcp/logs",
            params={
                "since": "2026-09-03T15:00:00",
                "until": "2026-09-03T10:00:00",
            },
        )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "since" in detail.lower() or "until" in detail.lower() or "顺序" in detail


@pytest.mark.asyncio
async def test_get_mcp_logs_异常_不泄露路径(app_with_mcp_auth):
    """日志读取异常时，detail 不应包含绝对路径或具体异常信息。"""
    with patch(
        "app.api.mcp_logs.resolved_dev_log_file_path",
        return_value=Path("/secret/path/app.log"),
    ):
        # 模拟 _read_log_file 抛出含路径的异常
        with patch(
            "app.api.mcp_logs.asyncio.to_thread",
            side_effect=Exception("/secret/path/app.log: permission denied"),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app_with_mcp_auth),
                base_url="http://test",
            ) as client:
                response = await client.get("/api/mcp/logs")

    assert response.status_code == 500
    detail = response.json()["detail"]
    # 不应泄露绝对路径
    assert "/secret/path" not in detail
    # 不应泄露具体异常信息
    assert "permission denied" not in detail


# ---------------------------------------------------------------------------
# Scenario 8: since/until 带时区与朴素日志时间戳比较不崩溃（M9）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_mcp_logs_since_带Z后缀_非空日志_正常过滤(app_with_mcp_auth):
    """since=2026-01-01T00:00:00Z 且日志非空时，不抛 TypeError，正常过滤。"""
    with patch(
        "app.api.mcp_logs.resolved_dev_log_file_path",
        return_value=Path("/fake/app.log"),
    ):
        with patch("app.api.mcp_logs.os.path.exists", return_value=True):
            with patch("app.api.mcp_logs.os.stat") as mock_stat:
                mock_stat.return_value = MagicMock(
                    st_size=len(SAMPLE_LOG), st_mtime=1234567890.0
                )
                with patch("builtins.open", mock_open(read_data=SAMPLE_LOG)):
                    async with AsyncClient(
                        transport=ASGITransport(app=app_with_mcp_auth),
                        base_url="http://test",
                    ) as client:
                        response = await client.get(
                            "/api/mcp/logs",
                            params={"since": "2026-01-01T00:00:00Z"},
                        )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    # 2026-01-01 早于所有日志行（2026-09-03），应返回全部日志
    assert "开始同步" in data["data"]["content"]
    assert "同步结束" in data["data"]["content"]


@pytest.mark.asyncio
async def test_get_mcp_logs_since_带偏移量_非空日志_正常过滤(app_with_mcp_auth):
    """since=2026-09-03T11:00:00+08:00 且日志非空时，不抛 TypeError，正常过滤。"""
    with patch(
        "app.api.mcp_logs.resolved_dev_log_file_path",
        return_value=Path("/fake/app.log"),
    ):
        with patch("app.api.mcp_logs.os.path.exists", return_value=True):
            with patch("app.api.mcp_logs.os.stat") as mock_stat:
                mock_stat.return_value = MagicMock(
                    st_size=len(SAMPLE_LOG), st_mtime=1234567890.0
                )
                with patch("builtins.open", mock_open(read_data=SAMPLE_LOG)):
                    async with AsyncClient(
                        transport=ASGITransport(app=app_with_mcp_auth),
                        base_url="http://test",
                    ) as client:
                        response = await client.get(
                            "/api/mcp/logs",
                            params={"since": "2026-09-03T11:00:00+08:00"},
                        )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    # +08:00 11:00 = UTC 03:00，早于所有日志行（UTC 02:00-08:00 对应 10:00-16:00+08:00）
    # 实际上日志时间是 naive（服务器本地时间），since 也被转为 naive
    # 2026-09-03T11:00:00+08:00 → naive 2026-09-03T11:00:00
    # 应过滤掉 10:00 的行
    assert "开始同步" not in data["data"]["content"]
    assert "数据库连接失败" in data["data"]["content"]


@pytest.mark.asyncio
async def test_get_mcp_logs_until_带Z后缀_非空日志_正常过滤(app_with_mcp_auth):
    """until=2026-09-03T13:00:00Z 且日志非空时，不抛 TypeError，正常过滤。"""
    with patch(
        "app.api.mcp_logs.resolved_dev_log_file_path",
        return_value=Path("/fake/app.log"),
    ):
        with patch("app.api.mcp_logs.os.path.exists", return_value=True):
            with patch("app.api.mcp_logs.os.stat") as mock_stat:
                mock_stat.return_value = MagicMock(
                    st_size=len(SAMPLE_LOG), st_mtime=1234567890.0
                )
                with patch("builtins.open", mock_open(read_data=SAMPLE_LOG)):
                    async with AsyncClient(
                        transport=ASGITransport(app=app_with_mcp_auth),
                        base_url="http://test",
                    ) as client:
                        response = await client.get(
                            "/api/mcp/logs",
                            params={"until": "2026-09-03T13:00:00Z"},
                        )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    # 13:00Z = 13:00 naive（按服务器本地时间对齐）
    # 应包含 10:00 和 12:00 的行
    assert "开始同步" in data["data"]["content"]
    assert "数据库连接失败" in data["data"]["content"]
    # 14:00 和 16:00 的行应被过滤
    assert "重试第 1 次" not in data["data"]["content"]
    assert "同步结束" not in data["data"]["content"]
