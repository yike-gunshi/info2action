export default {
  async fetch(request, env, ctx) {
    const jsonResp = (data, status = 200) => new Response(JSON.stringify(data), {
      status,
      headers: { 'Content-Type': 'application/json; charset=utf-8', 'Access-Control-Allow-Origin': '*' }
    });

    if (request.method === 'OPTIONS') {
      return new Response(null, { headers: { 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Methods': 'GET', 'Access-Control-Allow-Headers': 'Authorization' } });
    }

    // 共享密钥从 Cloudflare env binding 读取，不硬编码。
    // 部署前设置: wrangler secret put WX_PROXY_SECRET
    const SECRET = env.WX_PROXY_SECRET;
    const auth = request.headers.get('Authorization') || '';
    if (!SECRET || auth !== `Bearer ${SECRET}`) {
      return jsonResp({ error: 'unauthorized' }, 401);
    }

    const url = new URL(request.url);
    const target = url.searchParams.get('url');
    if (!target) return jsonResp({ error: 'url required' }, 400);

    try {
      const parsed = new URL(target);
      if (parsed.hostname !== 'mp.weixin.qq.com') {
        return jsonResp({ error: 'only mp.weixin.qq.com allowed' }, 400);
      }
    } catch { return jsonResp({ error: 'invalid url' }, 400); }

    try {
      // First request: don't follow redirects to see if WeChat redirects to captcha
      const resp1 = await fetch(target, {
        headers: {
          'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.49(0x18003137) NetType/WIFI Language/zh_CN',
          'Referer': 'https://mp.weixin.qq.com/',
          'Origin': 'https://mp.weixin.qq.com',
          'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
          'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
          'Accept-Encoding': 'gzip, deflate, br',
          'Connection': 'keep-alive',
          'Sec-Fetch-Dest': 'document',
          'Sec-Fetch-Mode': 'navigate',
          'Sec-Fetch-Site': 'none',
          'Sec-Fetch-User': '?1',
          'Upgrade-Insecure-Requests': '1',
        },
        redirect: 'manual',
      });

      // If redirect, follow it manually
      let html;
      if (resp1.status >= 300 && resp1.status < 400) {
        const loc = resp1.headers.get('location');
        const resp2 = await fetch(loc.startsWith('http') ? loc : `https://mp.weixin.qq.com${loc}`, {
          headers: {
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.49(0x18003137) NetType/WIFI Language/zh_CN',
            'Referer': 'https://mp.weixin.qq.com/',
            'Accept': 'text/html',
          },
          redirect: 'follow',
        });
        html = await resp2.text();
      } else {
        html = await resp1.text();
      }

      if (html.includes('secitptpage/verify') || html.includes('环境异常')) {
        // Return debug info to help diagnose
        return jsonResp({ error: 'wechat_verify', status: resp1.status, redirect: resp1.headers.get('location') || null, html_length: html.length }, 502);
      }

      // Extract title
      let title = '';
      let m = html.match(/var\s+msg_title\s*=\s*["'](.+?)["']\s*;/);
      if (m) title = m[1];
      else { m = html.match(/og:title[^>]*content="([^"]+)"/); if (m) title = m[1]; }

      // Extract author
      let author = '';
      m = html.match(/var\s+nickname\s*=\s*["'](.+?)["']\s*;/) || html.match(/profile_nickname[^>]*>([^<]+)</);
      if (m) author = m[1];

      // Extract cover
      let cover_url = '';
      m = html.match(/var\s+msg_cdn_url\s*=\s*["'](.+?)["']\s*;/) || html.match(/og:image[^>]*content="([^"]+)"/);
      if (m) cover_url = m[1];

      // Extract content
      let content = '';
      m = html.match(/id="js_content"[^>]*>([\s\S]*?)(<\/div>\s*<\/div>\s*<\/div>)/);
      if (m) {
        content = m[1].replace(/<br\s*\/?>/gi, '\n').replace(/<\/p>/gi, '\n').replace(/<[^>]+>/g, '').replace(/&nbsp;/g, ' ').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&').replace(/\n{3,}/g, '\n\n').trim();
      }
      if (!content) {
        m = html.match(/<body[^>]*>([\s\S]*?)<\/body>/i);
        if (m) content = m[1].replace(/<script[\s\S]*?<\/script>/gi, '').replace(/<style[\s\S]*?<\/style>/gi, '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim().substring(0, 5000);
      }

      return jsonResp({ ok: true, title, author, cover_url, content: (content || '').substring(0, 8000), html_length: html.length });
    } catch (e) {
      return jsonResp({ error: e.message }, 500);
    }
  }
};
