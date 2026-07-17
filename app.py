#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bothosting 自动续期脚本 - 优化版
参照 aida_renew.py 的通用模式进行优化：
- 增加 logging 日志系统（替代 print）
- 增加关键步骤截图功能
- 改进 Turnstile 验证检测（精确检查 cf-turnstile-response）
- 增加速率限制检测
- 统一 Telegram 通知格式
- 增加浏览器状态清理
"""

import os, re, sys, time, json, requests, subprocess, logging
import urllib.request, urllib.parse, urllib.error
from datetime import datetime
from seleniumbase import SB

# ==================== 日志配置 ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# ==================== 环境变量配置 ====================
EMAIL         = os.environ.get("EMAIL") or ""           # 邮箱，仅用于通知
SESSION_TOKEN = os.environ.get("SESSION_TOKEN") or ""   # session token，默认登录方式
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN") or ""   # Discord Token 备用登录方式
GH_TOKEN      = os.environ.get("GH_TOKEN") or ""        # GitHub PAT，用于自动更新 session token
TG_CHAT_ID    = os.environ.get("TG_CHAT_ID") or ""      # TG chat id
TG_BOT_TOKEN  = os.environ.get("TG_BOT_TOKEN") or ""    # TG bot token
PROXY_SERVER  = os.environ.get("PROXY_SERVER", "").strip() or None

LOGIN_URL = "https://bothosting.com/login"
DASH_URL  = "https://hosting.bothosting.com/dashboard"

# ==================== Telegram 通知 ====================
def send_tg(token, chat_id, message):
    """发送 Telegram 通知（参照 aida_renew.py 的通用实现）"""
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(url, json={"chat_id": chat_id, "text": message}, timeout=10)
        if resp.status_code == 200:
            logger.info("📨 Telegram 通知已发送")
        else:
            logger.warning(f"❌ Telegram 发送失败: {resp.text}")
    except Exception as e:
        logger.error(f"❌ Telegram 发送异常: {e}")

# ==================== 工具函数 ====================
def mask_email(email):
    """脱敏邮箱显示"""
    if '@' not in email:
        return email
    local, domain = email.split('@', 1)
    if len(local) <= 4:
        masked_local = local[0] + '****' + local[-1] if len(local) > 1 else local
    else:
        masked_local = local[:2] + '****' + local[-2:]
    return f"{masked_local}@{domain}"

def get_current_ip():
    """获取当前出口 IP（参照 aida_renew.py）"""
    try:
        resp = requests.get("https://api.ip.sb/ip", timeout=10)
        return resp.text.strip()
    except Exception:
        try:
            resp = requests.get("https://api.iran.liara.ir/ip", timeout=10)
            return resp.text.strip()
        except Exception:
            return "未知"

def screenshot_path(sb, name):
    """生成截图路径"""
    return f"/tmp/{name}_{int(time.time())}.png"

def safe_screenshot(sb, name):
    """安全截图（失败时不中断流程）"""
    path = screenshot_path(sb, name)
    try:
        sb.save_screenshot(path)
        logger.info(f"📸 截图已保存: {path}")
    except Exception as e:
        logger.warning(f"⚠️ 截图失败: {e}")

def check_rate_limit(page_source):
    """检查是否触发速率限制（新增）"""
    rate_limit_patterns = [
        r'Too Many Requests',
        r'rate.limit',
        r'Too many attempts',
        r'Too many login attempts',
    ]
    for pattern in rate_limit_patterns:
        if re.search(pattern, page_source, re.IGNORECASE):
            return True
    return False

def clear_browser_state(sb):
    """清理浏览器状态（新增，参照 Lunes-Keep.py）"""
    try:
        sb.delete_all_cookies()
        sb.driver.execute_cdp_cmd("Network.clearBrowserCookies", {})
        sb.driver.execute_cdp_cmd("Network.clearBrowserCache", {})
        logger.info("🧹 浏览器缓存和 Cookie 已清理")
    except Exception as e:
        logger.warning(f"⚠️ 浏览器状态清理失败: {e}")

def wait_for_turnstile_success(sb, timeout=30):
    """等待 Turnstile 验证成功（精确检测，参照 Lunes-Keep.py）"""
    logger.info("🛡️ 等待 Turnstile 验证完成...")
    for _ in range(timeout):
        try:
            result = sb.driver.execute_script("""
                var response = document.querySelector('iframe[name^="cf-turnstile"]')?.contentDocument?.querySelector('[name="cf-turnstile-response"]');
                return response ? response.value : null;
            """)
            if result and len(result) > 10:
                logger.info("✅ Turnstile 验证成功")
                return True
        except Exception:
            pass
        time.sleep(1)
    logger.warning("⚠️ Turnstile 验证超时")
    return False

def bypass_cloudflare_challenge(sb):
    """处理 Cloudflare 整页挑战（参照 Lunes-Keep.py）"""
    try:
        sb.uc_gui_click_captcha()
        logger.info("✅ Cloudflare 挑战已处理")
        return wait_for_turnstile_success(sb)
    except Exception as e:
        logger.warning(f"⚠️ Cloudflare 挑战处理异常: {e}")
        return False

# ==================== 登录函数 ====================
def do_login_with_session(sb):
    """使用 SESSION_TOKEN 登录"""
    logger.info("🔑 尝试使用 SESSION_TOKEN 登录...")
    
    # 设置 cookie 后访问 dashboard
    sb.open(DASH_URL)
    sb.wait_for_ready_state_complete()
    time.sleep(3)
    
    # 检查是否登录成功
    current_url = sb.get_current_url()
    if "login" not in current_url.lower():
        logger.info("✅ SESSION_TOKEN 登录成功")
        return True
    
    # 手动注入 cookie
    logger.info("🍪 尝试手动注入 cookie...")
    driver = sb.driver
    driver.add_cookie({"name": "session_token", "value": SESSION_TOKEN, "domain": ".bothosting.com"})
    driver.add_cookie({"name": "login", "value": "true", "domain": ".bothosting.com"})
    driver.add_cookie({"name": "theme", "value": "system", "domain": ".bothosting.com"})
    
    sb.open(DASH_URL)
    sb.wait_for_ready_state_complete()
    time.sleep(3)
    
    current_url = sb.get_current_url()
    if "login" not in current_url.lower():
        logger.info("✅ Cookie 注入登录成功")
        return True
    
    logger.warning("❌ SESSION_TOKEN 登录失败")
    return False

def do_login_with_discord(sb):
    """使用 Discord OAuth 登录"""
    logger.info("🎮 尝试使用 Discord OAuth 登录...")
    sb.open(LOGIN_URL)
    sb.wait_for_ready_state_complete()
    time.sleep(2)
    
    # 点击 Discord 登录按钮
    try:
        sb.uc_click('button:contains("Discord")', timeout=5)
    except Exception:
        try:
            sb.uc_click('a:contains("Discord")', timeout=5)
        except Exception:
            logger.warning("⚠️ 未找到 Discord 登录按钮")
            return False
    
    # 等待 Turnstile 验证
    if not bypass_cloudflare_challenge(sb):
        logger.warning("⚠️ Turnstile 验证失败，继续尝试...")
    
    # 等待 Discord 授权页面
    time.sleep(5)
    sb.save_screenshot(screenshot_path(sb, "discord_auth"))
    
    # 查找并点击授权按钮
    try:
        sb.uc_click('button:contains("Authorize")', timeout=5)
        logger.info("✅ Discord 授权提交")
    except Exception:
        logger.warning("⚠️ 未找到授权按钮")
    
    time.sleep(5)
    current_url = sb.get_current_url()
    
    if "login" in current_url.lower():
        logger.warning("❌ Discord OAuth 登录失败")
        return False
    
    logger.info("✅ Discord OAuth 登录成功")
    return True

def update_github_secrets(session, new_token):
    """更新 GitHub Secrets 中的 session token（参照原逻辑）"""
    if not GH_TOKEN:
        logger.info("ℹ️ 未配置 GH_TOKEN，跳过 secrets 更新")
        return
    
    owner = "caixike"
    repo = "Auto-Renew-Bothosting"
    secret_name = "SESSION_TOKEN"
    
    headers = {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    
    try:
        resp = session.get(
            f"https://api.github.com/repos/{owner}/{repo}/actions/secrets/{secret_name}",
            headers=headers, timeout=15
        )
        
        if resp.status_code == 200:
            data = resp.json()
            key_id = data.get("key_id", "")
            encrypted = data.get("encrypted", "")
            
            if not key_id:
                logger.warning("⚠️ 无法获取密钥 ID")
                return
            
            # 获取公钥
            pub_resp = session.get(
                f"https://api.github.com/repos/{owner}/{repo}/actions/secrets/public-key",
                headers=headers, timeout=15
            )
            if pub_resp.status_code != 200:
                logger.warning("⚠️ 无法获取公钥")
                return
            
            pub_data = pub_resp.json()
            pub_key_id = pub_data.get("key_id", "")
            pub_key = pub_data.get("key", "")
            
            # 加密新 token
            from cryptography.hazmat.primitives.asymmetric import padding
            from cryptography.hazmat.primitives import serialization
            import base64
            
            public_key = serialization.load_der_public_key(
                base64.b64decode(pub_key.encode())
            )
            encrypted_bytes = public_key.encrypt(
                new_token.encode(),
                padding.OAEP(
                    mgf=padding.MGF1(algorithm=hashes.SHA256()),
                    algorithm=hashes.SHA256(),
                    label=None
                )
            )
            encrypted_token = base64.b64encode(encrypted_bytes).decode()
            
            # 更新 secret
            session.put(
                f"https://api.github.com/repos/{owner}/{repo}/actions/secrets/{secret_name}",
                headers=headers,
                json={
                    "key_id": key_id,
                    "encrypted_value": encrypted_token,
                },
                timeout=15
            )
            logger.info(f"✅ GitHub Secrets 已更新: {secret_name}")
        elif resp.status_code == 404:
            logger.info("ℹ️ Secret 不存在，跳过更新")
        else:
            logger.warning(f"⚠️ Secrets API 返回 {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        logger.error(f"❌ 更新 GitHub Secrets 失败: {e}")

# ==================== 续期函数 ====================
def do_renew(sb):
    """执行续期操作"""
    logger.info("🔄 尝试点击续期按钮...")
    sb.open(DASH_URL)
    sb.wait_for_ready_state_complete()
    time.sleep(3)
    
    selectors = [
        'button:contains("Renew")',
        'button:contains("Extend")',
        'a:contains("Renew")',
        'a:contains("Extend")',
        'button[aria-label*="renew"]',
        'button[title*="renew"]',
    ]
    
    for sel in selectors:
        try:
            btn = sb.find_element(sel, timeout=2)
            sb.driver.execute_script("arguments[0].click();", btn)
            logger.info(f"✅ 续期按钮点击成功: {sel}")
            return True
        except Exception:
            continue
    
    logger.warning("❌ 未找到续期按钮")
    return False

def get_expiry_info(sb):
    """提取到期信息"""
    page_source = sb.get_page_source()
    
    expiry_match = re.search(r'(?i)(?:expir(?:y|ed)|到期)[:\s]*([\d\w\-:/]+)', page_source)
    next_bill_match = re.search(r'(?i)(?:next.bill|下次扣款| renewal)[:\s]*([\d\w\-:/]+)', page_source)
    
    info = {}
    if expiry_match:
        info['expiry'] = expiry_match.group(1).strip()
    if next_bill_match:
        info['next_billing'] = next_bill_match.group(1).strip()
    
    plan_match = re.search(r'(?i)(?:plan|套餐)[:\s]*([^\n<]{2,30})', page_source)
    if plan_match:
        info['plan'] = plan_match.group(1).strip()
    
    return info

# ==================== 主函数 ====================
def main():
    logger.info("=" * 40)
    logger.info("   Bothosting 自动续期脚本 (优化版)")
    logger.info("=" * 40)
    
    # 1. 获取出口 IP
    logger.info("📍 检测出口 IP...")
    export_ip = get_current_ip()
    logger.info(f"📍 当前出口 IP: {export_ip}")
    
    # 2. 构建浏览器参数
    sb_kwargs = {"uc": True, "headless": False}
    if PROXY_SERVER:
        logger.info(f"🔗 使用代理: {PROXY_SERVER}")
        sb_kwargs["proxy"] = PROXY_SERVER
    else:
        logger.info("🌐 直连访问")
    
    _LOGIN_METHOD = "SESSION_TOKEN"
    
    with SB(**sb_kwargs) as sb:
        try:
            # 3. 清理浏览器状态
            clear_browser_state(sb)
            
            # 4. 尝试 SESSION_TOKEN 登录
            if SESSION_TOKEN:
                if not do_login_with_session(sb):
                    logger.warning("⚠️ SESSION_TOKEN 登录失败")
            else:
                logger.info("ℹ️ 未配置 SESSION_TOKEN，跳过")
            
            # 5. 如果 SESSION_TOKEN 失败，尝试 Discord OAuth
            current_url = sb.get_current_url()
            if "login" in current_url.lower():
                if DISCORD_TOKEN:
                    if not do_login_with_discord(sb):
                        msg = f"❌ Bothosting 登录失败\n📧 账户: {mask_email(EMAIL)}\n📍 IP: {export_ip}"
                        send_tg(TG_BOT_TOKEN, TG_CHAT_ID, msg)
                        safe_screenshot(sb, "login_failed")
                        return
                    _LOGIN_METHOD = "DISCORD_OAUTH"
                else:
                    msg = f"❌ Bothosting 登录失败（未配置 DISCORD_TOKEN）\n📧 账户: {mask_email(EMAIL)}"
                    send_tg(TG_BOT_TOKEN, TG_CHAT_ID, msg)
                    safe_screenshot(sb, "login_failed")
                    return
            
            # 6. 截图确认登录状态
            safe_screenshot(sb, "logged_in")
            
            # 7. 执行续期
            renew_success = do_renew(sb)
            time.sleep(3)
            
            # 8. 提取到期信息
            expiry_info = get_expiry_info(sb)
            safe_screenshot(sb, "after_renew")
            
            # 9. 更新 GitHub Secrets
            if SESSION_TOKEN:
                update_github_secrets(requests.Session(), SESSION_TOKEN)
            
            # 10. 发送 Telegram 通知
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            status_icon = "✅" if renew_success else "❌"
            status_text = "续期成功" if renew_success else "续期失败"
            
            msg = f"""🇯🇵  Bothosting续期通知

{status_icon} {status_text}
👤 登录账户: {mask_email(EMAIL)}
🔑 登录方式: {_LOGIN_METHOD}
📍 出口 IP: {export_ip}
📅 到期时间: {expiry_info.get('expiry', 'N/A')}
💳 下次扣款: {expiry_info.get('next_billing', 'N/A')}
📦 套餐: {expiry_info.get('plan', 'N/A')}
⏱️ 执行时间: {now_str}"""
            
            logger.info(msg)
            send_tg(TG_BOT_TOKEN, TG_CHAT_ID, msg)
            
            logger.info("🏁 脚本执行完毕")
            
        except Exception as e:
            logger.error(f"❌ 脚本执行异常: {e}", exc_info=True)
            safe_screenshot(sb, "error")
            msg = f"❌ Bothosting 脚本异常\n📧 账户: {mask_email(EMAIL)}\n⚠️ 错误: {str(e)[:200]}"
            send_tg(TG_BOT_TOKEN, TG_CHAT_ID, msg)
            raise

if __name__ == "__main__":
    main()
