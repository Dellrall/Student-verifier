from tarveri.config import (
    FACULTY_ROLES,
    hash_student_id,
    mask_student_id,
    validate_student_id,
)


def test_hash_student_id():
    secret = "test_secret_key_12345"
    student_id = "23WMD09867"
    hash1 = hash_student_id(student_id, secret)
    hash2 = hash_student_id(student_id, secret)
    assert hash1 == hash2
    assert len(hash1) == 64

    # Different secret or ID produces different hash
    assert hash_student_id("23WMD09868", secret) != hash1
    assert hash_student_id(student_id, "different_secret") != hash1


def test_mask_student_id():
    assert mask_student_id("23WMD09867") == "23***867"
    assert mask_student_id("123456") == "12***456"
    assert mask_student_id("123") == "***"


def test_validate_student_id_valid():
    # Valid FOCS (M)
    is_valid, norm_id, f_code, role = validate_student_id("  23wmd09867  ")
    assert is_valid is True
    assert norm_id == "23WMD09867"
    assert f_code == "M"
    assert role == "FOCS"

    # Valid FOET (G)
    is_valid, norm_id, f_code, role = validate_student_id("22WGD12345")
    assert is_valid is True
    assert norm_id == "22WGD12345"
    assert f_code == "G"
    assert role == "FOET"

    # Valid CPUS (P)
    is_valid, norm_id, f_code, role = validate_student_id("24WPF00001")
    assert is_valid is True
    assert norm_id == "24WPF00001"
    assert f_code == "P"
    assert role == "CPUS"



def test_validate_student_id_invalid():
    # Invalid length/format
    is_valid, _, _, _ = validate_student_id("invalid_id")
    assert is_valid is False

    # Unknown faculty code
    is_valid, _, f_code, role = validate_student_id("23WZD09867")
    assert is_valid is False
    assert f_code == "Z"
    assert role is None


def test_settings_from_env(monkeypatch):
    from tarveri.config import Settings
    monkeypatch.setenv("TARVERI_BOT_TOKEN", "mock_token")
    monkeypatch.setenv("TARVERI_ID_HASH_SECRET", "mock_secret")
    monkeypatch.setenv("TARVERI_UPDATE_STREAM", "refactor/modular-optimization")
    monkeypatch.setenv("TARVERI_HELP_CHANNEL_ID", "1122334455")
    monkeypatch.setenv("TARVERI_WELCOME_CHANNEL_ID", "6677889900")

    settings = Settings.from_env()
    assert settings.bot_token == "mock_token"
    assert settings.id_hash_secret == "mock_secret"
    assert settings.update_stream == "refactor/modular-optimization"
    assert settings.help_channel_id == 1122334455
    assert settings.welcome_channel_id == 6677889900


def test_role_help_keywords_pattern():
    from tarveri.config import ROLE_HELP_KEYWORDS_PATTERN

    # Matching cases
    assert ROLE_HELP_KEYWORDS_PATTERN.search("How to get role?") is not None
    assert ROLE_HELP_KEYWORDS_PATTERN.search("how do i get a role") is not None
    assert ROLE_HELP_KEYWORDS_PATTERN.search("Where to verify") is not None
    assert ROLE_HELP_KEYWORDS_PATTERN.search("how to verify?") is not None
    assert ROLE_HELP_KEYWORDS_PATTERN.search("I need role please") is not None
    assert ROLE_HELP_KEYWORDS_PATTERN.search("can you give role") is not None
    assert ROLE_HELP_KEYWORDS_PATTERN.search("i have no role") is not None
    assert ROLE_HELP_KEYWORDS_PATTERN.search("claim role") is not None
    assert ROLE_HELP_KEYWORDS_PATTERN.search("faculty role") is not None
    assert ROLE_HELP_KEYWORDS_PATTERN.search("what is my role") is not None
    assert ROLE_HELP_KEYWORDS_PATTERN.search("roles") is not None

    # Non-matching cases
    assert ROLE_HELP_KEYWORDS_PATTERN.search("hello everyone") is None
    assert ROLE_HELP_KEYWORDS_PATTERN.search("good morning") is None

