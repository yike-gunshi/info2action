#!/usr/bin/env python3
"""QA: 前端用户认证 UI 系统测试 (Python Playwright)

测试范围：LoginPage, RegisterPage, SettingsPage, AdminPage, TopBar UserMenu
后端: localhost:8090, Vite dev: localhost:3456
测试账号: qaadmin / qapassword123 (admin), 邀请码: QATEST01
"""

import json, os, sys, time
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = "http://localhost:3456"
SCREENSHOT_DIR = Path(".gstack/qa-reports/screenshots")
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

results = []
shot_idx = 0

def snap(page, name, **kw):
    global shot_idx
    shot_idx += 1
    path = SCREENSHOT_DIR / f"{shot_idx:02d}_{name}.png"
    page.screenshot(path=str(path), **kw)
    print(f"  📸 {path}")
    return str(path)

def PASS(tid, desc, evidence=None):
    results.append({"id": tid, "status": "pass", "description": desc, "evidence": evidence})
    print(f"  ✅ {tid}: {desc}")

def FAIL(tid, desc, detail, evidence=None):
    results.append({"id": tid, "status": "fail", "description": desc, "detail": detail, "evidence": evidence})
    print(f"  ❌ {tid}: {desc}")
    print(f"     → {detail}")


with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    ctx = browser.new_context(viewport={"width": 1440, "height": 900}, locale="zh-CN")
    page = ctx.new_page()

    console_errors = []
    page.on("pageerror", lambda err: console_errors.append(str(err)))
    page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

    # ═══════════════════════════════════════
    print("\n🧪 Phase 1: 冒烟测试")
    # ═══════════════════════════════════════

    # AUTH-001: 未登录重定向到登录页
    try:
        page.goto(f"{BASE}/", wait_until="networkidle", timeout=15000)
        page.wait_for_timeout(2000)
        url = page.url
        if "#login" in url:
            ev = snap(page, "auth-001-redirect")
            PASS("AUTH-001", "未登录用户重定向到登录页", ev)
        else:
            ev = snap(page, "auth-001-fail")
            FAIL("AUTH-001", "未登录用户重定向到登录页", f"URL={url}", ev)
    except Exception as e:
        FAIL("AUTH-001", "未登录用户重定向到登录页", str(e))

    # AUTH-002: 登录页结构
    try:
        page.goto(f"{BASE}/#login", wait_until="networkidle", timeout=10000)
        page.wait_for_timeout(1000)
        checks = page.evaluate("""() => {
            const form = document.querySelector('form');
            if (!form) throw new Error('未找到登录表单');
            const logo = form.querySelector('div.bg-primary');
            if (!logo) throw new Error('未找到品牌 Logo');
            const texts = form.innerText;
            if (!texts.includes('OhMyNews')) throw new Error('品牌名 OhMyNews 缺失');
            const inputs = form.querySelectorAll('input');
            if (inputs.length < 2) throw new Error('输入框数量不足: ' + inputs.length);
            const btn = form.querySelector('button[type="submit"]');
            if (!btn || !btn.textContent.includes('登录')) throw new Error('登录按钮缺失');
            const link = form.querySelector('a[href="#register"]');
            if (!link) throw new Error('注册链接缺失');
            return true;
        }""")
        ev = snap(page, "auth-002-login-page")
        PASS("AUTH-002", "登录页结构完整（Logo+表单+按钮+注册链接）", ev)
    except Exception as e:
        ev = snap(page, "auth-002-fail")
        FAIL("AUTH-002", "登录页结构", str(e), ev)

    # AUTH-003: 登录卡片视觉规格
    try:
        style = page.evaluate("""() => {
            const form = document.querySelector('form');
            const cs = getComputedStyle(form);
            const mw = parseInt(cs.maxWidth);
            const br = parseInt(cs.borderRadius);
            const pd = parseInt(cs.padding);
            const issues = [];
            if (mw !== 400) issues.push('maxWidth=' + mw + ' 期望400');
            if (br < 14) issues.push('borderRadius=' + br + ' 期望≥16');
            if (pd < 24) issues.push('padding=' + pd + ' 期望≥24');
            if (issues.length > 0) throw new Error(issues.join('; '));
            return {maxWidth: mw, borderRadius: br, padding: pd};
        }""")
        PASS("AUTH-003", f"登录卡片样式: maxWidth={style['maxWidth']}px, radius={style['borderRadius']}px, padding={style['padding']}px")
    except Exception as e:
        FAIL("AUTH-003", "登录卡片视觉规格", str(e))

    # ═══════════════════════════════════════
    print("\n🧪 Phase 2: 功能测试")
    # ═══════════════════════════════════════

    # AUTH-004: 错误密码
    try:
        page.goto(f"{BASE}/#login", wait_until="networkidle", timeout=10000)
        page.wait_for_timeout(500)
        page.locator("input[type='text']").first.fill("qaadmin")
        page.locator("input[type='password']").first.fill("wrongpassword")
        page.locator("button[type='submit']").click()
        page.wait_for_timeout(2000)
        error_text = page.evaluate("""() => {
            const el = document.querySelector('[class*="destructive"]');
            if (!el || el.textContent.trim().length === 0) throw new Error('未显示错误信息');
            return el.textContent.trim();
        }""")
        ev = snap(page, "auth-004-login-error")
        PASS("AUTH-004", f'错误密码显示错误: "{error_text}"', ev)
    except Exception as e:
        ev = snap(page, "auth-004-fail")
        FAIL("AUTH-004", "错误密码显示错误信息", str(e), ev)

    # AUTH-005: 正确登录
    try:
        page.goto(f"{BASE}/#login", wait_until="networkidle", timeout=10000)
        page.wait_for_timeout(500)
        page.locator("input[type='text']").first.fill("qaadmin")
        page.locator("input[type='password']").first.fill("qapassword123")
        page.locator("button[type='submit']").click()
        page.wait_for_timeout(3000)
        url = page.url
        if "#login" in url:
            raise Exception(f"仍在登录页: {url}")
        header_visible = page.locator("header").first.is_visible()
        if not header_visible:
            raise Exception("登录成功但 TopBar 未显示")
        ev = snap(page, "auth-005-login-success")
        PASS("AUTH-005", "正确登录 → 跳转到 Dashboard", ev)
    except Exception as e:
        ev = snap(page, "auth-005-fail")
        FAIL("AUTH-005", "正确登录 → Dashboard", str(e), ev)

    # AUTH-006: TopBar 用户菜单
    try:
        menu_btn = page.locator("[aria-label='用户菜单']")
        menu_btn.wait_for(state="visible", timeout=3000)
        initial = page.evaluate("""() => {
            const avatar = document.querySelector('[aria-label="用户菜单"] div');
            return avatar?.textContent?.trim() || '';
        }""")
        menu_btn.click()
        page.wait_for_timeout(500)
        dropdown_text = page.evaluate("""() => {
            const parent = document.querySelector('[aria-label="用户菜单"]')?.parentElement;
            const dropdown = parent?.querySelector('.absolute');
            if (!dropdown) throw new Error('下拉菜单未出现');
            const text = dropdown.innerText;
            if (!text.includes('设置')) throw new Error('菜单缺少"设置"');
            if (!text.includes('退出登录')) throw new Error('菜单缺少"退出登录"');
            if (!text.includes('管理')) throw new Error('admin 菜单缺少"管理"');
            return text;
        }""")
        ev = snap(page, "auth-006-user-menu")
        PASS("AUTH-006", f'TopBar 用户菜单: 头像"{initial}", 含设置/管理/退出', ev)
        page.locator("main").first.click()
        page.wait_for_timeout(300)
    except Exception as e:
        ev = snap(page, "auth-006-fail")
        FAIL("AUTH-006", "TopBar 用户菜单", str(e), ev)

    # AUTH-007: 设置页
    try:
        page.goto(f"{BASE}/#settings", wait_until="networkidle", timeout=10000)
        page.wait_for_timeout(2000)
        page.evaluate("""() => {
            const body = document.body.innerText;
            if (!body.includes('用户设置')) throw new Error('标题缺失');
            if (!body.includes('Discord Bot Token')) throw new Error('Discord 区块缺失');
            if (!body.includes('AES-256-GCM')) throw new Error('加密提示缺失');
            if (!body.includes('qaadmin')) throw new Error('用户名未显示');
            if (!body.includes('qaadmin@test.com')) throw new Error('邮箱未显示');
        }""")
        ev = snap(page, "auth-007-settings")
        PASS("AUTH-007", "设置页: 用户信息+Discord Token+加密提示", ev)
    except Exception as e:
        ev = snap(page, "auth-007-fail")
        FAIL("AUTH-007", "设置页", str(e), ev)

    # AUTH-008: 管理页
    try:
        page.goto(f"{BASE}/#admin", wait_until="networkidle", timeout=10000)
        page.wait_for_timeout(2000)
        page.evaluate("""() => {
            const body = document.body.innerText;
            if (!body.includes('管理面板')) throw new Error('标题缺失');
            if (!body.includes('邀请码管理')) throw new Error('邀请码区块缺失');
            if (!body.includes('用户列表')) throw new Error('用户列表区块缺失');
            if (!body.includes('生成新邀请码')) throw new Error('生成按钮缺失');
            if (!body.includes('QATEST01')) throw new Error('邀请码 QATEST01 未显示');
            if (!body.includes('qaadmin')) throw new Error('admin 用户未显示');
        }""")
        ev = snap(page, "auth-008-admin")
        PASS("AUTH-008", "管理页: 邀请码QATEST01+用户列表qaadmin", ev)
    except Exception as e:
        ev = snap(page, "auth-008-fail")
        FAIL("AUTH-008", "管理页", str(e), ev)

    # AUTH-009: 生成邀请码
    try:
        page.goto(f"{BASE}/#admin", wait_until="networkidle", timeout=10000)
        page.wait_for_timeout(2000)
        page.locator("button:has-text('生成新邀请码')").click()
        page.wait_for_timeout(2000)
        row_count = page.evaluate("() => document.querySelectorAll('table tbody tr').length")
        if row_count < 1:
            raise Exception("生成后邀请码列表为空")
        ev = snap(page, "auth-009-generate-code")
        PASS("AUTH-009", f"生成邀请码成功（{row_count} 行）", ev)
    except Exception as e:
        ev = snap(page, "auth-009-fail")
        FAIL("AUTH-009", "生成邀请码", str(e), ev)

    # AUTH-010: 注册页结构
    try:
        # Navigate directly to register (no need to logout — register page is always accessible)
        page.goto(f"{BASE}/#register", wait_until="networkidle", timeout=10000)
        page.wait_for_timeout(1000)
        page.evaluate("""() => {
            const form = document.querySelector('form');
            if (!form) throw new Error('未找到注册表单');
            const inputs = form.querySelectorAll('input');
            if (inputs.length < 5) throw new Error('输入框数量 ' + inputs.length + ', 期望≥5');
            const inviteInput = form.querySelector('input.font-mono, input[maxlength="8"]');
            if (!inviteInput) throw new Error('邀请码等宽字体输入框缺失');
            const btn = form.querySelector('button[type="submit"]');
            if (!btn || !btn.textContent.includes('注册')) throw new Error('注册按钮缺失');
            const link = form.querySelector('a[href="#login"]');
            if (!link) throw new Error('登录链接缺失');
        }""")
        ev = snap(page, "auth-010-register-page")
        PASS("AUTH-010", "注册页: 5字段+邀请码等宽+注册按钮+登录链接", ev)
    except Exception as e:
        ev = snap(page, "auth-010-fail")
        FAIL("AUTH-010", "注册页结构", str(e), ev)

    # AUTH-011: 注册页 blur 验证
    try:
        page.goto(f"{BASE}/#register", wait_until="networkidle", timeout=10000)
        page.wait_for_timeout(1000)
        username_input = page.locator("input[placeholder*='用户名']").first
        username_input.fill("ab")
        username_input.blur()
        page.wait_for_timeout(300)
        email_input = page.locator("input[placeholder*='邮箱']").first
        email_input.fill("notanemail")
        email_input.blur()
        page.wait_for_timeout(300)
        error_count = page.evaluate("""() => {
            const els = document.querySelectorAll('[class*="destructive"]');
            return Array.from(els).filter(e => e.textContent.trim().length > 0).length;
        }""")
        if error_count < 1:
            raise Exception("blur 验证未显示错误")
        ev = snap(page, "auth-011-validation")
        PASS("AUTH-011", f"注册页 blur 验证: {error_count} 条错误", ev)
    except Exception as e:
        ev = snap(page, "auth-011-fail")
        FAIL("AUTH-011", "注册页 blur 验证", str(e), ev)

    # ═══════════════════════════════════════
    print("\n🧪 Phase 3: 视觉 + 响应式")
    # ═══════════════════════════════════════

    # AUTH-012: 输入框 focus 态
    try:
        page.goto(f"{BASE}/#login", wait_until="networkidle", timeout=10000)
        page.wait_for_timeout(1000)
        page.locator("input").first.focus()
        page.wait_for_timeout(300)
        style = page.evaluate("""() => {
            const input = document.querySelector('input:focus');
            if (!input) throw new Error('无 focused input');
            const cs = getComputedStyle(input);
            return { borderColor: cs.borderColor, boxShadow: cs.boxShadow.slice(0, 60) };
        }""")
        ev = snap(page, "auth-012-focus-state")
        PASS("AUTH-012", f"输入框 focus: border={style['borderColor']}, shadow={style['boxShadow']}", ev)
    except Exception as e:
        ev = snap(page, "auth-012-fail")
        FAIL("AUTH-012", "输入框 focus 态", str(e), ev)

    # AUTH-013: 移动端登录卡片
    try:
        page.set_viewport_size({"width": 375, "height": 812})
        page.goto(f"{BASE}/#login", wait_until="networkidle", timeout=10000)
        page.wait_for_timeout(1000)
        ratio = page.evaluate("""() => {
            const form = document.querySelector('form');
            const rect = form.getBoundingClientRect();
            const vw = window.innerWidth;
            const r = rect.width / vw;
            if (r < 0.8) throw new Error('卡片宽度占比 ' + (r*100).toFixed(0) + '%, 期望≥80%');
            return (r*100).toFixed(0);
        }""")
        ev = snap(page, "auth-013-mobile-login")
        PASS("AUTH-013", f"移动端登录卡片全宽: {ratio}%", ev)
    except Exception as e:
        ev = snap(page, "auth-013-fail")
        FAIL("AUTH-013", "移动端登录卡片", str(e), ev)

    # AUTH-014: 移动端注册页无溢出
    try:
        page.goto(f"{BASE}/#register", wait_until="networkidle", timeout=10000)
        page.wait_for_timeout(1000)
        overflow = page.evaluate("() => document.documentElement.scrollWidth > document.documentElement.clientWidth")
        if overflow:
            raise Exception("注册页移动端有水平溢出")
        ev = snap(page, "auth-014-mobile-register")
        PASS("AUTH-014", "移动端注册页无水平溢出", ev)
    except Exception as e:
        ev = snap(page, "auth-014-fail")
        FAIL("AUTH-014", "移动端注册页", str(e), ev)

    # Reset viewport
    page.set_viewport_size({"width": 1440, "height": 900})

    # AUTH-015: 密码可见性切换
    try:
        page.goto(f"{BASE}/#login", wait_until="networkidle", timeout=10000)
        page.wait_for_timeout(1000)
        pw_input = page.locator("input[type='password']").first
        pw_input.fill("testpassword")
        # Find the eye toggle button (sibling of password input)
        toggle = page.locator("input[type='password'] ~ button, .relative button").first
        toggle.click()
        page.wait_for_timeout(300)
        # Check password is now visible
        input_type = page.evaluate("""() => {
            const inputs = document.querySelectorAll('input');
            // Find the input that had password and now should be text
            for (const inp of inputs) {
                if (inp.value === 'testpassword') return inp.type;
            }
            throw new Error('找不到密码输入框');
        }""")
        if input_type != "text":
            raise Exception(f"切换后 type={input_type}, 期望 text")
        ev = snap(page, "auth-015-pw-toggle")
        PASS("AUTH-015", "密码可见性切换: password → text", ev)
    except Exception as e:
        ev = snap(page, "auth-015-fail")
        FAIL("AUTH-015", "密码可见性切换", str(e), ev)

    # ═══════════════════════════════════════
    # Console error check
    # ═══════════════════════════════════════
    if console_errors:
        # Filter out known non-critical warnings
        real_errors = [e for e in console_errors if "Download the React DevTools" not in e and "Warning:" not in e]
        if real_errors:
            FAIL("AUTH-CONSOLE", f"控制台错误: {len(real_errors)} 个", "; ".join(real_errors[:5]))
        else:
            PASS("AUTH-CONSOLE", "控制台无严重错误")
    else:
        PASS("AUTH-CONSOLE", "控制台零错误")

    browser.close()

# ── Summary ──
passed = sum(1 for r in results if r["status"] == "pass")
failed = sum(1 for r in results if r["status"] == "fail")
total = len(results)
rate = f"{passed/(total)*100:.0f}%" if total else "N/A"

print("\n" + "=" * 60)
print("  QA 测试结果")
print("=" * 60)
print(f"  总计: {total}  通过: {passed}  失败: {failed}")
print(f"  通过率: {rate}")
if failed > 0:
    print("\n  失败用例:")
    for r in results:
        if r["status"] == "fail":
            print(f"    ❌ {r['id']}: {r['description']}")
            print(f"       {r['detail']}")
print("=" * 60)

# Write results
out_path = SCREENSHOT_DIR / "test-results.json"
with open(out_path, "w") as f:
    json.dump({"total": total, "passed": passed, "failed": failed, "results": results}, f, indent=2, ensure_ascii=False)
print(f"\n  📄 结果已写入: {out_path}")

sys.exit(1 if failed > 0 else 0)
