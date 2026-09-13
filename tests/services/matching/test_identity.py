"""业务键（identity）测试

验证 build_match_business_key 的归一化与键生成逻辑：
- 大小写/空白/噪声差异的同名剧集映射同一键
- season 缺失按 1
- 归一化结果为空时退回 str(title).strip().lower()
- 键格式：match|{user_name}|{normalize(title)}|{season}
"""

from app.services.matching.identity import build_match_business_key


class TestBuildMatchBusinessKey:
    """build_match_business_key 行为验证"""

    def test_basic_key_format(self):
        """基本键格式：match|user|title|season"""
        key = build_match_business_key("alice", "测试番剧", 1)
        assert key == "match|alice|测试番剧|1"

    def test_case_insensitive(self):
        """标题大小写差异映射同一键（user_name 不做归一化）"""
        k1 = build_match_business_key("alice", "Test Anime", 1)
        k2 = build_match_business_key("alice", "test anime", 1)
        assert k1 == k2

    def test_whitespace_normalized(self):
        """首尾空白差异映射同一键"""
        k1 = build_match_business_key("alice", "  Test Anime  ", 1)
        k2 = build_match_business_key("alice", "Test Anime", 1)
        assert k1 == k2

    def test_noise_patterns_stripped(self):
        """噪声标记（分辨率/编码/发布组）差异映射同一键"""
        k1 = build_match_business_key("alice", "Test Anime [1080p]", 1)
        k2 = build_match_business_key("alice", "Test Anime", 1)
        assert k1 == k2

    def test_season_missing_defaults_to_1(self):
        """season 缺失（None/0）按 1 处理"""
        k1 = build_match_business_key("alice", "Test Anime", None)
        k2 = build_match_business_key("alice", "Test Anime", 0)
        k3 = build_match_business_key("alice", "Test Anime", 1)
        assert k1 == k2 == k3

    def test_season_preserved_when_provided(self):
        """season 有值时保留"""
        k = build_match_business_key("alice", "Test Anime", 2)
        assert k.endswith("|2")

    def test_empty_title_fallback(self):
        """归一化结果为空时退回 str(title).strip().lower()"""
        # 纯噪声标题归一化后为空
        key = build_match_business_key("alice", "[1080p]", 1)
        # 退回 str("[1080p]").strip().lower()
        assert key == "match|alice|[1080p]|1"

    def test_empty_string_title(self):
        """空字符串标题退回空"""
        key = build_match_business_key("alice", "", 1)
        assert key == "match|alice||1"

    def test_user_name_case_preserved_as_is(self):
        """user_name 不做归一化（保持原样）"""
        k = build_match_business_key("Alice", "Test", 1)
        assert k.startswith("match|Alice|")

    def test_different_users_different_keys(self):
        """不同用户同一标题不同键"""
        k1 = build_match_business_key("alice", "Test Anime", 1)
        k2 = build_match_business_key("bob", "Test Anime", 1)
        assert k1 != k2

    def test_different_seasons_different_keys(self):
        """同一用户同一标题不同季不同键"""
        k1 = build_match_business_key("alice", "Test Anime", 1)
        k2 = build_match_business_key("alice", "Test Anime", 2)
        assert k1 != k2

    def test_chinese_punctuation_normalized(self):
        """中文标点归一化后映射同一键"""
        k1 = build_match_business_key("alice", "测试：番剧", 1)
        k2 = build_match_business_key("alice", "测试:番剧", 1)
        assert k1 == k2

    def test_season_string_coerced(self):
        """season 为字符串时也能正确处理"""
        k = build_match_business_key("alice", "Test", "2")
        assert k.endswith("|2")
