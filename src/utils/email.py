"""Email sending via Resend API."""
from html import escape
import os
import resend

RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '')
BRAND_NAME = os.environ.get('EMAIL_BRAND_NAME') or os.environ.get('APP_BRAND_NAME') or 'info2act'
FROM_EMAIL = os.environ.get('RESEND_FROM_EMAIL', f'{BRAND_NAME} <noreply@info2act.com>')


def _email_shell(title: str, greeting: str, body_html: str, footer_note: str) -> str:
    """Return conservative table-free HTML that renders well in Gmail."""
    safe_brand = escape(BRAND_NAME)
    return f"""<!doctype html>
<html>
  <body style="margin:0;padding:0;background:#f7f5ef;color:#222;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;">
    <div style="max-width:560px;margin:0 auto;padding:32px 18px;">
      <div style="font-size:22px;font-weight:700;letter-spacing:0;margin-bottom:18px;">{safe_brand}</div>
      <div style="background:#fff;border:1px solid #e5dfd3;border-radius:14px;padding:28px;box-shadow:0 10px 30px rgba(32,28,20,0.06);">
        <h1 style="margin:0 0 18px;font-size:22px;line-height:1.35;font-weight:700;color:#201c14;">{title}</h1>
        <p style="margin:0 0 18px;font-size:16px;line-height:1.7;color:#3a352c;">{greeting}</p>
        {body_html}
        <p style="margin:24px 0 0;font-size:13px;line-height:1.7;color:#817866;">{footer_note}</p>
      </div>
      <div style="margin-top:18px;font-size:12px;line-height:1.6;color:#9a927f;">— {safe_brand}</div>
    </div>
  </body>
</html>"""


def _verification_html(code: str, username: str = '') -> str:
    greeting = f"你好 {escape(username)}，" if username else "你好，"
    body = (
        f'<div style="margin:22px 0 20px;padding:18px 20px;background:#f0eee7;'
        f'border:1px solid #ded6c8;border-radius:12px;text-align:center;">'
        f'<div style="font-size:12px;line-height:1.4;color:#817866;margin-bottom:8px;">邮箱验证码</div>'
        f'<div style="font-size:34px;line-height:1;font-weight:800;letter-spacing:8px;color:#201c14;">{escape(code)}</div>'
        f'</div>'
        f'<p style="margin:0;font-size:15px;line-height:1.7;color:#3a352c;">请在 10 分钟内完成验证。'
        f'如果不是你本人操作，可以安全忽略这封邮件。</p>'
    )
    return _email_shell(
        title="验证你的邮箱",
        greeting=greeting,
        body_html=body,
        footer_note="这封邮件用于确认你的 info2act 账号邮箱。",
    )


def _password_reset_html(reset_url: str, username: str = '') -> str:
    safe_url = escape(reset_url, quote=True)
    greeting = f"你好 {escape(username)}，" if username else "你好，"
    body = (
        f'<p style="margin:0 0 22px;font-size:15px;line-height:1.7;color:#3a352c;">'
        f'我们收到了你的密码重置请求。点击下方按钮继续：</p>'
        f'<p style="margin:0 0 22px;">'
        f'<a href="{safe_url}" style="display:inline-block;background:#201c14;color:#fff;'
        f'text-decoration:none;border-radius:10px;padding:12px 18px;font-size:15px;font-weight:700;">重置密码</a>'
        f'</p>'
        f'<p style="margin:0;font-size:13px;line-height:1.7;color:#817866;word-break:break-all;">'
        f'如果按钮无法打开，请复制这个链接：<br>{safe_url}</p>'
    )
    return _email_shell(
        title="重置你的密码",
        greeting=greeting,
        body_html=body,
        footer_note="链接 30 分钟内有效。如果不是你本人操作，请忽略这封邮件。",
    )


def send_verification_code(to_email: str, code: str, username: str = '') -> bool:
    """Send a 6-digit verification code email. Returns True on success."""
    if not RESEND_API_KEY:
        print(f"[email] RESEND_API_KEY not set, code for {to_email}: {code}")
        return True  # Dev mode: print to console

    resend.api_key = RESEND_API_KEY

    greeting = f"你好 {username}，" if username else "你好，"

    try:
        resend.Emails.send({
            "from": FROM_EMAIL,
            "to": [to_email],
            "subject": f"{BRAND_NAME} 邮箱验证码：{code}",
            "html": _verification_html(code, username),
            "text": (
                f"{greeting}\n\n"
                f"你的邮箱验证码是：{code}\n\n"
                f"验证码 10 分钟内有效。如果不是你本人操作，请忽略此邮件。\n\n"
                f"— {BRAND_NAME}"
            ),
        })
        return True
    except Exception as e:
        print(f"[email] Failed to send to {to_email}: {e}")
        return False


def send_password_reset(to_email: str, reset_url: str, username: str = '') -> bool:
    """Send a password reset link email. Returns True on success."""
    if not RESEND_API_KEY:
        print(f"[email] RESEND_API_KEY not set, reset link for {to_email}: {reset_url}")
        return True  # Dev mode: print to console

    resend.api_key = RESEND_API_KEY

    greeting = f"你好 {username}，" if username else "你好，"

    try:
        resend.Emails.send({
            "from": FROM_EMAIL,
            "to": [to_email],
            "subject": f"{BRAND_NAME} 密码重置",
            "html": _password_reset_html(reset_url, username),
            "text": (
                f"{greeting}\n\n"
                f"我们收到了你的密码重置请求。请点击以下链接重置密码：\n\n"
                f"{reset_url}\n\n"
                f"链接 30 分钟内有效。如果不是你本人操作，请忽略此邮件。\n\n"
                f"— {BRAND_NAME}"
            ),
        })
        return True
    except Exception as e:
        print(f"[email] Failed to send reset to {to_email}: {e}")
        return False
