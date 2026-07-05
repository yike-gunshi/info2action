def test_default_from_email_uses_info2act_domain(monkeypatch):
    import importlib
    import utils.email as email

    monkeypatch.delenv("RESEND_FROM_EMAIL", raising=False)
    monkeypatch.delenv("EMAIL_BRAND_NAME", raising=False)
    monkeypatch.delenv("APP_BRAND_NAME", raising=False)

    reloaded = importlib.reload(email)

    assert reloaded.FROM_EMAIL == "info2act <noreply@info2act.com>"


def test_verification_email_uses_info2act_brand(monkeypatch):
    import utils.email as email

    sent = []

    monkeypatch.setattr(email, "RESEND_API_KEY", "test-resend-key")
    monkeypatch.setattr(email, "BRAND_NAME", "info2act")
    monkeypatch.setattr(email, "FROM_EMAIL", "info2act <noreply@info2act.com>")
    monkeypatch.setattr(email.resend.Emails, "send", lambda payload: sent.append(payload))

    assert email.send_verification_code("qa@example.com", "123456", "loadqa") is True

    payload = sent[0]
    assert payload["from"] == "info2act <noreply@info2act.com>"
    assert payload["subject"] == "info2act 邮箱验证码：123456"
    assert payload["html"].startswith("<!doctype html>")
    assert "验证你的邮箱" in payload["html"]
    assert "邮箱验证码" in payload["html"]
    assert "123456" in payload["html"]
    assert "你好 loadqa，" in payload["text"]
    assert "— info2act" in payload["text"]
    assert "OhMyNews" not in payload["html"]
    assert "OhMyNews" not in payload["subject"]
    assert "OhMyNews" not in payload["text"]


def test_password_reset_email_uses_info2act_brand(monkeypatch):
    import utils.email as email

    sent = []

    monkeypatch.setattr(email, "RESEND_API_KEY", "test-resend-key")
    monkeypatch.setattr(email, "BRAND_NAME", "info2act")
    monkeypatch.setattr(email, "FROM_EMAIL", "info2act <noreply@info2act.com>")
    monkeypatch.setattr(email.resend.Emails, "send", lambda payload: sent.append(payload))

    assert email.send_password_reset("qa@example.com", "https://info2act.com/#reset-password?token=t") is True

    payload = sent[0]
    assert payload["from"] == "info2act <noreply@info2act.com>"
    assert payload["subject"] == "info2act 密码重置"
    assert payload["html"].startswith("<!doctype html>")
    assert "重置你的密码" in payload["html"]
    assert "重置密码" in payload["html"]
    assert "https://info2act.com/#reset-password?token=t" in payload["html"]
    assert "https://info2act.com/#reset-password?token=t" in payload["text"]
    assert "— info2act" in payload["text"]
    assert "OhMyNews" not in payload["html"]
    assert "OhMyNews" not in payload["subject"]
    assert "OhMyNews" not in payload["text"]
