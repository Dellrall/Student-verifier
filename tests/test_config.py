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


def test_validate_student_id_invalid():
    # Invalid length/format
    is_valid, _, _, _ = validate_student_id("invalid_id")
    assert is_valid is False

    # Unknown faculty code
    is_valid, _, f_code, role = validate_student_id("23WZD09867")
    assert is_valid is False
    assert f_code == "Z"
    assert role is None
