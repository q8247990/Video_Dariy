from src.core.security import create_access_token, get_password_hash, verify_password


def test_password_hashing():
    password = "test_password123"
    hashed = get_password_hash(password)
    assert hashed != password
    assert verify_password(password, hashed) is True
    assert verify_password("wrong_password", hashed) is False


def test_create_access_token():
    user_id = 1
    token = create_access_token(subject=str(user_id))
    assert isinstance(token, str)
    assert len(token) > 0
